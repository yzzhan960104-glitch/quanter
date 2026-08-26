# -*- coding: utf-8 -*-
"""R6-11c · 挂单冻结资金实测（2026-08-26 用户指令）。

B3 冠军 × 4×7.5% × 整手+min5@100w × freeze_pending on/off
——占用起点从成交日提前到信号次日，Little's law 节流自动生效。
产物 logs/r6_11c_freeze.json。
"""
import json
import os
import sys
import time
from copy import deepcopy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd


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

    t0 = time.time()
    filled = run_full_scan(base, universe)
    outer = [r for r in filled if split.outer.covers(pd.Timestamp(r["signal_date"]))]
    print(f"[scan] n_outer={len(outer)} ({time.time()-t0:.0f}s)", flush=True)

    # 等待期分布（signal→buy）
    waits = []
    for r in outer:
        try:
            w = (pd.Timestamp(r["buy_date"]) - pd.Timestamp(r["signal_date"])).days
            waits.append(max(w, 0))
        except Exception:
            pass
    waits.sort()
    print(f"[等待期] 中位={waits[len(waits)//2]}日 p75={waits[3*len(waits)//4]}日 "
          f"p95={waits[int(0.95*len(waits))]}日", flush=True)

    rows = {}
    for tag, pm in (
        ("off", PositionModel(capital=1_000_000, lot_size=100, min_fee=5.0,
                              max_positions=4, pos_cap=0.075)),
        ("on", PositionModel(capital=1_000_000, lot_size=100, min_fee=5.0,
                             max_positions=4, pos_cap=0.075, freeze_pending=True)),
    ):
        m = portfolio_metrics(outer, split.outer, udates, position_model=pm)
        rows[tag] = {"taken": m["n_taken"], "taken_per_day": round(m["n_taken"] / 156, 2),
                     "ann": round(m["ann"], 3), "equity_end": round(m["equity_end"], 4),
                     "win": round(m.get("win_rate", 0), 3)}
        print(f"[freeze {tag:>3}] taken={m['n_taken']:>4}({rows[tag]['taken_per_day']:>4.2f}/日) "
              f"ann={m['ann']:+9.1%} eq_end={m['equity_end']:.3f} "
              f"胜率={m.get('win_rate', 0):.0%}", flush=True)

    json.dump(rows, open("logs/r6_11c_freeze.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1, default=str)
    print("[done] logs/r6_11c_freeze.json", flush=True)


if __name__ == "__main__":
    main()
