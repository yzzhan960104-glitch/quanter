# -*- coding: utf-8 -*-
"""四方面同口径对比(2026-09-07):纯收益栈/均衡栈/主腿B3/实验腿r10leg。
同一实盘口径:各自语料 → amihud keep-top5 → PM 20×5% 冻结 → 随机21种子。"""
import sys
sys.path.insert(0, '.')
import numpy as np, pandas as pd
from dataclasses import replace
from backtest.models import PositionModel
from discovery.objective import portfolio_metrics
from discovery.split import Segment

idx = pd.read_parquet("data_lake/index_daily.parquet", columns=["close"])
cal = idx.xs("000300.SH", level="symbol").index.sort_values()
cyb = idx.xs("399006.SZ", level="symbol")["close"].sort_index()
lake = pd.read_parquet("data_lake/a_shares_daily.parquet",
                       filters=[("date", ">=", pd.Timestamp("2019-06-01"))], columns=["close", "amount"])

def load_base(path):
    tr = pd.read_parquet(path)
    tr["formed_at"] = pd.to_datetime(tr["formed_at"])
    syms = set(tr["symbol"].unique())
    lk = lake[lake.index.get_level_values("symbol").isin(syms)]
    ret = lk["close"].groupby(level="symbol").pct_change()
    am = (ret.abs() / (lk["amount"].astype("float32") + 1.0)).groupby(level="symbol").rolling(60, min_periods=48).mean()
    am = am.reset_index(level=0, drop=True).sort_index()
    tr["amihud60"] = am.reindex(pd.MultiIndex.from_arrays([tr["formed_at"], tr["symbol"]])).values
    def kt5(g):
        m = pd.Series(True, index=g.index)
        if len(g) <= 5: return m
        v = g.dropna(subset=["amihud60"])
        if len(v) < 5: return m
        k = v.sort_values("amihud60", ascending=False).head(5).index
        m.loc[g.index.difference(k)] = False
        return m
    tr["sel"] = tr.groupby("formed_at", group_keys=False).apply(kt5)
    return tr[tr["sel"]].copy()

B3 = load_base("diag/retrial_corpus_rolling.parquet")
B3["premium"] = (B3["entry_price"] - B3["neckline"]) / B3["atr"]
B3["cyb_ratio"] = (cyb / cyb.rolling(60).mean()).reindex(B3["formed_at"]).values
EXP = load_base("diag/exp_regime_trades.parquet")

PM = PositionModel(capital=200_000., pos_cap=.05, max_positions=20, lot_size=0, min_fee=0., freeze_pending=True)
full = Segment("f", pd.Timestamp("2021-01-01").date(), pd.Timestamp("2026-09-04").date())
y26 = Segment("y26", pd.Timestamp("2026-01-01").date(), pd.Timestamp("2026-09-04").date())
years = [Segment(f"y{y}", pd.Timestamp(f"{y}-01-01").date(), pd.Timestamp(f"{y}-12-31").date()) for y in range(2021, 2027)]

def ev(sub, seg, n=21):
    trades = [{"symbol": s, "signal_date": pd.Timestamp(d), "buy_date": b, "exit_date": e,
               "avg_pnl_pct": float(p), "entry": None, "priority": None, "pos_cap": None}
              for s, d, b, e, p in zip(sub.symbol, sub.formed_at, sub.entry_date, sub.exit_date, sub.avg_pnl_pct)]
    ms = [portfolio_metrics(trades, seg, cal, position_model=replace(PM, queue_order="random", queue_seed=s)) for s in range(n)]
    an = np.array([m["ann"] for m in ms]); dd = np.array([m["max_dd"] for m in ms])
    return np.median(an)*100, np.median(dd)*100, an.min()*100, an.max()*100

ARMS = [
    ("主腿 B3(基线)", B3),
    ("实验腿 r10leg", EXP),
    ("纯收益栈 skip@3.0", B3[~(B3.premium > 3.0)]),
    ("均衡栈 skip@3.0+T1", B3[(~(B3.premium > 3.0)) & (B3.cyb_ratio >= 1.0)]),
]
print(f"{'臂':<20}{'n':>6}{'逐笔':>7}{'ann中位':>9}{'ann带':>15}{'dd中位':>8}{'2026YTD':>9} | 分年ann 21→26")
for name, sub in ARMS:
    a, d, amin, amax = ev(sub, full)
    a26 = ev(sub, y26, 11)[0]
    ys = [ev(sub, s, 11)[0] for s in years]
    print(f"{name:<20}{len(sub):>6}{sub.avg_pnl_pct.mean():>+6.2f}%{a:>+8.1f}%"
          f"{amin:>+6.1f}~{amax:<+6.1f}%{d:>+7.1f}%{a26:>+8.1f}% | " + " ".join(f"{y:+5.1f}" for y in ys), flush=True)
