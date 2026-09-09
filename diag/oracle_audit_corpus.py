# -*- coding: utf-8 -*-
"""oracle 污染审计 · 语料构建(2026-09-06)。

两个历史决策的实盘口径重判所需语料:
  A. r68(换代前部署冠军,git aa101850 的 params_snapshot)——判「B3 晋升是否 oracle 假象」
  B. B3+time_stop_days=8——判「已 PUBLISHED 提案 p_60faf2e4(ts8)是否 oracle 假象」
同底座:2021 冻结池 + 2020 暖机,replay 2021-01-01→2026-09-04。
"""
from __future__ import annotations

import json
import subprocess
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

# r68 参数:换代前一版 params_snapshot(00c2edc9 的父链)
r68_raw = subprocess.run(
    ["git", "show", "aa101850:emquant/config/params_snapshot.json"],
    capture_output=True, text=True, encoding="utf-8", cwd=str(ROOT)).stdout
snap = json.loads(r68_raw)
r68_params = {**snap["id_params"], **snap["exec_params"]}
b3_params = dict(resolve_champion().params)
ts8_params = {**b3_params, "time_stop_days": 8}
print("r68 5键差异核对:", {k: r68_params.get(k) for k in
      ("min_suppression", "max_h_atr", "decay_tau", "tp_adapt_h_atr", "chase_entry")}, flush=True)

frozen, _ = freeze("2021-01-01")
uni = {s: df for s, df in load_universe(start="2020-01-01").items() if s in frozen}
print(f"universe: {len(uni)}", flush=True)

for name, params, out in (("r68", r68_params, "diag/r68_regime_trades.parquet"),
                          ("b3ts8", ts8_params, "diag/b3ts8_regime_trades.parquet")):
    rep = replay(dict(uni), NecklineMethodStrategy(cfg_override=params),
                 "2021-01-01", "2026-09-04")
    rows = [{k: t[k] for k in ("symbol", "formed_at", "entry_date", "exit_date",
                               "exit_reason", "rr", "holding_bars", "avg_pnl_pct",
                               "neckline", "atr")} for t in rep.trades]
    tr = pd.DataFrame(rows)
    tr["formed_at"] = pd.to_datetime(tr["formed_at"])
    tr.to_parquet(out)
    yr = tr.groupby(tr.formed_at.dt.year)["avg_pnl_pct"]
    print(f"[{name}] n={len(tr)} 全期{tr.avg_pnl_pct.mean():+.2f}% 胜率{(tr.avg_pnl_pct>0).mean()*100:.1f}% "
          f"ts出口占比{(tr.exit_reason=='time_stop').mean()*100:.1f}%", flush=True)
    print(pd.DataFrame({"n": yr.size(), "avg%": yr.mean().round(2)}).to_string(), flush=True)
