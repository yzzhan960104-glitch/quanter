# -*- coding: utf-8 -*-
"""R6-10 · L1 时间止损受控验证（2026-08-26 用户裁决发射）。

假设 H-time：入场 N 个交易日未触发任何 tp → 离场（持有期分桶单调衰减
74%→35% 的结构化兑现）。受控：base=R6-8 冠军原样，仅变 time_stop_days。

预登记裁决（与调研报告一致）：单笔 avg ↑（砍 [8,∞) 负尾）∧ timeout 份额↓
∧ outer_raw 不降 ∧ 模拟线不降 ∧ 年段（2022-26）无恶化>2pp。
产物 logs/r6_10_h_time.json。
"""
import json
import os
import sys
import time
from copy import deepcopy
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

LEVELS = [0, 4, 5, 7, 10, 15]   # 0=关（对照）


def main():
    from discovery.snapshot import freeze
    from discovery.split import holdout_split, Segment
    from discovery.objective import run_full_scan, portfolio_metrics
    from discovery.manual_risk_sim import build_block_calendar

    base = json.loads(open("logs/r6_8_loop/state.json", encoding="utf-8").read())["base"]
    universe, _ = freeze("2021-01-01")
    split = holdout_split()
    cal = build_block_calendar(universe)
    udates = next(iter(universe.values())).index
    year_segs = {y: Segment(f"y{y}", date(y, 1, 1), date(y, 12, 31))
                 for y in (2022, 2023, 2024, 2025, 2026)}

    rows = {}
    for tsd in LEVELS:
        p = {**deepcopy(base), "time_stop_days": tsd}
        t0 = time.time()
        filled = run_full_scan(p, universe)
        outer = [r for r in filled if split.outer.covers(pd.Timestamp(r["signal_date"]))]
        ps = [r["avg_pnl_pct"] for r in outer]
        n_ts = sum(1 for r in outer if r["exit_reason"] == "time_stop")
        n_to = sum(1 for r in outer if r["exit_reason"] == "timeout")
        m_raw = portfolio_metrics(outer, split.outer, udates)
        m_mr = portfolio_metrics(outer, split.outer, udates, block_dates=cal)
        row = {"tsd": tsd, "n_outer": len(outer),
               "avg": round(sum(ps) / len(ps), 2),
               "median": round(sorted(ps)[len(ps) // 2], 2),
               "n_time_stop": n_ts, "n_timeout": n_to,
               "raw_ann": m_raw["ann"], "mr_ann": m_mr["ann"],
               "years": {y: round(portfolio_metrics(
                   [r for r in filled if seg.covers(pd.Timestamp(r["signal_date"]))],
                   seg, udates)["ann"], 4) for y, seg in year_segs.items()},
               "sec": round(time.time() - t0, 1)}
        rows[tsd] = row
        print(f"[tsd={tsd:>2}] n={len(outer):>5} avg={row['avg']:+.2f}% "
              f"ts={n_ts:>4} to={n_to:>4} | raw {m_raw['ann']:+8.1%} "
              f"mr {m_mr['ann']:+8.1%} | yrs: " +
              " ".join(f"{y}:{v:+.0%}" for y, v in row["years"].items()) +
              f" ({row['sec']}s)", flush=True)

    json.dump(rows, open("logs/r6_10_h_time.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1, default=str)
    print("[done] logs/r6_10_h_time.json", flush=True)


if __name__ == "__main__":
    main()
