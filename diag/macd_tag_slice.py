# -*- coding: utf-8 -*-
"""MACD(DIF/DEA/柱)×颈线信号 第七轮(2026-09-05 用户指令)。

预注册特征(信号日 T 快照,无前视;EMA 自 2020 起暖机):
  A DIF 零轴上下 / DEA 零轴上下
  B 柱符号(红/绿) × 柱3日斜率(扩张/收缩) 四象限
  C 金叉新旧:DIF 上穿 DEA 距今天数(≤3 新鲜 / 4-10 / >10 / 死叉态)
  D DIF 3日斜率;DIF/close 归一水平分位;柱/ATR 分位
纪律:inner 2025 定桶 → outer 2026 验证 + 分年稳定;幸存→组合口径。
多重检验警示:第七轮,判据从严。
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
syms = tr["symbol"].unique()
lake = pd.read_parquet("data_lake/a_shares_daily.parquet",
                       filters=[("date", ">=", pd.Timestamp("2020-01-01"))],
                       columns=["close"])
lake = lake[lake.index.get_level_values("symbol").isin(syms)]

rows = []
for sym, g in lake.groupby(level="symbol", sort=False):
    g = g.droplevel("symbol").sort_index()
    c = g["close"]
    ema12 = c.ewm(span=12, adjust=False).mean()
    ema26 = c.ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()
    hist = 2.0 * (dif - dea)
    # 金叉新旧:自最近一次 DIF 从 ≤DEA 变 >DEA 以来的天数
    up = dif > dea
    cross = up & ~up.shift(1, fill_value=False)
    days_since = cross.cumsum()
    grp = cross.index.to_series().groupby(days_since)
    age = (grp.cumcount() + 1).where(up, 0)     # 死叉态=0;金叉日=1(昨天刚叉)
    rows.append(pd.DataFrame({
        "symbol": sym, "date": g.index, "close": c.values,
        "dif": dif.values, "dea": dea.values, "hist": hist.values,
        "dif_slope3": (dif - dif.shift(3)).values,
        "hist_slope3": (hist - hist.shift(3)).values,
        "cross_age": age.values}))
mg = pd.concat(rows)
mg["date"] = pd.to_datetime(mg["date"])
tr = tr.merge(mg, left_on=["symbol", "formed_at"], right_on=["symbol", "date"],
              how="left", suffixes=("", "_m"))
tr = tr.dropna(subset=["dif"])
tr["dif_n"] = tr["dif"] / tr["close"]                    # DIF 归一(占价格%)
tr["hist_n"] = tr["hist"] / tr["atr"]                    # 柱归一(ATR 单位)
tr.to_parquet("diag/macd_trades_tagged.parquet")
print(f"tagged n={len(tr)}", flush=True)

inner = tr[(tr.formed_at >= "2025-01-01") & (tr.formed_at < "2026-01-01")]
outer = tr[tr.formed_at >= "2026-01-01"]
def line(d):
    return (f"n={len(d):>5}({len(d)/len(tr[tr.formed_at.dt.year==d.formed_at.dt.iloc[0].year] if len(d) else d)*100:>3.0f}%)"
            if len(d) else "n=0")
def show(label, mask, segs=(("内25", inner), ("外26", outer))):
    out = []
    for nm, d in segs:
        a = d[mask.reindex(d.index)]
        out.append(f"{nm} n={len(a):>5} 胜{(a.avg_pnl_pct>0).mean()*100 if len(a) else 0:>5.1f}% 均{a.avg_pnl_pct.mean() if len(a) else 0:+6.2f}%")
    print(f"{label:<26} " + " | ".join(out))

print(f"\ninner n={len(inner)} 基线{inner.avg_pnl_pct.mean():+.2f}% | outer n={len(outer)} 基线{outer.avg_pnl_pct.mean():+.2f}%")
print("\n── A 零轴 ──")
show("DIF>0", tr.dif > 0); show("DEA>0", tr.dea > 0)
print("\n── B 柱四象限(符号×3日斜率) ──")
show("红柱扩张", (tr["hist"] > 0) & (tr["hist_slope3"] > 0))
show("红柱收缩", (tr["hist"] > 0) & (tr["hist_slope3"] <= 0))
show("绿柱收缩", (tr["hist"] < 0) & (tr["hist_slope3"] >= 0))
show("绿柱扩张", (tr["hist"] < 0) & (tr.hist_slope3 < 0))
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

# 分年稳定性(双窗有苗头的条件在这里过第三关)
print("\n── 分年稳定性(候选条件六年均笔) ──")
tr["year"] = pd.to_datetime(tr["formed_at"]).dt.year
cands = {
    "DIF>0": tr.dif > 0, "DIF<0": tr.dif <= 0,
    "红柱扩张": (tr["hist"] > 0) & (tr["hist_slope3"] > 0), "红柱收缩": (tr["hist"] > 0) & (tr["hist_slope3"] <= 0),
    "绿柱收缩": (tr["hist"] < 0) & (tr["hist_slope3"] >= 0),
    "金叉新鲜≤3": (tr.cross_age >= 1) & (tr.cross_age <= 3), "死叉态": tr.cross_age == 0,
}
hdr = f"{'条件':<12}" + "".join(f"{y:>8}" for y in range(2021, 2027)) + f"{'六年':>8}"
print(hdr)
for lab, m in cands.items():
    sub = tr[m]
    cells = "".join(f"{sub[sub.year==y].avg_pnl_pct.mean():+8.2f}" if len(sub[sub.year==y]) > 100 else f"{'—':>8}"
                    for y in range(2021, 2027))
    print(f"{lab:<12}{cells}{sub.avg_pnl_pct.mean():+8.2f}")
