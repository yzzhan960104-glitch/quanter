# -*- coding: utf-8 -*-
"""验证时间驱动移动止损（trailing）逻辑生效 + top1 回撤对比（固定 vs trailing）。

① 单元验证：print stop 演进（day0~20，grace5/step0.1/floor0.5），确认前grace天固定、之后收紧到floor
② top1 创板+科创 2026，固定14%仓位，固定止损 vs trailing(grace5/step0.1/floor0.5)，回撤对比

用法：PowerShell Start-Process
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from strategies.neckline.method_v0 import DEFAULTS
from strategies.neckline.backtest import scan_symbol, kelly_metrics, EXEC_DEFAULTS


def is_kbkg(sym):
    return sym.split(".")[0].startswith(("300", "301", "688", "689"))


# ① 单元验证 stop 演进
print("=== 单元验证：trailing stop 演进（颈线=10, ATR=0.5, stop_mult=1.5, grace=5, step=0.1, floor=0.5）===", flush=True)
c_star, atr_val, stop_mult = 10.0, 0.5, 1.5
base_stop = c_star - stop_mult * atr_val
print(f"  base_stop(day0) = {base_stop:.3f} (颈线-1.5ATR)", flush=True)
for holding_days in range(0, 21):
    grace, step, floor = 5, 0.1, 0.5
    if holding_days <= grace:
        eff = stop_mult
    else:
        eff = max(stop_mult - (holding_days - grace) * step, floor)
    stop = c_star - eff * atr_val
    tag = " (宽限期)" if holding_days <= grace else (" (收紧)" if eff > floor else " (floor卡住)")
    if holding_days % 2 == 0 or holding_days in (1, 5, 6, 15):
        print(f"  day{holding_days:>2}: eff_mult={eff:.2f} stop={stop:.3f}{tag}", flush=True)

# ② top1 创板+科创 2026，固定 vs trailing
state = json.load(open("logs/param_iter_state.json", encoding="utf-8"))
top1 = sorted(state["tried"].items(), key=lambda x: x[1]["ann"], reverse=True)[0]
params = json.loads(top1[0])
id_keys = ["window", "min_touches", "min_suppression", "local_extrema_window", "min_bottoms",
           "breakout_vol_mult", "min_rr", "max_h_atr", "stop_atr_mult", "tp_h_mult", "decay_tau"]
exec_keys = ["max_holding", "max_wait", "cooldown", "buy_limit_atr_mult",
             "tp1_h_mult", "tp1_portion", "cancel_thresh_mult"]
id_p = {k: params[k] for k in id_keys if k in params}
exec_p_base = {k: params[k] for k in exec_keys if k in params}

lake = pd.read_parquet("data_lake/a_shares_daily.parquet").reset_index()
lake["date"] = pd.to_datetime(lake["date"])
lake = lake[lake["date"] >= pd.Timestamp("2025-10-01")]
lake = lake[lake["symbol"].apply(is_kbkg)]
lake = lake.set_index(["date", "symbol"])
universe = {}
for s in lake.index.get_level_values("symbol").unique():
    try:
        df = lake.xs(s, level="symbol").sort_index()
        if len(df) >= 60:
            universe[s] = df
    except Exception:
        continue
print(f"\n创板+科创 2025-10+ {len(universe)}只", flush=True)


def run(lbl, exec_p):
    # 显式构造 id_cfg/exec_cfg 传入 scan_symbol（去全局 mutation，与 param_iter.run_one 同口径）。
    id_cfg = {**DEFAULTS, **id_p}
    exec_cfg = {**EXEC_DEFAULTS, **exec_p}
    all_filled = []
    t0 = time.time()
    for sym, df in universe.items():
        try:
            filled, _, _ = scan_symbol(df, id_cfg["window"], exec=exec_cfg, id_cfg=id_cfg)
            all_filled.extend(filled)
        except Exception:
            continue
    f2026 = sorted([r for r in all_filled if pd.to_datetime(r["signal_date"]) >= pd.Timestamp("2026-01-01")],
                   key=lambda r: r["signal_date"])
    # 固定14% curve + 回撤
    pos = 0.14
    curve = 1.0; peak = 1.0; max_mdd = 0.0; mdd_start = mdd_end = 0; peak_idx = 0
    for i, r in enumerate(f2026):
        curve *= (1 + pos * r["avg_pnl_pct"] / 100)
        if curve > peak: peak = curve; peak_idx = i
        mdd = curve / peak - 1
        if mdd < max_mdd: max_mdd = mdd; mdd_start = peak_idx; mdd_end = i
    years = (pd.to_datetime(f2026[-1]["signal_date"]) - pd.to_datetime(f2026[0]["signal_date"])).days / 365.25
    ann = curve ** (1 / years) - 1 if curve > 0 else -1
    from collections import Counter
    rc = Counter(r["exit_reason"] for r in f2026)
    print(f"\n  [{lbl}] n={len(f2026)} 年化{ann*100:+.1f}% 回撤{max_mdd*100:.0f}% curve{curve:.3f} 用{time.time()-t0:.0f}s", flush=True)
    print(f"    exit分布: {dict(rc)}", flush=True)
    print(f"    回撤段: 第{mdd_start+1}~{mdd_end+1}笔 ({f2026[mdd_start]['signal_date']}~{f2026[mdd_end]['signal_date']})", flush=True)
    # 止损笔的平均亏损（trailing 应让止损更小亏）
    sl = [r["avg_pnl_pct"] for r in f2026 if r["exit_reason"] == "stop_loss"]
    if sl:
        print(f"    stop_loss {len(sl)}笔 平均{sum(sl)/len(sl):+.2f}% 最差{min(sl):+.1f}%", flush=True)
    return ann, max_mdd


print("\n=== top1 固定 vs trailing（创板+科创 2026，固定14%）===", flush=True)
run("固定止损", {**exec_p_base, "trailing_grace": 0, "trailing_step": 0.0})
run("trailing grace5/step0.1/floor0.5", {**exec_p_base, "trailing_grace": 5, "trailing_step": 0.1, "trailing_floor": 0.5})
print("\n=== 完成 ===", flush=True)
