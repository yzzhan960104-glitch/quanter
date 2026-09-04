# -*- coding: utf-8 -*-
"""通用并行批扫描（2026-09-04 深夜优化马拉松·批①执行层）。

复用 momentum_exhaustion_sweep 的 _cell 并行模式；网格内联（不依赖 /tmp）。
"""
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

# 批①执行层（当前值：grace10/step0.05/max_holding30/time_stop0）
GRID = [
    ("__o_decay30", "outer"), ("__o_ts8", "outer"),
    ("__o_supp35", "outer"), ("__o_minrr225", "outer"),
    ("__o_grace5", "outer"), ("__c2__decay30_ts8", "outer"),
]


def _cell(job):
    key, val = job
    from discovery.objective import evaluate_replay
    from discovery.snapshot import freeze
    from discovery.split import holdout_split
    from experiment.resolver import resolve_champion
    base = dict(resolve_champion().params)
    OVERRIDES = {
        "__o_decay30": {"decay_tau": 30.0},
        "__o_ts8": {"time_stop_days": 8.0},
        "__o_supp35": {"min_suppression": 0.35},
        "__o_minrr225": {"min_rr": 2.25},
        "__o_grace5": {"trailing_grace": 5.0},
        "__c2__decay30_ts8": {"decay_tau": 30.0, "time_stop_days": 8.0},
    }
    seg = "inner"
    if key.startswith("__o_"):
        seg = "outer"
    ov = OVERRIDES.get(key, {})
    if key.startswith("__"):
        key = key.strip("_")
    val = key[:12]
    base.update(ov)
    split = holdout_split()
    universe, _ = freeze("2025-01-01")
    r = split.inner if seg == "inner" else split.outer
    m = evaluate_replay(base, universe, split,
                        start=str(r.start), end=str(r.end)).get("inner") or {}
    if seg == "outer":
        key = "OUTER " + key
    return {"key": key, "val": val,
            **{k: m.get(k) for k in ("n_hits", "win_rate", "avg_rr",
                                     "annualized_return", "max_drawdown")}}


if __name__ == "__main__":
    t0 = datetime.now()
    with ProcessPoolExecutor(3) as ex:
        cells = list(ex.map(_cell, GRID))
    for c in cells:
        print(f"{c['key']}={c['val']:<5} n={c['n_hits']:>6} "
              f"胜率{c['win_rate'] or 0:>6.1%} 均rr{c['avg_rr'] or 0:>6.3f} "
              f"年化{c['annualized_return'] or 0:>7.1%} dd{c['max_drawdown'] or 0:>6.1%}")
    out = ROOT / "diag" / f"batch_sweep_{t0:%H%M%S}.json"
    out.write_text(json.dumps({"launched": f"{t0:%H:%M:%S}", "cells": cells},
                              default=str, ensure_ascii=False), encoding="utf-8")
    print(f"[done] {out} ({(datetime.now() - t0).total_seconds():.0f}s)")
