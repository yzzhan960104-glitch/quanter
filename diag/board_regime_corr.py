# -*- coding: utf-8 -*-
"""分板块×对应指数 相关性分析(2026-09-05)。

用户问题:相关性应对照「买入股票对应的指数」——当前池是双创,是否与双创指数
高度正相关?分析面:
  ① 池构成逐年漂移(创业板/科创板/主板占比)
  ② 各板块月度均笔 vs 候选基准(对应指数/沪深300/中证1000/中证500)相关性排名
  ③ 双创合成指数(399006+000688 等权日收益累积)与双创子池的相关性
  ④ 条件分布:信号日对应指数 vs MA60(决策时可用)
  ⑤ 当前各指数状态读数
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
tr["ym"] = tr["formed_at"].dt.strftime("%Y-%m")

def board(sym):
    code, ex = sym.split(".")
    if code.startswith("688"): return "科创板"
    if code.startswith(("300", "301")): return "创业板"
    if ex == "SH": return "沪主板"
    return "深主板"
tr["board"] = tr["symbol"].map(board)

# ── 指数月收益 ──
idx = pd.read_parquet("data_lake/index_daily.parquet", columns=["close"])
def d(sym): return idx.xs(sym, level="symbol")["close"].sort_index()
S = {"hs300": d("000300.SH"), "csi500": d("000905.SH"), "csi1000": d("000852.SH"),
     "cyb": d("399006.SZ"), "kc50": d("000688.SH")}
# 双创合成:创业板指+科创50 等权日收益累积
ret = pd.DataFrame({k: v.pct_change() for k, v in S.items()}).dropna()
ret["dual"] = ret[["cyb", "kc50"]].mean(axis=1)
dual_idx = (1 + ret["dual"]).cumprod()
M = pd.DataFrame({k: v.resample("ME").last().pct_change() for k, v in
                  {**S, "dual": dual_idx}.items()})
M.index = M.index.to_period("M")

# ── ① 池构成漂移 ──
tr["year"] = tr["formed_at"].dt.year
comp = pd.crosstab(tr["year"], tr["board"])
print("== ① 池构成(笔数) ==")
print(comp.to_string())
print("\n占比%:")
print((comp.div(comp.sum(1), axis=0) * 100).round(1).to_string())

# ── ②③ 各板块月度均笔 vs 基准相关性 ──
print("\n== ② 各板块:月度均笔 vs 基准 同月 corr(n月) ==")
bench_of = {"创业板": ["cyb", "csi1000", "csi500", "hs300"],
            "科创板": ["kc50", "csi1000", "hs300"],
            "沪主板": ["hs300", "csi500", "csi1000"],
            "深主板": ["hs300", "csi500", "csi1000"]}
for b in ("创业板", "科创板", "沪主板", "深主板"):
    sub = tr[tr.board == b]
    if len(sub) < 1500:
        print(f"{b}: n={len(sub)} 样本不足"); continue
    m = sub.groupby("ym")["avg_pnl_pct"].mean()
    m.index = pd.PeriodIndex(m.index, freq="M")
    row = []
    for bench in bench_of[b]:
        j = pd.DataFrame({"x": m}).join(M[bench]).dropna()
        row.append(f"{bench} {np.corrcoef(j.x, j[bench])[0,1]:+.3f}({len(j)})")
    print(f"  {b} n={len(sub):>6}: " + " | ".join(row))
# 双创合并池 vs 双创合成
dual = tr[tr.board.isin(("创业板", "科创板"))]
m = dual.groupby("ym")["avg_pnl_pct"].mean(); m.index = pd.PeriodIndex(m.index, freq="M")
row = []
for bench in ("dual", "cyb", "kc50", "csi1000", "hs300"):
    j = pd.DataFrame({"x": m}).join(M[bench]).dropna()
    row.append(f"{bench} {np.corrcoef(j.x, j[bench])[0,1]:+.3f}({len(j)})")
print(f"  双创合并 n={len(dual):>6}: " + " | ".join(row))
m2 = dual.groupby("ym")["avg_pnl_pct"].sum(); m2.index = pd.PeriodIndex(m2.index, freq="M")
j = pd.DataFrame({"x": m2}).join(M["dual"]).dropna()
print(f"    (月度总量口径 vs dual: {np.corrcoef(j.x, j['dual'])[0,1]:+.3f})")

# ── ④ 条件分布:信号日对应指数 vs MA60 ──
print("\n== ③ 条件分布(信号日对应指数 vs 各自 MA60) ==")
ma_of = {"创业板": S["cyb"].rolling(60).mean(), "科创板": S["kc50"].rolling(60).mean(),
         "沪主板": S["hs300"].rolling(60).mean(), "深主板": S["hs300"].rolling(60).mean()}
for b in ("创业板", "科创板", "沪主板", "深主板"):
    sub = tr[tr.board == b].set_index("formed_at")
    if len(sub) < 1500: continue
    ma = ma_of[b]
    sub["bull"] = (ma.index.to_series().map(ma).reindex(sub.index).notna() &
                   (sub.index.to_series().map(S[{"创业板":"cyb","科创板":"kc50","沪主板":"hs300","深主板":"hs300"}[b]]).reindex(sub.index) >
                    sub.index.to_series().map(ma).reindex(sub.index)).values)
    for lab, s2 in (("多头日", sub[sub.bull]), ("空头日", sub[~sub.bull])):
        if len(s2) < 200: continue
        print(f"  {b} {lab}: n={len(s2):>6} 胜率{(s2.avg_pnl_pct>0).mean()*100:>5.1f}% "
              f"均笔{s2.avg_pnl_pct.mean():+6.2f}% 止损占比{(s2.exit_reason=='stop_loss').mean()*100:.0f}%")

# ── ⑤ 当前状态 ──
print("\n== ④ 当前指数状态(最新交易日) ==")
for k in ("dual", "cyb", "kc50", "hs300", "csi1000"):
    s = dual_idx if k == "dual" else S[k]
    ma = s.rolling(60).mean()
    last = s.iloc[-1]
    print(f"  {k:>8}: {last:9.2f} vs MA60 {ma.iloc[-1]:9.2f} "
          f"{'↑多头' if last > ma.iloc[-1] else '↓空头'}  "
          f"20日 {(last/s.iloc[-21]-1)*100:+5.1f}%  60日 {(last/s.iloc[-61]-1)*100:+5.1f}%", 
          f"  [{s.index[-1].date()}]")
