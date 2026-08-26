# -*- coding: utf-8 -*-
"""R6-11 · 有限资金敏感性（2026-08-26 用户指令：10 万资金能否同样收益）。

B3 冠军 × capital {100k, 300k, 1M, 10M} × {百分比模式, 整手+min5 模式}
→ outer 年化对比。整手模式揭示三个小资金杀手：高价股买不起一手（跳过）、
低价股取整损耗、佣金 min5 固定税。
产物 logs/r6_11_capital.json。
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

    # 高价股分布（一眼看出 10 万预算的覆盖面）
    prices = sorted(float(r.get("entry") or 0) for r in outer if r.get("entry"))
    import statistics
    print(f"[入场价分布] p25={prices[len(prices)//4]:.1f} 中位={prices[len(prices)//2]:.1f} "
          f"p75={prices[3*len(prices)//4]:.1f} p95={prices[int(0.95*len(prices))]:.1f} "
          f"max={prices[-1]:.1f}", flush=True)

    rows = {}
    for cap in (100_000, 300_000, 1_000_000, 10_000_000):
        for mode, pm in (
            ("pct", PositionModel(capital=cap, lot_size=0)),
            ("lot", PositionModel(capital=cap, lot_size=100, min_fee=5.0)),
        ):
            m = portfolio_metrics(outer, split.outer, udates, position_model=pm)
            key = f"{cap//10000}w_{mode}"
            rows[key] = {"cap": cap, "mode": mode,
                         "n_taken": m["n_taken"], "ann": round(m["ann"], 3),
                         "equity_end": round(m["equity_end"], 4),
                         "win": round(m.get("win_rate", 0), 3)}
            print(f"[{key:>12}] taken={m['n_taken']:>5} ann={m['ann']:+9.1%} "
                  f"eq_end={m['equity_end']:.3f} 胜率={m.get('win_rate', 0):.0%}",
                  flush=True)

    json.dump(rows, open("logs/r6_11_capital.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1, default=str)
    print("[done] logs/r6_11_capital.json", flush=True)


if __name__ == "__main__":
    main()
