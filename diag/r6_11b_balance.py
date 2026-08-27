# -*- coding: utf-8 -*-
"""R6-11b · 收益-信号量平衡受控（2026-08-26 用户裁决：100 万基准，不能有信号就买）。

双轴：
  轴 A 信号层闸：cooldown/min_rr/supp——把日信号从 ~70 压到 5-30；
  轴 B 组合结构：max_positions×pos_cap（并发↓单笔↑，资金利用率持衡 ~30%）。
口径：100 万 + 整手(100股)+min5（R6-11 有限资金模式）。输出 outer 年化 /
成交密度 / 信号密度 / 回撤——找「信号个位数-两位数/日 且 年化可守」的平衡点。
产物 logs/r6_11b_balance.json。
"""
import json
import os
import sys
import time
from copy import deepcopy
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

CAP = 1_000_000.0


def main():
    from discovery.snapshot import freeze
    from discovery.split import holdout_split, Segment
    from discovery.objective import run_full_scan, portfolio_metrics
    from backtest.models import PositionModel

    st = json.load(open("logs/r6_10_loop/state.json", encoding="utf-8"))
    base = st["base"]
    universe, _ = freeze("2021-01-01")
    split = holdout_split()
    udates = next(iter(universe.values)).index if False else next(iter(universe.values())).index
    y_segs = {y: Segment(f"y{y}", date(y, 1, 1), date(y, 12, 31))
              for y in (2022, 2023, 2024, 2025, 2026)}

    pm_lot = lambda mp, pc: PositionModel(capital=CAP, lot_size=100, min_fee=5.0,
                                          max_positions=mp, pos_cap=pc)

    # ── 轴 A：信号层闸（每变体重扫）──
    sig_variants = [
        ("champion(cd=0)", {}),
        ("cd=3", {"cooldown": 3}),
        ("cd=5", {"cooldown": 5}),
        ("cd=8", {"cooldown": 8}),
        ("min_rr=2.5", {"min_rr": 2.5}),
        ("supp=0.4", {"min_suppression": 0.4}),
    ]
    results = {}
    caches = {}
    for tag, ov in sig_variants:
        p = {**deepcopy(base), **ov}
        t0 = time.time()
        filled = run_full_scan(p, universe)
        outer = [r for r in filled if split.outer.covers(pd.Timestamp(r["signal_date"]))]
        m = portfolio_metrics(outer, split.outer, udates, position_model=pm_lot(6, 0.05))
        yrs = {y: round(portfolio_metrics(
            [r for r in filled if seg.covers(pd.Timestamp(r["signal_date"]))],
            seg, udates, position_model=pm_lot(6, 0.05))["ann"], 3)
            for y, seg in y_segs.items()}
        row = {"sig_per_day": round(len(outer) / 156, 1),
               "taken": m["n_taken"], "taken_per_day": round(m["n_taken"] / 156, 2),
               "ann": round(m["ann"], 3), "max_dd": m["max_dd"],
               "win": round(m.get("win_rate", 0), 3), "years": yrs}
        results[tag] = row
        caches[tag] = outer
        print(f"[{tag:>14}] 信号={row['sig_per_day']:>5.1f}/日 taken={m['n_taken']:>4}"
              f"({row['taken_per_day']:>4.1f}/日) ann={m['ann']:+8.1%} dd={m['max_dd']:.0%} | "
              + " ".join(f"{y}:{v:+.0%}" for y, v in yrs.items())
              + f" ({time.time()-t0:.0f}s)", flush=True)

    # ── 轴 B：组合结构（champion 信号免费重排）──
    print("\n=== 轴 B：组合结构（champion 信号，整手+min5@100w）===", flush=True)
    outer = caches["champion(cd=0)"]
    for mp, pc in ((6, 0.05), (4, 0.075), (3, 0.10), (2, 0.15), (1, 0.25)):
        m = portfolio_metrics(outer, split.outer, udates, position_model=pm_lot(mp, pc))
        results[f"struct({mp},{pc})"] = {
            "taken": m["n_taken"], "taken_per_day": round(m["n_taken"] / 156, 2),
            "ann": round(m["ann"], 3), "max_dd": m["max_dd"]}
        print(f"[{mp}并发×{pc:.0%}/笔] taken={m['n_taken']:>4}"
              f"({results[f'struct({mp},{pc})']['taken_per_day']:>4.1f}/日) "
              f"ann={m['ann']:+8.1%} dd={m['max_dd']:.0%}", flush=True)

    json.dump(results, open("logs/r6_11b_balance.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1, default=str)
    print("[done] logs/r6_11b_balance.json", flush=True)


if __name__ == "__main__":
    main()
