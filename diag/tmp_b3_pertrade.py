# -*- coding: utf-8 -*-
"""一次性:B3(部署冠军) holdout inner/outer 逐笔 avg_pnl_pct 统计(与 r68_vs_b3_holdout 同口径,保留 trades)。"""
import statistics as st
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
from backtest.replay import replay
from discovery.snapshot import freeze
from discovery.split import holdout_split
from experiment.resolver import resolve_champion
from strategies.neckline.strategy import NecklineMethodStrategy

params = dict(resolve_champion().params)
universe, _ = freeze("2025-01-01")
split = holdout_split()
for name in ("inner", "outer"):
    r = split.inner if name == "inner" else split.outer
    rep = replay(universe, NecklineMethodStrategy(cfg_override=params),
                 str(r.start), str(r.end))
    pn = [t["avg_pnl_pct"] for t in rep.trades]
    by = {}
    for t in rep.trades:
        by.setdefault(t["exit_reason"], []).append(t["avg_pnl_pct"])
    print(f"[{name}] {r.start}~{r.end} n={len(pn)} "
          f"win_rate={sum(1 for x in pn if x > 0)/len(pn)*100:.1f}% "
          f"avg_rr={rep.avg_rr:.4f} 均笔={st.mean(pn):+.3f}% 中位={st.median(pn):+.3f}%",
          flush=True)
    for k, v in sorted(by.items(), key=lambda kv: -len(kv[1])):
        print(f"    {k:<12} {len(v):>6} 均笔 {st.mean(v):+8.3f}%  占比 {len(v)/len(pn)*100:.0f}%",
              flush=True)
