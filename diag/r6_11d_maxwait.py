# -*- coding: utf-8 -*-
"""R6-11d · 冻结口径下 max_wait 重扫（2026-08-26 用户指令）。

假设：长尾挂单（等待期 p95=26 日）冻结 7.5% 资金近月——缩短 max_wait 释放
冻结更快，冻结口径年化可能反转回升。受控：B3 冠军 × 4×7.5% × 整手+min5
+ freeze_pending（全摩擦口径），仅变 max_wait。
产物 logs/r6_11d_maxwait.json。
"""
import json
import os
import sys
import time
from copy import deepcopy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

LEVELS = [8, 12, 15, 20, 27]


def main():
    from discovery.snapshot import freeze
    from discovery.split import holdout_split
    from discovery.objective import run_full_scan, portfolio_metrics
    from backtest.models import PositionModel

    st = json.load(open("logs/r6_10_loop/state.json", encoding="utf-8"))
    base = st["base"]
    universe, _ = freeze("2021-01-01")
    split = holdout_split()
    udates = next(iter(universe.values())).index
    pm = lambda: PositionModel(capital=1_000_000, lot_size=100, min_fee=5.0,
                               max_positions=4, pos_cap=0.075, freeze_pending=True)

    rows = {}
    for mw in LEVELS:
        p = {**deepcopy(base), "max_wait": mw}
        t0 = time.time()
        filled = run_full_scan(p, universe)
        outer = [r for r in filled if split.outer.covers(pd.Timestamp(r["signal_date"]))]
        m = portfolio_metrics(outer, split.outer, udates, position_model=pm())
        rows[mw] = {"n_outer": len(outer), "taken": m["n_taken"],
                    "taken_per_day": round(m["n_taken"] / 156, 2),
                    "ann": round(m["ann"], 3), "win": round(m.get("win_rate", 0), 3)}
        print(f"[mw={mw:>2}] 信号={len(outer):>5} taken={m['n_taken']:>4}"
              f"({rows[mw]['taken_per_day']:>4.2f}/日) ann={m['ann']:+9.1%} "
              f"胜率={m.get('win_rate', 0):.0%} ({time.time()-t0:.0f}s)", flush=True)

    json.dump(rows, open("logs/r6_11d_maxwait.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1, default=str)
    print("[done] logs/r6_11d_maxwait.json", flush=True)


if __name__ == "__main__":
    main()
