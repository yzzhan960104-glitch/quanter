# -*- coding: utf-8 -*-
"""MACD 切片(读已落盘 diag/macd_trades_tagged.parquet,第七轮第二段)。"""
import sys
sys.path.insert(0, '.')
import numpy as np, pandas as pd

tr = pd.read_parquet("diag/macd_trades_tagged.parquet")
tr["year"] = pd.to_datetime(tr["formed_at"]).dt.year
inner = tr[(tr.formed_at >= "2025-01-01") & (tr.formed_at < "2026-01-01")]
outer = tr[tr.formed_at >= "2026-01-01"]

def show(label, mask):
    out = []
    for nm, d in (("内25", inner), ("外26", outer)):
        a = d[mask.reindex(d.index).fillna(False)]
        out.append(f"{nm} n={len(a):>5} 胜{(a.avg_pnl_pct>0).mean()*100 if len(a) else 0:>5.1f}% 均{a.avg_pnl_pct.mean() if len(a) else 0:+6.2f}%")
    print(f"{label:<24} " + " | ".join(out))

print(f"inner n={len(inner)} 基线{inner.avg_pnl_pct.mean():+.2f}% | outer n={len(outer)} 基线{outer.avg_pnl_pct.mean():+.2f}%")
print("\n── A 零轴 ──")
show("DIF>0", tr.dif > 0); show("DEA>0", tr.dea > 0)
print("\n── B 柱四象限(符号×3日斜率) ──")
show("红柱扩张", (tr["hist"] > 0) & (tr["hist_slope3"] > 0))
show("红柱收缩", (tr["hist"] > 0) & (tr["hist_slope3"] <= 0))
show("绿柱收缩", (tr["hist"] < 0) & (tr["hist_slope3"] >= 0))
show("绿柱扩张", (tr["hist"] < 0) & (tr["hist_slope3"] < 0))
print("\n── C 金叉新旧 ──")
show("死叉态", tr.cross_age == 0)
show("金叉≤3日(新鲜)", (tr.cross_age >= 1) & (tr.cross_age <= 3))
show("金叉4-10日", (tr.cross_age > 3) & (tr.cross_age <= 10))
show("金叉>10日(老)", tr.cross_age > 10)
print("\n── D 其他 ──")
show("DIF3日上行", tr.dif_slope3 > 0)
q = inner["dif_n"].quantile([.25, .5, .75])
for lab, m in (("dif_n Q1低", tr.dif_n <= q.iloc[0]), ("dif_n Q2", (tr.dif_n > q.iloc[0]) & (tr.dif_n <= q.iloc[1])),
               ("dif_n Q3", (tr.dif_n > q.iloc[1]) & (tr.dif_n <= q.iloc[2])), ("dif_n Q4高", tr.dif_n > q.iloc[2])):
    show(lab, m)
q2 = inner["hist_n"].quantile([.25, .5, .75])
for lab, m in (("hist_n Q1", tr.hist_n <= q2.iloc[0]), ("hist_n Q2", (tr.hist_n > q2.iloc[0]) & (tr.hist_n <= q2.iloc[1])),
               ("hist_n Q3", (tr.hist_n > q2.iloc[1]) & (tr.hist_n <= q2.iloc[2])), ("hist_n Q4", tr.hist_n > q2.iloc[2])):
    show(lab, m)

print("\n── 分年稳定性(候选六年均笔%) ──")
cands = {
    "DIF>0": tr.dif > 0, "DIF<=0": tr.dif <= 0,
    "红柱扩张": (tr["hist"] > 0) & (tr["hist_slope3"] > 0), "红柱收缩": (tr["hist"] > 0) & (tr["hist_slope3"] <= 0),
    "绿柱收缩": (tr["hist"] < 0) & (tr["hist_slope3"] >= 0), "绿柱扩张": (tr["hist"] < 0) & (tr["hist_slope3"] < 0),
    "金叉新鲜≤3": (tr.cross_age >= 1) & (tr.cross_age <= 3), "金叉老>10": tr.cross_age > 10,
    "死叉态": tr.cross_age == 0, "DIF3日上行": tr.dif_slope3 > 0,
}
print(f"{'条件':<12}" + "".join(f"{y:>8}" for y in range(2021, 2027)) + f"{'六年':>8}")
for lab, m in cands.items():
    sub = tr[m]
    cells = "".join(f"{sub[sub.year==y].avg_pnl_pct.mean():+8.2f}" if len(sub[sub.year==y]) > 100 else f"{'—':>8}"
                    for y in range(2021, 2027))
    print(f"{lab:<12}{cells}{sub.avg_pnl_pct.mean():+8.2f}")
