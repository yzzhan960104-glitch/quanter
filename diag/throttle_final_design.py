# -*- coding: utf-8 -*-
"""节流形态定案 + 叠加检验(24h 审计 · 步骤E · 2026-09-05)。

crosscheck2 发现:实盘形态 PM-B(20×5%)下全空头带节流 dd -51.9→-21.1%,
而昨日 DRAFT 的「只拦缠线带」不保护回撤。本步在两 PM 下比较节流三形态
并与唯一幸存的逐笔过滤(弃过热 Q5)叠加:
  T0 无节流 / T1 全空头带停新仓 / T2 分级(缠线带停、深区半仓、正常满仓)
  × {不过滤, 弃过热Q5}
指标:31 种子 ann/dd 极差带 + 中位 calmar + 分年中位 ann。
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
tr["formed_at"] = pd.to_datetime(tr["formed_at"])
tr["heat_g"] = Z["heat_g"].reindex(tr.index).values
tr["hq_n"] = pd.qcut(tr["heat_g"], 5, labels=False)

idx = pd.read_parquet("data_lake/index_daily.parquet", columns=["close"])
cyb = idx.xs("399006.SZ", level="symbol")["close"].sort_index()
ratio = (cyb / cyb.rolling(60).mean()).reindex(tr["formed_at"]).values
tr["zone"] = np.where(ratio >= 1.0, "N", np.where(ratio >= 0.97, "B", "D"))
cal = idx.xs("000300.SH", level="symbol").index.sort_values()

PMS = {
    "PM-A 4×7.5%": PositionModel(capital=200_000.0, pos_cap=0.075, max_positions=4,
                                lot_size=0, min_fee=0.0, freeze_pending=True),
    "PM-B 20×5%": PositionModel(capital=200_000.0, pos_cap=0.05, max_positions=20,
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


def band(trades, seg, pm, n):
    ms = [portfolio_metrics(trades, seg, cal,
                            position_model=replace(pm, queue_order="random", queue_seed=s))
          for s in range(n)]
    an = np.array([m["ann"] for m in ms]) * 100
    dd = np.array([m["max_dd"] for m in ms]) * 100
    return an, dd, float(np.median([m["calmar"] for m in ms]))


full = Segment("full", pd.Timestamp("2021-01-01").date(), pd.Timestamp("2026-09-04").date())
years = {y: Segment(f"y{y}", pd.Timestamp(f"{y}-01-01").date(), pd.Timestamp(f"{y}-12-31").date())
         for y in range(2021, 2027)}

for pm_name, PM in PMS.items():
    base_cap = PM.pos_cap
    graded = tr["zone"].map({"N": base_cap, "B": 0.0, "D": base_cap / 2}).astype(float)
    ARMS = {
        "T0 无节流": (pd.Series(True, index=tr.index), None),
        "T1 全空头带停新仓": (tr["zone"] == "N", None),
        "T2 分级(带停/深半仓)": (tr["zone"] != "B", graded),
    }
    print(f"\n======== {pm_name} ========")
    print(f"{'臂':<28}{'ann[min/中/max]':>22}{'dd[min/中/max]':>24}{'calmar':>7} | 分年中位 21→26")
    for filt_name, fmask in (("", pd.Series(True, index=tr.index)), ("+弃过热Q5", tr["hq_n"] != 4)):
        for tname, (tmask, pc) in ARMS.items():
            mask = tmask & fmask
            pm = replace(PM, quality_alloc=True) if pc is not None else PM
            trades = rows(mask, pc)
            an, dd, cm = band(trades, full, pm, 31)
            ys = [np.median(band(trades, seg, pm, 11)[0]) for seg in years.values()]
            print(f"{(tname + filt_name):<28}{an.min():>+6.1f}/{np.median(an):>+6.1f}/{an.max():>+6.1f}%"
                  f"{dd.min():>+8.1f}/{np.median(dd):>+6.1f}/{dd.max():>+6.1f}%{cm:>7.2f} | "
                  + " ".join(f"{a:+5.1f}" for a in ys), flush=True)
