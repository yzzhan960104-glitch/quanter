# -*- coding: utf-8 -*-
"""科创板状态不敏感的敏感性检查:换状态变量(csi1000/dual)重切+分年分板。"""
import sys
sys.path.insert(0, '.')
import numpy as np, pandas as pd

tr = pd.read_parquet("diag/neckline_regime_trades.parquet")
tr["ym"] = tr["formed_at"].dt.strftime("%Y-%m")
def board(sym):
    code = sym.split(".")[0]
    if code.startswith("688"): return "科创板"
    if code.startswith(("300","301")): return "创业板"
    return "主板"
tr["board"] = tr["symbol"].map(board)

idx = pd.read_parquet("data_lake/index_daily.parquet", columns=["close"])
def d(s): return idx.xs(s, level="symbol")["close"].sort_index()
S = {"hs300": d("000300.SH"), "csi1000": d("000852.SH"), "cyb": d("399006.SZ"), "kc50": d("000688.SH")}
dual = (1 + pd.DataFrame({k: v.pct_change() for k, v in S.items()}).dropna()[["cyb","kc50"]].mean(axis=1)).cumprod()
S["dual"] = dual

def split(sub, state_name):
    s = S[state_name]; ma = s.rolling(60).mean()
    bull = (s > ma)
    ts = sub.set_index("formed_at")
    b = bull.reindex(ts.index).fillna(False).values
    for lab, mask in (("多头", b), ("空头", ~b)):
        x = ts[mask]
        if len(x) < 200: continue
        print(f"    {state_name:>7} {lab}日: n={len(x):>6} 胜率{(x.avg_pnl_pct>0).mean()*100:>5.1f}% 均笔{x.avg_pnl_pct.mean():+6.2f}%")

print("== 科创板(n=18100)换状态变量 ==")
for st in ("kc50", "csi1000", "dual"):
    split(tr[tr.board=="科创板"], st)
print("== 创业板(n=47942)换状态变量 ==")
for st in ("cyb", "csi1000", "dual"):
    split(tr[tr.board=="创业板"], st)

print("\n== 分年×分板 均笔%/胜率% ==")
g = tr.groupby(["year", "board"])["avg_pnl_pct"]
t = pd.DataFrame({"n": g.size(),
                  "胜率%": tr.assign(w=tr.avg_pnl_pct>0).groupby(["year","board"])["w"].mean()*100,
                  "均笔%": g.mean()})
print(t.round(2).to_string())
