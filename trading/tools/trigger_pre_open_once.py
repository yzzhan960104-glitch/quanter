# -*- coding: utf-8 -*-
"""一次性手动触发 ``pre_open``（盘中补挂当日已确认计划）。

物理意图（pre_open 漏挂的应急补挂入口 · 仿 ``trigger_eod_once.py`` 范式）：
    pre_open 正常由 APScheduler cron 在 09:22 触发一次。若该时刻网关锁死
    （qmt-connect-1-rootcause：双进程抢 session / 客户端未起），cron 跑完
    submitted=0 当日就不再自动重试（下一轮已是次日 09:22）。网关恢复后若
    研究员决定盘中补挂当日 plan，本脚本手动触发一次真实 pre_open。

与 ``trigger_eod_once.py`` 区别：
    - eod 脚本：不连网关、不下单（eod 只产计划）。
    - 本脚本：连网关真挂单（pre_open 内部 _submit → trading_service → broker.place_order）。

流程：
    load_env → 构造 TradingEngine（不 start scheduler，无 cron 副作用）
    → get_gateway → connect（带重试，让停 engine 后 session 释放有缓冲）
    → set_order_update_callback（成交回报回流到 _handle_order_update + 账本 + 钉钉）
    → pre_open(today, ports=eng._ports)（经 ports 激活三段闸 + 实例白名单）→ 等数秒收回报 → 退出

⚠️ 前置红线（必须人工先做，qmt-connect-1-rootcause）：
    必须先停掉常驻 engine 进程释放 QMT session，否则脚本 connect 会因
    session 占用返回 -1（双进程抢锁），且可能让锁态复发。脚本不自行杀 engine——
    杀进程是 outward 不可逆动作，由研究员/调度显式执行。

幂等安全（state-store-redesign T8）：
    pre_open 内部 has_order(OPEN) 做 DB 幂等检查；当前 state_store 6 表未 init
    时 has_order 抛异常被吞（不阻断），故脚本单次执行 = 首次挂单，无重复风险。
    脚本不重试 pre_open（逐单 try-except，失败即 continue），单次执行不会重挂。

用法（前置：已停常驻 engine）：
    .venv310/Scripts/python.exe trading/tools/trigger_pre_open_once.py
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

# C-6 V3：单一时间源（CLI 今日触发走 clock.today，与 engine.py V2 同款）。
from trading import clock

# 三层 dirname（与 trigger_eod_once.py 同范式，防 tools 路径少算一层 bug）：
# trigger_pre_open_once.py → tools → trading → quanter（项目根）。
ROOT = Path(__file__).resolve().parent.parent.parent
os.chdir(ROOT)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 切 stdio UTF-8（Windows GBK 控制台默认编码不了 ✅/❌ 等 Unicode 符号，
# 与 run_trading_engine.bat 的 PYTHONUTF8=1 同理，防中文乱码/UnicodeEncodeError）。
for _s in ("stdout", "stderr"):
    _st = getattr(sys, _s, None)
    if _st is not None and hasattr(_st, "reconfigure"):
        try:
            _st.reconfigure(encoding="utf-8")
        except Exception:
            pass

# 加载 .env（override=True：.env 是单一真相源，强制覆盖系统 env，与 __main__ 同口径）。
try:
    from dotenv import load_dotenv

    load_dotenv(override=True)
except ImportError:
    pass

# 中文友好日志格式（与 __main__ 同构），level=INFO 可见挂单逐单结果。
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


async def _run() -> int:
    # W1-B（Task 10）：pre_open 直 import 物理真身 phases.pre_open（engine re-export 垫层
    # 已删）；TradingEngine/get_gateway 是 engine 自有符号，仍从 trading.engine 取。
    # 保函数内 lazy：工具脚本冷启动只拉所需链，不顶层装配引擎。
    from trading.engine import TradingEngine, get_gateway
    from trading.phases.pre_open import pre_open

    today = clock.today()
    mode = os.getenv("AUTO_TRADE_MODE", "dry_run")
    print(f"[trigger_pre_open] today={today} AUTO_TRADE_MODE={mode}")
    if mode != "live":
        print("[trigger_pre_open] ⚠️ 非 live 模式，pre_open 将走 DRY_RUN 不真挂单")

    # 构造 engine（装 scheduler 对象但不 start，无 cron 副作用、不连网关）。
    # 取 eng 只为拿 _handle_order_update 回调 + _gw 反查句柄（与 __main__._run_forever 装配一致）。
    eng = TradingEngine()

    gw = get_gateway()
    if gw is None:
        print("[trigger_pre_open] 网关未装配（gw=None），退出")
        return 2

    # connect 重试（最多 3 次，每次间隔 3s）：
    # 停常驻 engine 后 QMT session 释放可能有延迟（柜台 keepalive / 进程退出缓冲），
    # 立即 connect 可能撞上残留 session 占用返回 -1，给几秒缓冲更稳。
    for attempt in range(1, 4):
        try:
            await gw.connect()
            gw.set_order_update_callback(eng._handle_order_update)  # 成交回报回流
            eng._gw = gw  # 供 handler 反查 _orders 判 BUY/SELL side
            print(f"[trigger_pre_open] 网关已连接（第 {attempt} 次）+ 成交回调已注册")
            break
        except Exception as e:
            print(f"[trigger_pre_open] connect 第 {attempt} 次失败：{e}")
            if attempt == 3:
                print("[trigger_pre_open] connect 三次失败（疑 session 未释放/客户端未起），放弃")
                return 3
            await asyncio.sleep(3)

    # 初始化本地持仓账本（gap4 · 幂等建表）：pre_open 成功挂单后 _handle_order_update
    # 会写 fill/position，账本必须先就绪（与 __main__._run_forever 同口径）。
    from trading import position_book
    position_book.init_db()

    # 跑 pre_open（读当日 plan → 确认闸 → 撤昨日单 → max_wait 过滤 → DB 幂等 → 逐单挂单）。
    # fix(T1 C1)：必须显式传 ports=eng._ports——恢复 baseline「TradingEngine().__init__ 置
    # _ACTIVE_ENGINE=self」语义（三段闸 ACTIVE + 动态白名单注入实例属性）。T1 消单例桥后
    # gate 仅在 ports 非空时生效；裸调 pre_open(today) 会走 ports is None 防御分支静默跳过
    # data-ready gate → 可能在 pipeline 数据未就绪时挂单（行为等价红线违规）。本脚本是 live
    # 应急补挂入口（见模块 docstring L4-8），gate 绝不可被静默旁路。
    result = await pre_open(today, ports=eng._ports)
    print(f"[trigger_pre_open] pre_open 结果：{result}")

    # 等 6s 收成交回报（连续竞价时段限价单若价格匹配可能立即部分成交，
    # 回报经 _handle_order_update 写 fill/position + 钉钉成交通知）。
    await asyncio.sleep(6)
    print("[trigger_pre_open] 完成，退出（后续止损监控由重启后的常驻 engine 接管）")
    return 0


if __name__ == "__main__":
    # stdout UTF-8 治理:防 GBK 管道崩 emoji(详见 infra/pyio.py)
    from infra.pyio import force_utf8_stdout
    force_utf8_stdout()
    sys.exit(asyncio.run(_run()))
