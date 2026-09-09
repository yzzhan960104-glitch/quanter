# -*- coding: utf-8 -*-
"""因子动物园构建(2026-09-05 24h 审计 · 步骤A)。

湖内未测信息源 → 信号日快照特征表 diag/factor_zoo.parquet(每笔一行,
决策时可用,无前视:全部取 formed_at 当日或之前窗口的值)。

家族(约 20 因子):
  估值/流动性(daily_basic): turnover, turnover20, log_mv, pe_rank, pb_rank
  资金流(moneyflow,万元): elg_ratio(超大单净/成交额), mf_ratio(净流入/流通市值), elg5(5日)
  筹码(cyq_perf): winner_rate(获利盘), chip_conc(集中度), pdev(价格/加权成本乖离)
  技术对照(stk_factor_pro): kdj_k, rsi6, cci
  价量衍生(湖自算): rv_ratio, beta1000, idio, gap20(跳空频率), pos20(距20日高)
  日历: weekday
  市场流(moneyflow_hsgt): north5(北向5日净额)
  龙虎榜(top_inst): inst_net10(信号前10日机构净买)
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

import numpy as np
import pandas as pd

tr = pd.read_parquet("diag/neckline_regime_trades.parquet")
syms = set(tr["symbol"].unique())
fa = pd.to_datetime(tr["formed_at"])
KEY = pd.MultiIndex.from_arrays([fa, tr["symbol"]], names=["date", "symbol"])
D0 = pd.Timestamp("2020-06-01")
print(f"corpus n={len(tr)} syms={len(syms)}", flush=True)


def load(f, cols):
    df = pd.read_parquet(f"data_lake/{f}.parquet", columns=cols)
    df = df[df.index.get_level_values("symbol").isin(syms)]
    df = df[df.index.get_level_values("date") >= D0]
    return df.sort_index()


def roll_by_symbol(s, win, fn="mean", minp=None):
    g = s.groupby(level="symbol", group_keys=False)
    return g.apply(lambda x: getattr(x.rolling(win, min_periods=minp or win), fn)())


feats = {}

# ── ① daily_basic:估值/流动性 ──
db = load("daily_basic", ["turnover_rate", "pe_ttm", "pb", "circ_mv"])
db["log_mv"] = np.log(db["circ_mv"].clip(lower=1))
pe = db["pe_ttm"].where(db["pe_ttm"] > 0)
pb = db["pb"].where(db["pb"] > 0)
db["pe_rank"] = pe.groupby(level="date").rank(pct=True)
db["pb_rank"] = pb.groupby(level="date").rank(pct=True)
db["turnover20"] = roll_by_symbol(db["turnover_rate"], 20, minp=10)
for feat, col in {"turnover": "turnover_rate", "turnover20": "turnover20",
                  "log_mv": "log_mv", "pe_rank": "pe_rank", "pb_rank": "pb_rank"}.items():
    feats[feat] = db[col].reindex(KEY).values
print("① daily_basic done", flush=True)

# ── ② moneyflow:主力资金 ──
mf = load("moneyflow", ["buy_elg_amount", "sell_elg_amount", "net_mf_amount"])
amt = load("a_shares_daily", ["amount"])["amount"]
mf["elg_ratio"] = (mf["buy_elg_amount"] - mf["sell_elg_amount"]) / amt.reindex(mf.index).replace(0, np.nan)
mf["mf_ratio"] = mf["net_mf_amount"] / db["circ_mv"].reindex(mf.index).replace(0, np.nan)
mf["elg5"] = roll_by_symbol(mf["elg_ratio"], 5, minp=3)
for feat in ("elg_ratio", "mf_ratio", "elg5"):
    feats[feat] = mf[feat].reindex(KEY).values
print("② moneyflow done", flush=True)

# ── ③ cyq_perf:筹码 ──
cyq = load("cyq_perf", ["winner_rate", "cost_15pct", "cost_85pct", "weight_avg"])
cl = load("a_shares_daily", ["close"])["close"]
cyq["chip_conc"] = (cyq["cost_85pct"] - cyq["cost_15pct"]) / cyq["weight_avg"].replace(0, np.nan)
cyq["pdev"] = cl.reindex(cyq.index) / cyq["weight_avg"].replace(0, np.nan) - 1.0
for feat in ("winner_rate", "chip_conc", "pdev"):
    feats[feat] = cyq[feat].reindex(KEY).values
print("③ cyq_perf done", flush=True)

# ── ④ 技术对照 ──
fp = load("stk_factor_pro", ["kdj_k_bfq", "rsi_bfq_6", "cci_bfq"])
for feat, col in {"kdj_k": "kdj_k_bfq", "rsi6": "rsi_bfq_6", "cci": "cci_bfq"}.items():
    feats[feat] = fp[col].reindex(KEY).values
print("④ stk_factor_pro done", flush=True)

# ── ⑤ 价量衍生 ──
lk = load("a_shares_daily", ["open", "high", "low", "close"])
idx = pd.read_parquet("data_lake/index_daily.parquet", columns=["close"])
r1000 = idx.xs("000852.SH", level="symbol")["close"].sort_index().pct_change()
rows = []
for sym, g in lk.groupby(level="symbol", sort=False):
    g = g.droplevel("symbol").sort_index()
    c, o, h, l = g["close"], g["open"], g["high"], g["low"]
    ret = c.pct_change()
    rv = ret.rolling(20).std()
    tr_ = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
    atr = tr_.rolling(14).mean() / c
    mkt = r1000.reindex(g.index)
    beta = ret.rolling(60).cov(mkt) / mkt.rolling(60).var()
    idio = (ret - beta * mkt).rolling(60).std()
    gap = ((o / c.shift(1) - 1).abs() > 0.01).astype(float).rolling(20).mean()
    pos20 = c / c.rolling(20).max() - 1.0
    rows.append(pd.DataFrame({"date": g.index, "symbol": sym,
                              "rv_ratio": (rv / atr).values, "beta1000": beta.values,
                              "idio": idio.values, "gap20": gap.values, "pos20": pos20.values}))
pv = pd.concat(rows).set_index(["date", "symbol"]).sort_index()
for feat in pv.columns:
    feats[feat] = pv[feat].reindex(KEY).values
print("⑤ price-derived done", flush=True)

# ── ⑥ 日历 ──
feats["weekday"] = fa.dt.weekday.values

# ── ⑦ 北向 5 日(市场级) ──
hg = pd.read_parquet("data_lake/moneyflow_hsgt.parquet", columns=["north_money"])
nm = hg["north_money"].groupby(level="date").sum().sort_index()
feats["north5"] = nm.rolling(5).sum().reindex(fa).values

# ── ⑧ 龙虎榜:信号前 10 日机构净买 ──
ti = load("top_inst", ["net_buy"])
netb = ti["net_buy"].groupby(level=["date", "symbol"]).sum()
# 稠密化到交易日历再滚动(稀疏事件表直接 rolling 会跨越非相邻日)
cal = cl.index.get_level_values("date").unique().sort_values()
dense = netb.unstack("symbol").reindex(cal).fillna(0.0)
roll10 = dense.rolling(10, min_periods=1).sum().stack()
roll10.index.names = ["date", "symbol"]
feats["inst_net10"] = roll10.reindex(KEY).fillna(0).values
print("⑦⑧ hsgt/top_inst done", flush=True)

F = pd.DataFrame(feats, index=tr.index)
F["avg_pnl_pct"] = tr["avg_pnl_pct"].values
F["exit_reason"] = tr["exit_reason"].values
F["symbol"] = tr["symbol"].values
F["formed_at"] = fa.values
F["seg"] = np.where(fa >= "2026-01-01", "outer", np.where(fa >= "2025-01-01", "inner", "hist"))
F["year"] = fa.dt.year.values
F.to_parquet("diag/factor_zoo.parquet")
print(f"saved diag/factor_zoo.parquet shape={F.shape}", flush=True)
print("缺失率:\n" + F.isna().mean().round(3).to_string(), flush=True)
