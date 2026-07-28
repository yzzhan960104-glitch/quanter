# -*- coding: utf-8 -*-
"""一次性手动触发 engine._eod（T 日盘后扫信号 + 落计划 + 真发钉钉）。

物理意图（A2 验证 · schtasks 未上线前的手动入口）：
    不依赖 schtasks / 常驻 APScheduler，独立触发一次真实 ``_eod``，确认全链路：
      ① scan_live 能扫出真实颈线法信号
      ② trading_plan.save_plan 落盘 confirmed=False
      ③ trading_plan.push_plan_to_dingtalk 真发钉钉（不拦推送）
    不连网关、不下单——``_eod`` 本身只产计划（confirmed=False 待人审），下单是次日
    pre_open 且需研究员确认；叠加 AUTO_TRADE_MODE=dry_run 双保险。

与 smoke_trading_engine.py 区别：
    - smoke：空信号 + monkeypatch 拦推送 → 验代码路径不崩（不发消息、不扫信号）
    - 本脚本：真实信号扫描 + 不拦推送 → 验完整链路（真发钉钉、真扫盘）

构造 TradingEngine 但【不 start】scheduler（``__init__`` 只装 AsyncIOScheduler 对象
+ add_job，不起 cron 循环、不连网关），故无任何常驻副作用。

用法：
    .venv310/Scripts/python.exe trading/tools/trigger_eod_once.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

# 三层 dirname（与 smoke_trading_engine.py 同范式，防 tools 路径少算一层 bug）：
# trigger_eod_once.py → tools → trading → quanter（项目根）。
ROOT = Path(__file__).resolve().parent.parent.parent
os.chdir(ROOT)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 切 stdio UTF-8（Windows GBK 控制台默认编码不了 ✅/❌ 等 Unicode 符号，与
# run_trading_engine.bat 的 PYTHONUTF8=1 同理，防中文乱码/UnicodeEncodeError）。
for _s in ("stdout", "stderr"):
    _st = getattr(sys, _s, None)
    if _st is not None and hasattr(_st, "reconfigure"):
        try:
            _st.reconfigure(encoding="utf-8")
        except Exception:
            pass


async def _run() -> int:
    from trading.engine import TradingEngine

    # 构造 engine（装 scheduler 但不 start，无 cron 副作用、不连网关）。
    eng = TradingEngine()
    today = datetime.now().strftime("%Y-%m-%d")
    mode = os.getenv("AUTO_TRADE_MODE", "dry_run")
    print(f"[trigger] 触发 _eod（today={today} AUTO_TRADE_MODE={mode}）")
    print("[trigger] 将真发钉钉「T-1 交易计划」到运营群（不拦推送）……")

    # _eod：交易日判定 → resolve_active → 读 lake → scan_live → eod_plan（落盘+真发钉钉）
    #        → _broadcast_positions_pnl（网关 None 软降级，不阻断主流程）
    await eng._eod()

    # 复核落盘计划（肉眼确认信号标的 + 止损/止盈/盈亏比）
    plan_dir = Path(os.getenv("TRADE_PLAN_DIR", "logs/trading_plans"))
    plan_path = plan_dir / f"plan_{today}.json"
    if plan_path.exists():
        payload = json.loads(plan_path.read_text(encoding="utf-8"))
        orders = payload.get("orders", [])
        print(f"[trigger] ✅ 落盘 {plan_path}")
        print(f"[trigger] date={payload.get('date')} confirmed={payload.get('confirmed')} n_orders={len(orders)}")
        for o in orders[:10]:
            od = o.get("order", {})
            print(f"  - {od.get('symbol')} {od.get('side')} {od.get('qty')}@{od.get('price')}"
                  f" 止损={o.get('stop_price')} 止盈={o.get('take_profit')} rr={o.get('rr')}")
        if len(orders) > 10:
            print(f"  ...（其余 {len(orders) - 10} 单见 plan json）")
    else:
        print(f"[trigger] ⚠️ 计划未落盘（{plan_path}）——可能无在线实验/非交易日/信号为空")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(_run()))
    except Exception:
        import traceback
        print("[trigger] ❌ 异常：")
        traceback.print_exc()
        sys.exit(1)
