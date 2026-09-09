# -*- coding: utf-8 -*-
"""口径交叉复检(24h 审计 · 步骤D · 关键方法论检验)。

问题:前八波过滤全部在 oracle 引擎口径(exit_date 先知序、无容量、吞吐=王)
判死。部署口径(4×7.5% 帽,六年只吃 0.6% 信号,槽位恒满)宽度不是约束——
被否决的闸在部署口径下是否翻活?若翻活,「宽度第一驱动」需降级为 oracle 口径定律。

复检对象(六年语料均有标签,零额外回测):
  缠线带节流 / 全空头带节流(创业板指 MA60) / 弃死叉态(MACD 12,26,9) /
  弃最弱板块 Q1(20 日板块强度) / 弃最热 Q5(过热度) / C 弹性仓位(过热度)
口径:部署=random 21 种子中位数(+oracle 对照),六年全期+分年。
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
Z = pd.read_parquet("diag/factor_zoo_heat.parquet")            # 含 heat_g(过热度全期分位)
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
PM = PositionModel(capital=200_000.0, pos_cap=0.075, max_positions=4,
                   lot_size=0, min_fee=0.0, freeze_pending=True)


def rows(mask, pos_cap=None):
    sub = tr[mask]
    pc = None if pos_cap is None else pos_cap[mask].astype(float).values
    return [{"symbol": s, "signal_date": pd.Timestamp(d), "buy_date": bd, "exit_date": ed,
             "avg_pnl_pct": float(p), "entry": None, "priority": None,
             "pos_cap": None if pc is None else float(pc[i])}
            for i, (s, d, bd, ed, p) in enumerate(zip(
                sub["symbol"].values, sub["formed_at"].values, sub["entry_date"].values,
                sub["exit_date"].values, sub["avg_pnl_pct"].values))]


def evaluate(trades, seg, pm):
    orc = portfolio_metrics(trades, seg, cal, position_model=pm)
    seeds = [portfolio_metrics(trades, seg, cal,
                               position_model=replace(pm, queue_order="random", queue_seed=s))
             for s in range(21)]
    med = {k: float(np.median([m[k] for m in seeds])) for k in ("ann", "max_dd", "calmar", "n_taken")}
    return med, orc


ALL = pd.Series(True, index=tr.index)
cap_map = tr["hq_n"].map({0: 0.10, 1: 0.10, 2: 0.075, 3: 0.075, 4: 0.03})
ARMS = {
    "基线": (ALL, None),
    "缠线带节流(拦.97-1.0)": (~tr["band"], None),
    "全空头带节流(拦<MA60)": (~tr["below"], None),
    "弃死叉态(MACD)": (~(tr["cross_age"].fillna(1) == 0), None),
    "弃最弱板块Q1": (~(tr["sec_r20"].fillna(0.5) < 0.25), None),
    "弃最热Q5(过热度)": (tr["hq_n"] < 4, None),
    "弹性仓位(过热度)": (ALL, cap_map),
}

full = Segment("full", pd.Timestamp("2021-01-01").date(), pd.Timestamp("2026-09-04").date())
years = {y: Segment(f"y{y}", pd.Timestamp(f"{y}-01-01").date(), pd.Timestamp(f"{y}-12-31").date())
         for y in range(2021, 2027)}

print(f"{'臂':<22}{'部署ann':>8}{'dd':>8}{'calmar':>7}{'n_tk':>6} | {'oracle':>8} | 分年部署ann 21→26")
base_years = {}
for name, (mask, pc) in ARMS.items():
    pm = replace(PM, quality_alloc=True) if pc is not None else PM
    trades = rows(mask, pc)
    med, orc = evaluate(trades, full, pm)
    ys = []
    for y, seg in years.items():
        m, _ = evaluate(trades, seg, pm)
        ys.append(m["ann"])
        if name == "基线":
            base_years[y] = m["ann"]
    better = sum(1 for y, a in zip(years, ys) if a >= base_years[y] - 1e-9) if name != "基线" else "-"
    print(f"{name:<22}{med['ann']*100:>+7.1f}%{med['max_dd']*100:>+7.1f}%{med['calmar']:>7.2f}{med['n_taken']:>6.0f} | "
          f"{orc['ann']*100:>+7.1f}% | " + " ".join(f"{a*100:+5.1f}" for a in ys)
          + (f"  非劣年 {better}/6" if name != "基线" else ""), flush=True)
