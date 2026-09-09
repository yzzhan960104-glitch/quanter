# -*- coding: utf-8 -*-
"""r68(部署) vs r610_b3(账面) holdout 同口径直接对比。"""
from __future__ import annotations
import json, sys
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
GRID = [("r68", "inner"), ("r68", "outer"), ("b3", "inner"), ("b3", "outer")]


def _cell(job):
    variant, seg = job
    import subprocess
    from discovery.objective import evaluate_replay
    from discovery.snapshot import freeze
    from discovery.split import holdout_split
    from experiment.resolver import resolve_champion
    if variant == "b3":
        params = dict(resolve_champion().params)
    else:
        head = subprocess.run(
            ["git", "show", "HEAD:emquant/config/params_snapshot.json"],
            capture_output=True, text=True, encoding="utf-8").stdout
        s = json.loads(head)
        params = {**s["id_params"], **s["exec_params"]}
    split = holdout_split()
    universe, _ = freeze("2025-01-01")
    r = split.inner if seg == "inner" else split.outer
    m = evaluate_replay(params, universe, split,
                        start=str(r.start), end=str(r.end)).get("inner") or {}
    return {"variant": variant, "seg": seg,
            **{k: m.get(k) for k in ("n_hits", "win_rate", "avg_rr",
                                     "annualized_return", "max_drawdown")}}


if __name__ == "__main__":
    t0 = datetime.now()
    with ProcessPoolExecutor(4) as ex:
        cells = list(ex.map(_cell, GRID))
    for c in cells:
        print(f"{c['variant']:<4} {c['seg']:<5} n={c['n_hits']:>6} "
              f"胜率{c['win_rate'] or 0:>6.1%} 均rr{c['avg_rr'] or 0:>6.3f} "
              f"年化{c['annualized_return'] or 0:>7.1%} dd{c['max_drawdown'] or 0:>6.1%}")
    out = ROOT / "diag" / f"r68_vs_b3_holdout_{t0:%Y%m%d}.json"
    out.write_text(json.dumps(cells, default=str, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print(f"[done] ({(datetime.now() - t0).total_seconds():.0f}s)")
