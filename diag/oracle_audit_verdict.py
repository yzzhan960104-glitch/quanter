# -*- coding: utf-8 -*-
"""oracle 污染审计 · 实盘口径三语料对比(2026-09-06)。

历史决策重判(全部在实盘口径下:amihud keep-top5 + PM 20×5% 冻结 + 随机 21 种子):
  D1 B3 vs r68 —— B3 的冠军晋升(oracle 闸 G2 5.74>5.14)在实盘口径下是否维持
  D2 B3+ts8 vs B3 —— 已 PUBLISHED 提案 p_60faf2e4 的实盘口径重验
同时报 oracle 口径对照(同语料 exit_date 序),量化各决策点的虚高幅度。
"""
from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import numpy as np
import pandas as pd

from backtest.models import PositionModel
from discovery.objective import portfolio_metrics
from discovery.split import Segment

CORPORA = {
    "r68(换代前)": "diag/r68_regime_trades.parquet",
    "B3(部署)": "diag/neckline_regime_trades.parquet",
    "B3+ts8(提案)": "diag/b3ts8_regime_trades.parquet",
}
idx = pd.read_parquet("data_lake/index_daily.parquet", columns=["close"])
cal = idx.xs("000300.SH", level="symbol").index.sort_values()
lake = pd.read_parquet("data_lake/a_shares_daily.parquet",
                       filters=[("date", ">=", pd.Timestamp("2019-06-01"))],
                       columns=["close", "amount"])


def amihud_for(tr):
    syms = set(tr["symbol"].unique())
    lk = lake[lake.index.get_level_values("symbol").isin(syms)]
    ret = lk["close"].groupby(level="symbol").pct_change()
    am = (ret.abs() / (lk["amount"].astype("float32") + 1.0)) \
        .groupby(level="symbol").rolling(60, min_periods=48).mean()
    am = am.reset_index(level=0, drop=True).sort_index()
    KEY = pd.MultiIndex.from_arrays([pd.to_datetime(tr["formed_at"]), tr["symbol"]])
    return am.reindex(KEY).values


def keep_top5(g):
    m = pd.Series(True, index=g.index)
    if len(g) <= 5:
        return m
    valid = g.dropna(subset=["amihud60"])
    if len(valid) < 5:
        return m
    keep = valid.sort_values("amihud60", ascending=False).head(5).index
    m.loc[g.index.difference(keep)] = False
    return m


PM = PositionModel(capital=200_000.0, pos_cap=0.05, max_positions=20,
                   lot_size=0, min_fee=0.0, freeze_pending=True)
full = Segment("full", pd.Timestamp("2021-01-01").date(), pd.Timestamp("2026-09-04").date())
years = {y: Segment(f"y{y}", pd.Timestamp(f"{y}-01-01").date(), pd.Timestamp(f"{y}-12-31").date())
         for y in range(2021, 2027)}


def rows(sub):
    return [{"symbol": s, "signal_date": pd.Timestamp(d), "buy_date": bd, "exit_date": ed,
             "avg_pnl_pct": float(p), "entry": None, "priority": None, "pos_cap": None}
            for s, d, bd, ed, p in zip(sub["symbol"].values, pd.to_datetime(sub["formed_at"]).values,
                                       sub["entry_date"].values, sub["exit_date"].values,
                                       sub["avg_pnl_pct"].values)]


print(f"{'语料':<14}{'实盘ann[min/中/max]':>24}{'实盘dd':>22}{'oracle ann':>11} | 分年实盘中位 21→26")
res = {}
for name, path in CORPORA.items():
    tr = pd.read_parquet(path)
    tr["formed_at"] = pd.to_datetime(tr["formed_at"])
    tr["amihud60"] = amihud_for(tr)
    sel = tr.groupby("formed_at", group_keys=False).apply(keep_top5)
    trades = rows(tr[sel])
    ms = [portfolio_metrics(trades, full, cal,
                            position_model=replace(PM, queue_order="random", queue_seed=s))
          for s in range(21)]
    an = np.array([m["ann"] for m in ms]) * 100
    dd = np.array([m["max_dd"] for m in ms]) * 100
    orc = portfolio_metrics(trades, full, cal, position_model=replace(PM, queue_order="exit_date"))
    ys = [np.median([portfolio_metrics(trades, seg, cal, position_model=replace(
        PM, queue_order="random", queue_seed=s))["ann"] for s in range(11)]) * 100
        for seg in years.values()]
    res[name] = (an, dd, ys)
    print(f"{name:<14}{an.min():>+7.1f}/{np.median(an):>+7.1f}/{an.max():>+7.1f}%"
          f"{dd.min():>+8.1f}/{np.median(dd):>+6.1f}/{dd.max():>+6.1f}%"
          f"{orc['ann']*100:>+10.1f}% | " + " ".join(f"{a:+5.1f}" for a in ys), flush=True)

print("\n== 判读 ==")
b3a = np.median(res["B3(部署)"][0]); r68a = np.median(res["r68(换代前)"][0])
ts8a = np.median(res["B3+ts8(提案)"][0])
print(f"D1 晋升重判: B3 {b3a:+.1f}% vs r68 {r68a:+.1f}% → {'维持' if b3a > r68a else '推翻(oracle 假象!)'}(差 {b3a-r68a:+.1f}pp)")
print(f"D2 ts8 重验: B3+ts8 {ts8a:+.1f}% vs B3 {b3a:+.1f}% → {'维持' if ts8a > b3a - 0.5 else '不维持'}(差 {ts8a-b3a:+.1f}pp)")
