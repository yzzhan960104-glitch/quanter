# -*- coding: utf-8 -*-
"""完全对齐实盘口径的终检(24h 审计 · 步骤F · 2026-09-06)。

用户问题:oracle 口径不能和实盘对齐么(实盘一年最多 ~5单/日,不是几万单)?
对齐三件套(全部落地):
  ① 选择规则=实盘 amihud60 keep-top5(当日>5 只按 amihud 降序留 5,公式与
     ops/amihud_pct_writer 同源:|ret|/(amount+1) 60日滚动均值);
  ② 并发形态=PM-B 20×5% 冻结挂单(单日均新挂≤5 由 keep-top5 天然满足);
  ③ 队列序=随机 21 种子中位(残留的现金竞争序不确定性)。
臂:基线 / +T1节流(创业板指<MA60 停新仓) / +弃过热Q5 / T1+弃Q5;
对照臂=无选择规则(全信号直进 PM-B,即前几轮的部署口径)——量化
「实盘 amihud 选序本身」的贡献。
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

# ── amihud60(与 ops/amihud_pct_writer 同源公式) ──
syms = set(tr["symbol"].unique())
lake = pd.read_parquet("data_lake/a_shares_daily.parquet",
                       filters=[("date", ">=", pd.Timestamp("2019-06-01"))],
                       columns=["close", "amount"])
lake = lake[lake.index.get_level_values("symbol").isin(syms)]
ret = lake["close"].groupby(level="symbol").pct_change()
amihud = (ret.abs() / (lake["amount"].astype("float32") + 1.0)) \
    .groupby(level="symbol").rolling(60, min_periods=48).mean()
amihud = amihud.reset_index(level=0, drop=True).sort_index()
KEY = pd.MultiIndex.from_arrays([tr["formed_at"], tr["symbol"]], names=["date", "symbol"])
tr["amihud60"] = amihud.reindex(KEY).values
print(f"amihud 覆盖 {tr['amihud60'].notna().mean()*100:.1f}%", flush=True)

# ── 实盘选择规则:每日 keep-top5(有效值不足→全留,fail-open 同规格) ──
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
sel = tr.groupby("formed_at", group_keys=False).apply(keep_top5)
print(f"keep-top5 后信号 {sel.sum()}/{len(tr)}(每日常态≤5)", flush=True)

idx = pd.read_parquet("data_lake/index_daily.parquet", columns=["close"])
cyb = idx.xs("399006.SZ", level="symbol")["close"].sort_index()
ratio = (cyb / cyb.rolling(60).mean()).reindex(tr["formed_at"]).values
tr["norm"] = ratio >= 1.0
cal = idx.xs("000300.SH", level="symbol").index.sort_values()
PM = PositionModel(capital=200_000.0, pos_cap=0.05, max_positions=20,
                   lot_size=0, min_fee=0.0, freeze_pending=True)


def rows(mask):
    sub = tr[mask]
    return [{"symbol": s, "signal_date": pd.Timestamp(d), "buy_date": bd, "exit_date": ed,
             "avg_pnl_pct": float(p), "entry": None, "priority": None, "pos_cap": None}
            for s, d, bd, ed, p in zip(sub["symbol"].values, sub["formed_at"].values,
                                       sub["entry_date"].values, sub["exit_date"].values,
                                       sub["avg_pnl_pct"].values)]


def band(trades, seg, n=21):
    ms = [portfolio_metrics(trades, seg, cal,
                            position_model=replace(PM, queue_order="random", queue_seed=s))
          for s in range(n)]
    an = np.array([m["ann"] for m in ms]) * 100
    dd = np.array([m["max_dd"] for m in ms]) * 100
    return an, dd


full = Segment("full", pd.Timestamp("2021-01-01").date(), pd.Timestamp("2026-09-04").date())
years = {y: Segment(f"y{y}", pd.Timestamp(f"{y}-01-01").date(), pd.Timestamp(f"{y}-12-31").date())
         for y in range(2021, 2027)}

ARMS = {
    "全信号直进(前几轮对照)": pd.Series(True, index=tr.index),
    "实盘口径(keep-top5)": sel,
    "  +T1节流": sel & tr["norm"],
    "  +弃过热Q5": sel & (tr["hq_n"] != 4),
    "  +T1+弃Q5": sel & tr["norm"] & (tr["hq_n"] != 4),
}
print(f"\n{'臂':<26}{'ann[min/中/max]':>22}{'dd[min/中/max]':>24}{'n_tk':>6} | 分年中位 21→26 | inner/outer")
for name, mask in ARMS.items():
    trades = rows(mask)
    an, dd = band(trades, full)
    ys = [np.median(band(trades, seg, 11)[0]) for seg in years.values()]
    inn = Segment("i", pd.Timestamp("2025-01-01").date(), pd.Timestamp("2025-12-31").date())
    out = Segment("o", pd.Timestamp("2026-01-01").date(), pd.Timestamp("2026-09-04").date())
    ri = np.median(band(trades, inn, 11)[0]); ro = np.median(band(trades, out, 11)[0])
    nt = np.median([portfolio_metrics(trades, full, cal, position_model=replace(
        PM, queue_order="random", queue_seed=0))["n_taken"]])
    print(f"{name:<26}{an.min():>+6.1f}/{np.median(an):>+6.1f}/{an.max():>+6.1f}%"
          f"{dd.min():>+8.1f}/{np.median(dd):>+6.1f}/{dd.max():>+6.1f}%{nt:>6.0f} | "
          + " ".join(f"{a:+5.1f}" for a in ys) + f" | {ri*100:+5.1f}/{ro*100:+5.1f}", flush=True)
