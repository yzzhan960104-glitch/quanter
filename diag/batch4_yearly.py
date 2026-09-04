# -*- coding: utf-8 -*-
"""ts8 多年窗口稳健性（马拉松批4）：2021-2024 逐年 基线 vs time_stop=8。"""
from __future__ import annotations

import json
import sys
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

GRID = [(v, y) for y in (2021, 2022, 2023, 2024) for v in ("base", "ts8")]


def _cell(job):
    variant, year = job
    from discovery.objective import evaluate_replay
    from discovery.snapshot import freeze
    from discovery.split import holdout_split
    from experiment.resolver import resolve_champion
    base = dict(resolve_champion().params)
    if variant == "ts8":
        base["time_stop_days"] = 8.0
    split = holdout_split()
    universe, _ = freeze("2021-01-01")
    m = evaluate_replay(base, universe, split,
                        start=f"{year}-01-01", end=f"{year}-12-31").get("inner") or {}
    return {"year": year, "variant": variant,
            **{k: m.get(k) for k in ("n_hits", "win_rate", "avg_rr",
                                     "annualized_return", "max_drawdown")}}


if __name__ == "__main__":
    t0 = datetime.now()
    with ProcessPoolExecutor(3) as ex:
        cells = list(ex.map(_cell, GRID))
    for c in cells:
        print(f"{c['year']} {c['variant']:<4} n={c['n_hits']:>6} "
              f"胜率{c['win_rate'] or 0:>6.1%} 均rr{c['avg_rr'] or 0:>6.3f} "
              f"年化{c['annualized_return'] or 0:>7.1%} dd{c['max_drawdown'] or 0:>6.1%}")
    out = ROOT / "diag" / f"yearly_ts8_{t0:%Y%m%d}.json"
    out.write_text(json.dumps(cells, default=str, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print(f"[done] {out} ({(datetime.now() - t0).total_seconds():.0f}s)")
