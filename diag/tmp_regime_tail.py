import sys
sys.path.insert(0, '.')
import numpy as np, pandas as pd

tr = pd.read_parquet("diag/neckline_regime_trades.parquet")
tr["ym"] = tr["formed_at"].dt.strftime("%Y-%m")
idx = pd.read_parquet("data_lake/index_daily.parquet", columns=["close"])
hs300 = idx.xs("000300.SH", level="symbol")["close"].sort_index()
csi1000 = idx.xs("000852.SH", level="symbol")["close"].sort_index()
m300 = hs300.resample("ME").last().pct_change()
m1000 = csi1000.resample("ME").last().pct_change()
m300.index = m300.index.to_period("M"); m1000.index = m1000.index.to_period("M")

lake = pd.read_parquet("data_lake/a_shares_daily.parquet",
                       filters=[("date", ">=", pd.Timestamp("2019-06-01"))], columns=["close"])
lake = lake[lake.index.get_level_values("symbol").isin(tr["symbol"].unique())]
rows = []
for sym, gdf in lake.groupby(level="symbol", sort=False):
    gdf = gdf.droplevel("symbol").sort_index()
    ma = gdf["close"].rolling(200, min_periods=200).mean()
    rows.append(pd.DataFrame({"symbol": sym, "date": gdf.index,
                              "below200": gdf["close"].values < ma.values}))
mg = pd.concat(rows)
tr2 = tr.merge(mg, left_on=["symbol", "formed_at"], right_on=["symbol", "date"], how="left")
frac = tr2.groupby("ym")["below200"].mean()
frac.index = pd.PeriodIndex(frac.index, freq="M")
j = pd.DataFrame({"below200%": frac * 100, "hs300月%": m300 * 100,
                  "csi1000月%": m1000 * 100}).dropna()
ny = tr2.groupby("ym").size(); ny.index = pd.PeriodIndex(ny.index, freq="M")
j["笔数"] = ny
print(j.resample("Q").mean(numeric_only=True).round(1).to_string())

ma60 = hs300.rolling(60).mean()
bull = (hs300 > ma60)
tr2["bull"] = bull.reindex(tr2["formed_at"]).fillna(False).values
print()
for lab, sub in (("多头日", tr2[tr2.bull]), ("空头日", tr2[~tr2.bull])):
    b, a = sub[sub.below200], sub[~sub.below200]
    print(f"{lab}: n={len(sub)} below200占比{sub.below200.mean()*100:.0f}% | "
          f"below200组 均笔{b.avg_pnl_pct.mean():+.2f}% 胜率{(b.avg_pnl_pct>0).mean()*100:.0f}% | "
          f"above组 均笔{a.avg_pnl_pct.mean():+.2f}% 胜率{(a.avg_pnl_pct>0).mean()*100:.0f}%")
