# -*- coding: utf-8 -*-
"""口径交叉复检 v2(24h 审计 · 步骤D'):双 PM × 种子极差带。

v1 发现部署口径(4×7.5%)下「弃最弱板块Q1」翻活。v2 加严:
  PM-A 仓库标准部署口径 4×7.5%(dual_caliber_baseline 同款,冻结挂单)
  PM-B 实盘形态 20×5%(主腿 09-04 实况:19 只在仓、单票均 4.8%)
每臂报 21 种子 [min, 中位, max] 年化 + 中位 dd/calmar + 分年非劣数。
弹性仓位臂修 NaN(缺热度 → 回落 7.5%)。
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

tr = pd.read_parquet("diag/neckline_regime_trades.parquet")
Z = pd.read_parquet("diag/factor_zoo_heat.parquet")
macd = pd.read_parquet("diag/macd_trades_tagged.parquet")[["symbol", "formed_at", "cross_age"]]
sec = pd.read_parquet("diag/sector_trades_tagged.parquet")[["symbol", "formed_at", "sec_r20"]]
tr["formed_at"] = pd.to_datetime(tr["formed_at"])
for d in (macd, sec):
    d["formed_at"] = pd.to_datetime(d["formed_at"])
tr = tr.merge(macd.drop_duplicates(["symbol", "formed_at"]), on=["symbol", "formed_at"], how="left")
tr = tr.merge(sec.drop_duplicates(["symbol", "formed_at"]), on=["symbol", "formed_at"], how="left")
tr["heat_g"] = Z["heat_g"].reindex(tr.index).values
tr["hq_n"] = pd.qcut(tr["heat_g"], 5, labels=False)

idx = pd.read_parquet("data_lake/index_daily.parquet", columns=["close"])
cyb = idx.xs("399006.SZ", level="symbol")["close"].sort_index()
ratio = (cyb / cyb.rolling(60).mean()).reindex(tr["formed_at"]).values
tr["band"] = (ratio < 1.0) & (ratio >= 0.97)
tr["below"] = ratio < 1.0
cal = idx.xs("000300.SH", level="symbol").index.sort_values()

PMS = {
    "PM-A 4×7.5%": PositionModel(capital=200_000.0, pos_cap=0.075, max_positions=4,
                                lot_size=0, min_fee=0.0, freeze_pending=True),
    "PM-B 20×5%(实盘形态)": PositionModel(capital=200_000.0, pos_cap=0.05, max_positions=20,
                                      lot_size=0, min_fee=0.0, freeze_pending=True),
}


def rows(mask, pos_cap=None):
    sub = tr[mask]
    pc = None if pos_cap is None else pos_cap[mask].astype(float).values
    return [{"symbol": s, "signal_date": pd.Timestamp(d), "buy_date": bd, "exit_date": ed,
             "avg_pnl_pct": float(p), "entry": None, "priority": None,
             "pos_cap": None if pc is None else float(pc[i])}
            for i, (s, d, bd, ed, p) in enumerate(zip(
                sub["symbol"].values, sub["formed_at"].values, sub["entry_date"].values,
                sub["exit_date"].values, sub["avg_pnl_pct"].values))]


def seeds_eval(trades, seg, pm, n=21):
    ms = [portfolio_metrics(trades, seg, cal,
                            position_model=replace(pm, queue_order="random", queue_seed=s))
          for s in range(n)]
    ann = np.array([m["ann"] for m in ms])
    return {"ann_min": ann.min(), "ann_med": np.median(ann), "ann_max": ann.max(),
            "dd": float(np.median([m["max_dd"] for m in ms])),
            "calmar": float(np.median([m["calmar"] for m in ms])),
            "n_tk": float(np.median([m["n_taken"] for m in ms]))}


ALL = pd.Series(True, index=tr.index)
cap_map = tr["hq_n"].map({0: 0.10, 1: 0.10, 2: 0.075, 3: 0.075, 4: 0.03}).fillna(0.075)
ARMS = {
    "基线": (ALL, None),
    "缠线带节流": (~tr["band"], None),
    "全空头带节流": (~tr["below"], None),
    "弃死叉态": (~(tr["cross_age"].fillna(1) == 0), None),
    "弃最弱板块Q1": (~(tr["sec_r20"].fillna(0.5) < 0.25), None),
    "弃最热Q5": (~(tr["hq_n"] == 4), None),
    "弹性仓位(热度)": (ALL, cap_map),
}
full = Segment("full", pd.Timestamp("2021-01-01").date(), pd.Timestamp("2026-09-04").date())
years = {y: Segment(f"y{y}", pd.Timestamp(f"{y}-01-01").date(), pd.Timestamp(f"{y}-12-31").date())
         for y in range(2021, 2027)}

for pm_name, PM in PMS.items():
    print(f"\n======== {pm_name} ========")
    print(f"{'臂':<16}{'ann[min/中位/max]':>26}{'dd':>8}{'calmar':>7}{'n_tk':>6} | 分年中位ann 21→26 | 非劣年")
    base_years = None
    for name, (mask, pc) in ARMS.items():
        pm = replace(PM, quality_alloc=True) if pc is not None else PM
        trades = rows(mask, pc)
        r = seeds_eval(trades, full, pm)
        ys = [seeds_eval(trades, seg, pm, n=11)["ann_med"] for seg in years.values()]
        if name == "基线":
            base_years = ys
            tag = ""
        else:
            tag = f"{sum(1 for a, b in zip(ys, base_years) if a >= b - 1e-9)}/6"
        print(f"{name:<16}{r['ann_min']*100:>+7.1f}/{r['ann_med']*100:>+6.1f}/{r['ann_max']*100:>+6.1f}%"
              f"{r['dd']*100:>+7.1f}%{r['calmar']:>7.2f}{r['n_tk']:>6.0f} | "
              + " ".join(f"{a*100:+5.1f}" for a in ys) + f" | {tag}", flush=True)
