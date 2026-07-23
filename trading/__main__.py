# -*- coding: utf-8 -*-
"""二期自动交易引擎独立常驻进程入口：``python -m trading``。

============================================================================
Why 独立进程（Task5/9 风险官硬约束 · 绝对红线）
============================================================================
本入口起一个独立常驻 Python 进程，**不寄生 server uvicorn**：

- ``trading.dynamic_whitelist._DYNAMIC`` 是模块级全局（当日计划标的临时注入），
  只在 engine 进程内有效（设计预期，见 ``dynamic_whitelist.py`` 模块 docstring）。
- 若 engine 与 server 同进程：engine 在 pre_open 注入的 _DYNAMIC 会污染 server 的
  手动下单路径（Cockpit/前端），导致 server 手动下单越过静态 env 白名单（前视污染），
  破坏「server 行为与改造前完全一致」的向后兼容红线。
- 因此 server 的 lifespan **不** import 本模块、不构造 TradingEngine；入口唯一在此。

职责切分（薄入口原则 · Karpathy 极简）：
- 本入口只做三件事：① 加载 .env ② 起 event loop 守护 AsyncIOScheduler
  ③ LIVE 模式启动期 WARNING 提醒影子模式红线。
- 全部业务逻辑（四触发点、APScheduler cron 装配、交易日判定、影子分流）都在
  ``trading/engine.py::TradingEngine``（Task9），本入口不重复实现任何业务逻辑。

============================================================================
⚠️ Scope 边界：本入口【不】做策略层数据源注入
============================================================================
本入口只起 APScheduler 常驻进程；四触发点的真实数据源属「二期引擎上线集成」
阶段的工作（SOP/follow-up），不在 Task 10/11 代码 scope：

- ``NecklineMethodStrategy.scan_at`` 扫颈线法信号（eod_plan 消费）
- 持仓状态机 ``stop_prices`` map（stop_loss_monitor 消费）
- ``active.json`` 真实 local_positions（post_close 对账消费）

Task 9 的四个内部触发方法（``_eod/_pre_open/_stoploss/_post_close``）已是
**安全 no-op**：先过 ``calendar.is_trading_day`` 判交易日，再 logger.info 触发
记录，数据源为 None/空时优雅降级不崩。故 __main__ 起进程后 APScheduler 即便
触发这四个 job 也不会崩。

详见 ``docs/superpowers/plans/2026-07-21-auto-trading-engine.md`` Task 11 SOP
+ ledger 必修清单（策略层→引擎层信号源集成 = 二期引擎上线集成阶段）。

============================================================================
Windows 进程托管
============================================================================
本入口是前台进程（stdout 日志），设计成可被 schtasks / PM2 / terminal tab 托管：
- Task 11 的 ``run_trading_engine.bat`` 会调它（schtasks 注册开机自启）。
- Ctrl-C（KeyboardInterrupt）→ 优雅 ``eng.shutdown()``（APScheduler ``wait=False``
  不等 pending job，进程退出场景）。
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys

# 加载 .env（Task4 已装 python-dotenv；环境无 dotenv 时 fallback 跳过，env 由
# 外层 schtasks/PM2 注入亦可——本行只是开发便利，非业务依赖）。
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

# 中文友好日志格式：asctime + levelname + logger name + message。
# level=INFO：启动/触发记录可见；DEBUG 太吵（APScheduler 内部日志量大）。
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def _run_forever() -> None:
    """起 TradingEngine + 守护 event loop（APScheduler 后台跑四 cron）。

    Why ``while True: await asyncio.sleep(3600)`` 而非 ``await eng.sched.running``
    这种事件等待：APScheduler 的 AsyncIOScheduler 在后台协程内跑 job 调度，主协程
    只需「挂起不退出」即可；每小时醒一次无业务意义（仅保活心跳，避免某些事件循环
    实现对纯阻塞 sleep 的超时打断异常）。 KeyboardInterrupt/CancelledError 由外层
    ``asyncio.run`` 冒泡到 ``__main__`` 守卫统一处置。
    """
    # 惰性 import：避免模块顶层 import 触发 trading 包重链（test 导入本模块时不
    # 应连带拉起 engine 依赖链；engine.py 顶层 import apscheduler 等）。
    from trading.engine import TradingEngine, get_gateway

    eng = TradingEngine()

    # ----------------------------------------------------------------------
    # 连接网关 + 注册成交回报回调（修 G5 根因：原 __main__ 既不 connect 也不
    # set_order_update_callback，导致 Task10 写的 _handle_order_update 永不被触发，
    # QMT 异步成交回报无法回流到 engine._orders / 钉钉 / 自动止盈挂单链路）。
    #
    # Why 在 eng.start() 之前：APScheduler 一旦 start，下一个 cron 触发点（如
    # stoploss_monitor）就可能进 place_order → 需要回调链路已就绪；故 connect +
    # 注入 callback 必须先于 scheduler 启动，保证任何触发点跑时回调链已通。
    #
    # Why 异常兜底不抛：连接失败时仍让 cron 起来——触发点内部 get_gateway() 会
    # 再次惰性取单例做兜底判空（None 时走 dry_run 分支），这里只打 exception 不
    # 阻断 APScheduler 装配，避免「网关短时连不上」直接让整个常驻进程退出。
    # ----------------------------------------------------------------------
    gw = get_gateway()
    if gw is not None:
        try:
            await gw.connect()  # async：内部 run_in_executor 包 xtquant C++ 阻塞 connect
            gw.set_order_update_callback(eng._handle_order_update)  # sync 注入成交回报回调
            eng._gw = gw  # 供 handler 反查 _orders 判 BUY/SELL side（见 engine._side_from_update）
            logger.info("网关已连接 + 成交回调已注册")
        except Exception:
            logger.exception("网关连接失败（cron 仍启动，触发点内部 get_gateway 兜底）")
    else:
        logger.warning("未装配网关（AUTO_TRADE_MODE=dry_run 影子模式，回调链路不生效）")

    eng.start()  # 注册四 cron job + 启动 AsyncIOScheduler（不阻塞）
    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        # Ctrl-C / 外部 cancel：优雅 shutdown APScheduler（wait=False 不等 pending job）。
        eng.shutdown()


if __name__ == "__main__":
    # 启动期模式读取（默认 dry_run · 影子红线）。
    # 缺省 dry_run：未显式 AUTO_TRADE_MODE=live 一律按影子处理，宁可漏挂单也
    # 不在未观测足够天数时盲发真单。
    mode = os.getenv("AUTO_TRADE_MODE", "dry_run")
    logger.info("=== 自动交易引擎启动（AUTO_TRADE_MODE=%s）===", mode)

    if mode != "dry_run":
        # LIVE 模式启动期显眼提醒（spec 红线）：
        # 影子模式必须跑满 TRADE_SHADOW_MIN_DAYS（≥5）才允许切 live。
        # 这条 WARNING 是最后一道人工确认闸——避免运维误改 env 即裸跑真单。
        logger.warning(
            "⚠️ LIVE 模式：将真实下单！确保影子模式已跑满 TRADE_SHADOW_MIN_DAYS"
            "(=%s) 天，且对账连续无 drift、网关连通、止损行情源已接入。",
            os.getenv("TRADE_SHADOW_MIN_DAYS", "5"),
        )

    try:
        asyncio.run(_run_forever())
    except KeyboardInterrupt:
        # Ctrl-C 在 asyncio.run 外层再次被捕（双保险）。
        logger.info("收到 Ctrl-C，进程退出。")
        sys.exit(0)
