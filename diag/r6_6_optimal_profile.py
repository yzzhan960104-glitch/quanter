# -*- coding: utf-8 -*-
"""R6-6 当前最优参数配置画像（2026-08-26，修复后引擎）。

候选：贪心栈 base / base+tp1_h_mult=2.0（R6-4 真值刷新的新单维最优）/
supp04（R5 支配锚）/ supp04+tp1=2.0（叠加候选）。四配置全画像：
outer/inner 双口径（raw+模拟线）+ 逐年 raw ann——回答「当前最优配置」的
可守版本（采纳口径：outer_raw ↑ ∧ 模拟线不降 ∧ 2022 不恶化 ∧ 无年段恶化>2pp，
与 R6-5 同款预登记）。

产物：logs/r6_6_optimal_profile.json。
"""
import json
import os
import sys
import time
from copy import deepcopy
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

YEARS = [2022, 2023, 2024, 2025, 2026]


def main():
    from discovery.snapshot import freeze
    from discovery.split import holdout_split, Segment
    from discovery.objective import evaluate_portfolio, run_full_scan, portfolio_metrics
    from discovery.manual_risk_sim import build_block_calendar

    base = json.loads(open("logs/r4_loop/state.json", encoding="utf-8").read())["base"]
    universe, meta = freeze("2021-01-01")
    split = holdout_split()
    cal = build_block_calendar(universe)
    universe_dates = next(iter(universe.values())).index
    year_segs = {y: Segment(f"y{y}", date(y, 1, 1), date(y, 12, 31)) for y in YEARS}

    cands = [
        ("greedy(83.5%)", deepcopy(base)),
        ("greedy+tp1=2.0", {**deepcopy(base), "tp1_h_mult": 2.0}),
        ("supp04(89.6%)", {**deepcopy(base), "min_suppression": 0.4}),
        ("supp04+tp1=2.0", {**deepcopy(base), "min_suppression": 0.4, "tp1_h_mult": 2.0}),
    ]
    rows = {}
    for tag, p in cands:
        t0 = time.time()
        ev = evaluate_portfolio(p, universe, split, block_dates=cal)
        filled = run_full_scan(p, universe)
        row = {"outer_raw": ev["outer_raw"]["ann"], "outer_mr": ev["outer"]["ann"],
               "inner_raw": ev.get("inner_raw", ev["inner"])["ann"],
               "inner_mr": ev["inner"]["ann"], "min_yr": ev["inner"]["min_yearly_calmar"],
               "n": ev["n_total"],
               "years": {y: portfolio_metrics(filled, seg, universe_dates)["ann"]
                         for y, seg in year_segs.items()},
               "sec": round(time.time() - t0, 1)}
        rows[tag] = row
        ytxt = " ".join(f"y{y}:{row['years'][y]:+.0%}" for y in YEARS)
        print(f"[{tag:>16}] outer_raw {row['outer_raw']:+.1%} mr {row['outer_mr']:+.1%} "
              f"inner_raw {row['inner_raw']:+.1%} | {ytxt} | n={row['n']} {row['sec']}s",
              flush=True)

    json.dump({"candidates": rows}, open("logs/r6_6_optimal_profile.json", "w",
                                         encoding="utf-8"),
              ensure_ascii=False, indent=1, default=str)
    print("[done] logs/r6_6_optimal_profile.json", flush=True)


if __name__ == "__main__":
    main()
