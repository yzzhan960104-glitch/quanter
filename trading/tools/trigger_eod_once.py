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
from pathlib import Path

# 三层 dirname（与 smoke_trading_engine.py 同范式，防 tools 路径少算一层 bug）：
# trigger_eod_once.py → tools → trading → quanter（项目根）。
ROOT = Path(__file__).resolve().parent.parent.parent
os.chdir(ROOT)
# 2026-08-05 事故修复：原 `from trading import clock` 在锚根之前执行，脚本直跑必抛
# ModuleNotFoundError（sys.path[0]=tools/）。仓库根必须先入 path，再做仓库内 import。
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# C-6 V3：单一时间源（CLI 今日触发走 clock.today，与 engine.py V2 同款）。
from trading import calendar, clock


def _plan_date_for(today: str) -> str:
    """补跑/触发场景的计划日：eod 恒产 T+1（plan_date），不能查 today。

    2026-08-05 修复：原复核逻辑查 ``plan_{today}.json``，而 ``_eod()`` 落的是
    ``plan_{next_trading_day(today)}.json``——补跑场景永远误报「计划未落盘」。
    """
    return calendar.next_trading_day(today)


def _plan_path_for(today: str) -> Path:
    """【保留 · C3 JSON 回退窗口】补跑/触发场景的落盘文件路径（仅 fallback 显示用）。

    C2d：复核主语义改查 ``count_signals_by_plan_date``（计划已落 DB），文件路径仅作
    操作员肉眼 fallback 显示（C3 load_plan 同款 DB 优先 + JSON 回退窗口，保留一发布周期）。
    """
    plan_date = _plan_date_for(today)
    plan_dir = Path(os.getenv("TRADE_PLAN_DIR", "logs/trading_plans"))
    return plan_dir / f"plan_{plan_date}.json"


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
    today = clock.today()
    mode = os.getenv("AUTO_TRADE_MODE", "dry_run")
    print(f"[trigger] 触发 _eod（today={today} AUTO_TRADE_MODE={mode}）")
    print("[trigger] 将真发钉钉「T-1 交易计划」到运营群（不拦推送）……")

    # _eod：交易日判定 → resolve_active → 读 lake → scan_live → eod_plan（落盘+真发钉钉）
    #        → _broadcast_positions_pnl（网关 None 软降级，不阻断主流程）
    await eng._eod()

    # 复核计划落 DB（C2d：真相源 = trade_event(SIGNAL).meta，按 plan_date 查）。
    # 物理意图：eod 产计划 = SIGNAL 行落 DB；count>0 即「计划已落」。plan_date=T+1
    # （与 _eod 内部 next_trading_day 同口径）。文件路径仅作操作员肉眼 fallback 显示
    # （C3 load_plan DB 优先 + JSON 回退窗口，保留一发布周期）。
    from trading import state_store

    plan_date = _plan_date_for(today)
    try:
        n_signals = state_store.count_signals_by_plan_date(plan_date)
    except Exception:
        # DB 读失败（无 init / 文件锁 / 表缺失）：保守视为「未落」，提示操作员排查。
        n_signals = -1
    if n_signals > 0:
        # 读 DB meta 拿「精确 per-symbol 计划参数」（真相源，非 JSON 落盘副本）。
        metas = state_store.list_signals_with_meta_by_plan_date(plan_date)
        print(f"[trigger] ✅ 计划落 DB plan_date={plan_date} n_signals={n_signals}")
        for m in metas[:10]:
            od = m.get("order") or {}
            print(f"  - {m.get('symbol')} {od.get('side')} {od.get('qty')}@{od.get('price')}"
                  f" 止损={m.get('stop_price')} 止盈={m.get('take_profit')} rr={m.get('rr')}")
        if len(metas) > 10:
            print(f"  ...（其余 {len(metas) - 10} 单见 DB）")
    else:
        # DB 无 SIGNAL：fallback 看老 JSON（C3 兼容窗口）+ 友好提示。
        plan_path = _plan_path_for(today)
        if plan_path.exists():
            try:
                payload = json.loads(plan_path.read_text(encoding="utf-8"))
                orders = payload.get("orders", [])
                print(f"[trigger] ⚠️ DB 无 SIGNAL 但 JSON 存在（兼容窗口）{plan_path}")
                print(f"[trigger] date={payload.get('date')} n_orders={len(orders)}")
            except Exception:
                print(f"[trigger] ⚠️ DB 无 SIGNAL 且 JSON 损坏：{plan_path}")
        else:
            print(f"[trigger] ⚠️ 计划未落（DB count={n_signals}，{plan_path} 亦不存在）"
                  "——可能无在线实验/非交易日/信号为空")
    return 0


if __name__ == "__main__":
    # stdout UTF-8 治理:防 GBK 管道崩 emoji(详见 infra/pyio.py)
    from infra.pyio import force_utf8_stdout
    force_utf8_stdout()
    try:
        sys.exit(asyncio.run(_run()))
    except Exception:
        import traceback
        print("[trigger] ❌ 异常：")
        traceback.print_exc()
        sys.exit(1)
