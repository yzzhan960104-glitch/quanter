# -*- coding: utf-8 -*-
"""颈线法×市场状态 全期语料构建(2026-09-05 原理探索)。

B3 参数,2021 冻结符号集(升格 gate 同池 1202 只)+ 2020 起数据暖机,
replay 2021-01-01 → 2026-09-04,逐笔落 diag/neckline_regime_trades.parquet。
"""
from __future__ import annotations
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import pandas as pd

from backtest.replay import replay
from discovery.snapshot import freeze, load_universe
from experiment.resolver import resolve_champion
from strategies.neckline.strategy import NecklineMethodStrategy

params = dict(resolve_champion().params)
frozen, meta = freeze("2021-01-01")
print(f"frozen syms={meta.universe_count}", flush=True)
uni = {s: df for s, df in load_universe(start="2020-01-01").items() if s in frozen}
print(f"warmup universe: {len(uni)}", flush=True)

rep = replay(uni, NecklineMethodStrategy(cfg_override=params),
             "2021-01-01", "2026-09-04")
rows = [{k: t[k] for k in ("symbol", "formed_at", "entry_date", "exit_date",
                           "exit_reason", "rr", "holding_bars", "avg_pnl_pct",
                           "neckline", "bottom", "atr")} for t in rep.trades]
tr = pd.DataFrame(rows)
tr["formed_at"] = pd.to_datetime(tr["formed_at"])
tr.to_parquet("diag/neckline_regime_trades.parquet")
tr["ym"] = tr["formed_at"].dt.strftime("%Y-%m")
yr = tr.groupby(tr["formed_at"].dt.year)["avg_pnl_pct"]
print(f"\ntrades={len(tr)} 全期均笔{tr.avg_pnl_pct.mean():+.2f}% 胜率{(tr.avg_pnl_pct>0).mean()*100:.1f}%")
print(pd.DataFrame({"n": yr.size(), "win%": (tr.assign(w=tr.avg_pnl_pct>0).groupby(tr.formed_at.dt.year)["w"].mean()*100).round(1),
                    "avg%": yr.mean().round(2)}).to_string())
print("saved diag/neckline_regime_trades.parquet", flush=True)
