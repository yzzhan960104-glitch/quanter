# -*- coding: utf-8 -*-
"""R6-8（线上 pilot 参数）vs B3（研究线 incumbent）部署口径配对。

回答："线上 R6-8 换成 B3 在部署口径下值多少"。CRN K=21 种子配对，
inner 2021-24 / outer 2025-26 双段 + oracle 全期 + 分年 + 单笔画像。
纯参数隔离：同 universe（1e5 创科新湖）、零过滤、同 PositionModel。
R6-8 参数抄录自 emquant/emquant_neckline_pilot.py §0 ID_PARAMS/EXEC_PARAMS
（行 46-47，export_snapshot 定稿快照；去掉研究侧不消费的费率键）。"""
import json
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
import pandas as pd

from diag.r9_breadth_loop import BASE_LIQUIDITY, _ann, _filled_dicts, _pm, _seg, build_universe
from discovery.objective import run_full_scan
from discovery.split import extended_split

R68 = {  # emquant §0 快照（08-26 换代，R6-8 九小时循环冠军）
    **{'breakout_vol_mult': 1.0, 'decay_tau': 60.0, 'local_extrema_window': 3,
       'max_h_atr': 4.5, 'min_bottoms': 2, 'min_rr': 2.0,
       'min_suppression': 0.3, 'min_touches': 2, 'momentum_gate': None,
       'stop_atr_mult': 1.5, 'tp_h_mult': 1.5, 'window': 60},
    **{'buy_limit_atr_mult': 2.5, 'cancel_thresh_mult': None,
       'chase_entry': True, 'cooldown': 0, 'max_holding': 30, 'max_wait': 27,
       'timeout_extend_days': 0, 'timeout_extend_min_pnl': 0.05,
       'tp1_h_mult': 2.0, 'tp1_portion': 0.9, 'tp_adapt_h_atr': None,
       'tp_adapt_scale': 0.5, 'trailing_floor': 0.5, 'trailing_grace': 10,
       'trailing_step': 0.05},
}
SEEDS = list(range(21))


def main():
    lake = pd.read_parquet("data_lake/a_shares_daily.parquet",
                           filters=[("date", ">=", pd.Timestamp("2021-01-01"))])
    uni = build_universe(lake, BASE_LIQUIDITY)
    udates = next(iter(uni.values())).index
    split = extended_split()

    B3 = json.load(open("logs/r6_10_loop/state.json", encoding="utf-8"))["base"]
    full = _seg("full", date(2021, 1, 1), date(2026, 12, 31))
    out = {}
    for tag, p in (("R68_online", R68), ("B3", B3)):
        filled = _filled_dicts(run_full_scan(p, uni))
        d = {}
        d["inner"] = {s: _ann(filled, split.inner, udates, _pm(seed=s))["ann"] for s in SEEDS}
        d["outer"] = {s: _ann(filled, split.outer, udates, _pm(seed=s))["ann"] for s in SEEDS}
        d["oracle_full"] = _ann(filled, full, udates, _pm())["ann"]
        d["n_taken_med"] = float(np.median(
            [_ann(filled, split.inner, udates, _pm(seed=s))["n_taken"] for s in SEEDS[:5]]))
        d["yearly"] = {y: round(float(np.median(
            [_ann(filled, _seg(f"y{y}", date(y, 1, 1), date(y, 12, 31)), udates,
                  _pm(seed=s))["ann"] for s in SEEDS[:5]])), 4)
            for y in (2021, 2022, 2023, 2024, 2025, 2026)}
        inners = [r for r in filled if split.inner.covers(pd.Timestamp(r["signal_date"]))]
        pnls = [r["avg_pnl_pct"] for r in inners]
        d["per_trade"] = {"n": len(pnls), "mean": round(float(np.mean(pnls)), 3),
                          "median": round(float(np.median(pnls)), 3),
                          "win": round(sum(p > 0 for p in pnls) / len(pnls), 4)}
        out[tag] = d
        print(f"[{tag}] inner 中位 {np.median(list(d['inner'].values())):+.4f} | "
              f"outer 中位 {np.median(list(d['outer'].values())):+.4f} | "
              f"oracle {d['oracle_full']:+.4f} | 单笔 {d['per_trade']}", flush=True)

    for seg in ("inner", "outer"):
        deltas = [out["B3"][seg][s] - out["R68_online"][seg][s] for s in SEEDS]
        print(f"\n[{seg}] B3 − R68: Δ中位 {np.median(deltas):+.4f} | "
              f"同向 {sum(v > 0 for v in deltas)}/21 | "
              f"B3 中位 {np.median(list(out['B3'][seg].values())):+.4f} vs "
              f"R68 中位 {np.median(list(out['R68_online'][seg].values())):+.4f}",
              flush=True)
    print(f"\n[oracle 全期] B3 {out['B3']['oracle_full']:+.4f} vs "
          f"R68 {out['R68_online']['oracle_full']:+.4f}", flush=True)
    print(f"[n_taken 中位] B3 {out['B3']['n_taken_med']:.0f} vs "
          f"R68 {out['R68_online']['n_taken_med']:.0f}", flush=True)
    print(f"[分年 deployable 中位] B3 {out['B3']['yearly']}", flush=True)
    print(f"                      R68 {out['R68_online']['yearly']}", flush=True)
    json.dump(out, open("logs/r68_vs_b3_pairing.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
