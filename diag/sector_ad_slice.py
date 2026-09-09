# -*- coding: utf-8 -*-
"""板块涨跌比(A/D 参与度)×颈线信号 第八轮(2026-09-05)。

特征(信号日 T 快照,T 收盘后可用,无前视):
  ad   = 当日板块内上涨家数占比(全湖成分)
  ad5  = 5 日均(参与度水平)
  adch = ad − ad_5日前(参与度方向:改善/恶化)
对照:全市场 A/D(不分组)。
假设两可:普涨确认(risk-on 延续)vs 弱市独强(龙头独立)。
纪律:inner 分位 → outer 验证 → 分年 → (幸存)组合口径。
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

sb = pd.read_parquet("data_lake/stock_basic.parquet")
ind_map = sb.set_index("ts_code")["industry"]
lake = pd.read_parquet("data_lake/a_shares_daily.parquet",
                       filters=[("date", ">=", pd.Timestamp("2020-06-01"))],
                       columns=["close"])
lake = lake[lake.index.get_level_values("symbol").isin(ind_map.index)]
ret = lake["close"].groupby(level="symbol").pct_change()
df = pd.DataFrame({"up": (ret > 0).astype(float)})
df["industry"] = df.index.get_level_values("symbol").map(ind_map)
df = df.dropna(subset=["up"])

ad = df.groupby([pd.Grouper(freq="D", level="date"), "industry"])["up"].mean().unstack("industry")
ad5 = ad.rolling(5).mean()
adch = ad - ad.shift(5)
mkt = df.groupby(pd.Grouper(freq="D", level="date"))["up"].mean()

tr = pd.read_parquet("diag/neckline_regime_trades.parquet")
tr["year"] = pd.to_datetime(tr["formed_at"]).dt.year
tr["industry"] = tr["symbol"].map(ind_map)
fa = pd.to_datetime(tr["formed_at"])
tr["ad"] = [ad.at[d, i] if (i in ad.columns and d in ad.index) else np.nan
            for d, i in zip(fa, tr["industry"])]
tr["ad5"] = [ad5.at[d, i] if (i in ad5.columns and d in ad5.index) else np.nan
             for d, i in zip(fa, tr["industry"])]
tr["adch"] = [adch.at[d, i] if (i in adch.columns and d in adch.index) else np.nan
              for d, i in zip(fa, tr["industry"])]
tr["mkt_ad"] = mkt.reindex(fa).values
tr = tr.dropna(subset=["ad"])
tr.to_parquet("diag/sector_ad_tagged.parquet")
print(f"tagged n={len(tr)}", flush=True)

inner = tr[(tr.formed_at >= "2025-01-01") & (tr.formed_at < "2026-01-01")]
outer = tr[tr.formed_at >= "2026-01-01"]
print(f"inner n={len(inner)} 基线{inner.avg_pnl_pct.mean():+.2f}% | outer n={len(outer)} 基线{outer.avg_pnl_pct.mean():+.2f}%")

def qs(col, n=4, labels=None):
    e = inner[col].quantile([i / n for i in range(1, n)]).values
    edges = np.r_[-1e9, e, 1e9]
    labs = labels or [f"Q{k+1}" for k in range(n)]
    return pd.cut(tr[col], edges, labels=labs), labs

for col, name in (("ad", "当日板块A/D"), ("ad5", "5日板块A/D"), ("adch", "A/D 5日变化"), ("mkt_ad", "全市场A/D(对照)")):
    b, labs = qs(col)
    print(f"\n== {name} ==")
    for lab in labs:
        r = []
        for nm, d in (("内25", inner), ("外26", outer)):
            a = d[b.reindex(d.index) == lab]
            r.append(f"{nm} n={len(a):>5} 胜{(a.avg_pnl_pct>0).mean()*100 if len(a) else 0:>5.1f}% 均{a.avg_pnl_pct.mean() if len(a) else 0:+6.2f}%")
        print(f"  {lab:<8} " + " | ".join(r))

# 分年:Q1(最弱参与)vs Q4(最强参与) 价差
print("\n== 分年 Q4−Q1 价差pp ==")
for col, name in (("ad", "当日A/D"), ("ad5", "5日A/D"), ("adch", "变化")):
    b, _ = qs(col)
    cells = []
    for y in range(2021, 2027):
        sub = tr[(b == "Q4") & (tr.year == y)]["avg_pnl_pct"]
        sub2 = tr[(b == "Q1") & (tr.year == y)]["avg_pnl_pct"]
        cells.append(f"{(sub.mean()-sub2.mean()):+5.2f}" if len(sub) > 80 and len(sub2) > 80 else "  —")
    print(f"  {name:<8} " + "/".join(cells) + "  (21-26)")
