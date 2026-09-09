# -*- coding: utf-8 -*-
"""实验腿(r10leg 变体)六年语料:其 ID/EXEC 参数喂标准策略(近似等价,注记)。"""
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
from strategies.neckline.strategy import NecklineMethodStrategy

ID = {'breakout_vol_mult': 1.0, 'decay_tau': 30, 'local_extrema_window': 2, 'max_h_atr': 4.0,
      'min_bottoms': 3, 'min_rr': 2.0, 'min_suppression': 0.05, 'min_touches': 2,
      'momentum_gate': 0.0, 'stop_atr_mult': 1.5, 'tp_h_mult': 1.2, 'window': 60}
EXEC = {'buy_limit_atr_mult': 3.0, 'cancel_thresh_mult': None, 'chase_entry': True,
        'commission_rate': 0.0003, 'cooldown': 1, 'max_holding': 15, 'max_wait': 20,
        'stamp_rate': 0.0005, 'time_stop_days': 5, 'timeout_extend_days': 3,
        'timeout_extend_min_pnl': 0.05, 'tp1_h_mult': 1.5, 'tp1_portion': 0.9,
        'tp_adapt_h_atr': None, 'tp_adapt_scale': 0.8, 'trailing_floor': 0.0,
        'trailing_grace': 0, 'trailing_step': 0.05, 'transfer_rate': 1e-05}
params = {**ID, **EXEC}
frozen, _ = freeze("2021-01-01")
uni = {s: df for s, df in load_universe(start="2020-01-01").items() if s in frozen}
rep = replay(dict(uni), NecklineMethodStrategy(cfg_override=params), "2021-01-01", "2026-09-04")
rows = [{k: t[k] for k in ("symbol", "formed_at", "entry_date", "entry_price", "exit_date",
                           "exit_reason", "rr", "avg_pnl_pct", "neckline", "atr")} for t in rep.trades]
tr = pd.DataFrame(rows)
tr["formed_at"] = pd.to_datetime(tr["formed_at"])
tr.to_parquet("diag/exp_regime_trades.parquet")
print(f"exp corpus n={len(tr)} 六年{tr.avg_pnl_pct.mean():+.2f}% 胜率{(tr.avg_pnl_pct>0).mean()*100:.1f}%")
yr = tr.groupby(tr.formed_at.dt.year)["avg_pnl_pct"]
print(pd.DataFrame({"n": yr.size(), "avg%": yr.mean().round(2)}).to_string())
