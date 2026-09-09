# -*- coding: utf-8 -*-
"""重审判语料:B3 六年全字段 + 全部干预标签(2026-09-06)。"""
from __future__ import annotations
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import numpy as np
import pandas as pd

from backtest.replay import replay
from discovery.snapshot import freeze, load_universe
from experiment.resolver import resolve_champion
from strategies.neckline.strategy import NecklineMethodStrategy

params = dict(resolve_champion().params)
frozen, _ = freeze("2021-01-01")
uni = {s: df for s, df in load_universe(start="2020-01-01").items() if s in frozen}
rep = replay(dict(uni), NecklineMethodStrategy(cfg_override=params),
             "2021-01-01", "2026-09-04")
rows = [{k: t[k] for k in ("symbol", "formed_at", "entry_date", "entry_price",
                           "exit_date", "exit_price", "exit_reason", "rr",
                           "holding_bars", "avg_pnl_pct", "neckline", "bottom", "atr")}
        for t in rep.trades]
tr = pd.DataFrame(rows)
tr["formed_at"] = pd.to_datetime(tr["formed_at"])
print(f"corpus n={len(tr)}", flush=True)

# ── 补标签:溢价 / 突破日量比 / 20日动量 ──
syms = set(tr["symbol"].unique())
lake = pd.read_parquet("data_lake/a_shares_daily.parquet",
                       filters=[("date", ">=", pd.Timestamp("2020-06-01"))],
                       columns=["close", "volume"])
lake = lake[lake.index.get_level_values("symbol").isin(syms)]
KEY = pd.MultiIndex.from_arrays([tr["formed_at"], tr["symbol"]], names=["date", "symbol"])
feats = {}
for sym, g in lake.groupby(level="symbol", sort=False):
    g = g.droplevel("symbol").sort_index()
    c, v = g["close"], g["volume"]
    vr = v / v.rolling(20, min_periods=10).mean()
    m20 = c / c.shift(20) - 1.0
    feats[sym] = (g.index, vr.values, m20.values)
idx_arr, vr_arr, m20_arr = [], [], []
for d, s in zip(tr["formed_at"], tr["symbol"]):
    ix, vr, m20 = feats[s]
    pos = ix.searchsorted(pd.Timestamp(d))
    idx_arr.append(pos)
    vr_arr.append(vr[pos] if pos < len(vr) else np.nan)
    m20_arr.append(m20[pos] if pos < len(m20) else np.nan)
tr["volr20"] = vr_arr
tr["mom20"] = m20_arr
tr["premium"] = (tr["entry_price"] - tr["neckline"]) / tr["atr"]

# ── 合并既有标签 ──
zoo = pd.read_parquet("diag/factor_zoo_heat.parquet")
for col in ("heat_g",):
    tr[col] = zoo[col].reindex(tr.index).values
for src, cols in (("diag/ma_struct_trades_tagged.parquet", ("ord_score", "neck_pos", "width_conv")),
                  ("diag/sector_trades_tagged.parquet", ("sec_r20",)),
                  ("diag/macd_trades_tagged.parquet", ("cross_age",))):
    d = pd.read_parquet(src)
    d["formed_at"] = pd.to_datetime(d["formed_at"])
    tr = tr.merge(d[["symbol", "formed_at", *cols]].drop_duplicates(["symbol", "formed_at"]),
                  on=["symbol", "formed_at"], how="left")
idx = pd.read_parquet("data_lake/index_daily.parquet", columns=["close"])
cyb = idx.xs("399006.SZ", level="symbol")["close"].sort_index()
ratio = (cyb / cyb.rolling(60).mean()).reindex(tr["formed_at"]).values
tr["cyb_ratio"] = ratio

# 当日内热度分位(决策时合法:同日截面)
tr["heat_day"] = tr.groupby("formed_at")["heat_g"].rank(pct=True)
tr.to_parquet("diag/retrial_corpus.parquet")
print(f"saved diag/retrial_corpus.parquet {tr.shape}")
print(tr[["premium", "volr20", "mom20", "heat_g", "sec_r20", "cross_age", "cyb_ratio"]].isna().mean().round(3).to_string())
print(f"premium 分位: {tr['premium'].quantile([.25,.5,.75]).round(2).to_dict()}")
