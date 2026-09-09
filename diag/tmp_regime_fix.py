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

g = tr.groupby("ym")["avg_pnl_pct"]
mon = pd.DataFrame({"n": g.size(), "avg": g.mean(), "sum": g.sum()})
mon.index = pd.PeriodIndex(mon.index, freq="M")

for name, mret in (("沪深300", m300), ("中证1000", m1000)):
    j = mon.join(mret.rename("mkt")).dropna()
    c_same = np.corrcoef(j["avg"], j["mkt"])[0, 1]
    c_sum = np.corrcoef(j["sum"], j["mkt"])[0, 1]
    j2 = mon.join(mret.shift(-1).rename("mkt_next")).dropna()
    c_next = np.corrcoef(j2["avg"], j2["mkt_next"])[0, 1]
    print(f"② 月度均笔 vs {name}: 同月 corr={c_same:+.3f} (n={len(j)}月) | 次月 corr={c_next:+.3f} | 月总量 corr={c_sum:+.3f}")

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
print(f"\n④ 月度「信号在MA200下方占比」vs 同月指数: corr(300)={np.corrcoef(j['below200%'], j['hs300月%'])[0,1]:+.3f} "
      f"corr(1000)={np.corrcoef(j['below200%'], j['csi1000月%'])[0,1]:+.3f} (n={len(j)}月)")
ny = tr2.groupby("ym").size(); ny.index = pd.PeriodIndex(ny.index, freq="M")
j["笔数"] = ny
q = j.resample("QE").mean(numeric_only=True).round(1)
print(q.to_string())

ma60 = hs300.rolling(60).mean()
bull = (hs300 > ma60)
tr2["bull"] = bull.reindex(tr2["formed_at"]).fillna(False).values
for lab, sub in (("多头日", tr2[tr2.bull]), ("空头日", tr2[~tr2.bull])):
    print(f"③b {lab}: below200占比 {sub.below200.mean()*100:.1f}%  均笔 {sub.avg_pnl_pct.mean():+.2f}%  "
          f"below200组均笔 {sub[sub.below200].avg_pnl_pct.mean():+.2f}%  above组 {sub[~sub.below200].avg_pnl_pct.mean():+.2f}%")
