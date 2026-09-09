# -*- coding: utf-8 -*-
"""MACD 参数×颈线时间窗匹配检验(2026-09-05)。

假设(用户):标准(12,26,9) 与颈线 window=60 不匹配,慢线应对齐形态窗。
变体:V0(12,26,9) 标准 / V1(18,38,13) 居中 / V2(26,60,18) 慢线=60 /
V3(30,60,20)。对比面:DIF 零轴劈开、金叉新旧梯度(上一轮唯一真发现)、
绿柱扩张单元、双窗+分年稳定性。
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
tr["year"] = pd.to_datetime(tr["formed_at"]).dt.year
syms = tr["symbol"].unique()
lake = pd.read_parquet("data_lake/a_shares_daily.parquet",
                       filters=[("date", ">=", pd.Timestamp("2020-01-01"))],
                       columns=["close"])
lake = lake[lake.index.get_level_values("symbol").isin(syms)]

VARIANTS = {"V0(12,26,9)": (12, 26, 9), "V1(18,38,13)": (18, 38, 13),
            "V2(26,60,18)": (26, 60, 18), "V3(30,60,20)": (30, 60, 20)}

out = {}
for vname, (f, sl, sg) in VARIANTS.items():
    rows = []
    for sym, g in lake.groupby(level="symbol", sort=False):
        g = g.droplevel("symbol").sort_index()
        c = g["close"]
        dif = c.ewm(span=f, adjust=False).mean() - c.ewm(span=sl, adjust=False).mean()
        dea = dif.ewm(span=sg, adjust=False).mean()
        hist = 2.0 * (dif - dea)
        up = dif > dea
        cross = up & ~up.shift(1, fill_value=False)
        age = (cross.index.to_series().groupby(cross.cumsum()).cumcount() + 1).where(up, 0)
        rows.append(pd.DataFrame({
            "symbol": sym, "date": g.index,
            "dif": dif.values, "dea": dea.values, "hist": hist.values,
            "hist_slope3": (hist - hist.shift(3)).values, "cross_age": age.values}))
    mg = pd.concat(rows)
    mg["date"] = pd.to_datetime(mg["date"])
    m = tr.merge(mg, left_on=["symbol", "formed_at"], right_on=["symbol", "date"],
                 how="left").dropna(subset=["dif"])
    out[vname] = m
    print(f"{vname}: tagged n={len(m)}", flush=True)

print(f"\n基线: inner25 +2.76% / outer26 +1.06% / 六年 +1.24%")
for vname, m in out.items():
    inner = m[(m.formed_at >= "2025-01-01") & (m.formed_at < "2026-01-01")]["avg_pnl_pct"]
    outer = m[m.formed_at >= "2026-01-01"]["avg_pnl_pct"]
    dead = m[m.cross_age == 0]["avg_pnl_pct"]
    old = m[m.cross_age > 10]["avg_pnl_pct"]
    fresh = m[(m.cross_age >= 1) & (m.cross_age <= 3)]["avg_pnl_pct"]
    dpos = m[m.dif > 0]["avg_pnl_pct"]; dneg = m[m.dif <= 0]["avg_pnl_pct"]
    gexp_i = m[(m["hist"] < 0) & (m.hist_slope3 < 0) & (m.formed_at >= "2025-01-01") & (m.formed_at < "2026-01-01")]["avg_pnl_pct"]
    gexp_o = m[(m["hist"] < 0) & (m.hist_slope3 < 0) & (m.formed_at >= "2026-01-01")]["avg_pnl_pct"]
    print(f"\n== {vname} ==")
    print(f"  DIF>0 六年{dneg.mean() if len(dneg) else 0:+.2f}(n={len(dneg)}) vs DIF<=0 {dneg.mean() if len(dneg) else 0:+.2f}" if False else
          f"  DIF>0 {dpos.mean():+.2f}(n={len(dpos)}) | DIF<=0 {dneg.mean():+.2f}(n={len(dneg)})")
    print(f"  金叉梯度: 老>10日 {old.mean():+.2f}(n={len(old)}) 新鲜≤3 {fresh.mean():+.2f}(n={len(fresh)}) 死叉 {dead.mean():+.2f}(n={len(dead)}) spread={old.mean()-dead.mean():+.2f}pp")
    print(f"  绿柱扩张: inner {gexp_i.mean():+.2f}(n={len(gexp_i)}) / outer {gexp_o.mean():+.2f}(n={len(gexp_o)})")
    ydead = dead.groupby(m[m.cross_age == 0]["year"]).mean()
    print("  死叉分年: " + "/".join(f"{ydead.get(y, float('nan')):+.2f}" for y in range(2021, 2027)))
