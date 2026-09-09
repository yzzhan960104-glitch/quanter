# -*- coding: utf-8 -*-
"""颈线法×大盘 相关性与状态依赖分析(2026-09-05)。

问题(用户):颈线法是否只适用于相对牛市,与大盘走势高度正相关?
分析面:
  ① 年度对齐:逐年 笔数/胜率/均笔 vs 指数年收益(沪深300/中证1000)
  ② 月度相关:策略月度均笔(等权信号池)与 指数同月/次月收益 的 Pearson
  ③ 状态条件(决策时可用):信号日 指数>MA60(多头月) vs <MA60 的期望差
  ④ 构成通道:月度「信号在 MA200 下方占比」vs 指数状态(池构成随行情漂移)
  ⑤ 机制:逐年 stop_loss 占比(假突破率=行情状态的最直接指纹)
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

# ── 指数 ──
idx = pd.read_parquet("data_lake/index_daily.parquet", columns=["close"])
def series(sym):
    s = idx.xs(sym, level="symbol")["close"].sort_index()
    return s
hs300, csi1000 = series("000300.SH"), series("000852.SH")
m300 = hs300.resample("ME").last()
m300_ret = m300.pct_change()
m1000_ret = csi1000.resample("ME").last().pct_change()

# ── ① 年度对齐 ──
tr["year"] = tr["formed_at"].dt.year
y = tr.groupby("year")["avg_pnl_pct"]
ydf = pd.DataFrame({
    "n": y.size(),
    "胜率%": (tr.assign(w=tr.avg_pnl_pct > 0).groupby("year")["w"].mean() * 100).round(1),
    "均笔%": y.mean().round(2),
    "止损占比%": (tr.assign(s=tr.exit_reason == "stop_loss").groupby("year")["s"].mean() * 100).round(1),
    "tp2占比%": (tr.assign(t=tr.exit_reason == "tp2").groupby("year")["t"].mean() * 100).round(1),
})
for name, s in (("沪深300", hs300), ("中证1000", csi1000)):
    yr = s.resample("YE").last().pct_change() * 100
    ydf[f"{name}年%"] = yr.reindex(ydf.index.map(lambda x: f"{x}-12-31")).values.round(1)
print("== ① 年度对齐 ==")
print(ydf.to_string())

# ── ② 月度相关 ──
g = tr.groupby("ym")["avg_pnl_pct"]
mon = pd.DataFrame({"n": g.size(), "avg": g.mean(), "sum": g.sum()})
mon.index = pd.to_datetime(mon.index + "-28")
for lag, col in ((0, "同月"), (1, "次月")):
    r300 = m300_ret.reindex(mon.index)
    r_next = m300_ret.shift(-lag).reindex(mon.index)
    r1000 = m1000_ret.reindex(mon.index)
    ok = r300.notna() & mon["avg"].notna()
    c1 = np.corrcoef(mon["avg"][ok], r300[ok])[0, 1]
    c2 = np.corrcoef(mon["avg"][ok], r1000[ok])[0, 1]
    print(f"② 月度均笔 vs 指数{col}收益: corr(沪深300)={c1:+.3f}  corr(中证1000)={c2:+.3f}  (n={ok.sum()}月)")

# ── ③ 状态条件:信号日指数 vs MA60(决策时可用) ──
ma60_300 = hs300.rolling(60).mean()
bull = (hs300 > ma60_300)
tr_state = tr.set_index("formed_at")
tr_state["bull"] = bull.reindex(tr_state.index).fillna(False).values
for lab, sub in (("指数>MA60(多头日)", tr_state[tr_state.bull]), ("指数<MA60(空头日)", tr_state[~tr_state.bull])):
    print(f"③ {lab:<18} n={len(sub):>6} 胜率{(sub.avg_pnl_pct>0).mean()*100:>5.1f}% 均笔{sub.avg_pnl_pct.mean():+6.2f}% 止损占比{(sub.exit_reason=='stop_loss').mean()*100:.1f}%")

# ── ④ 构成通道:月度 below-MA200 信号占比 vs 指数月收益 ──
lake = pd.read_parquet("data_lake/a_shares_daily.parquet",
                       filters=[("date", ">=", pd.Timestamp("2019-06-01"))], columns=["close"])
lake = lake[lake.index.get_level_values("symbol").isin(tr["symbol"].unique())]
rows = []
for sym, gdf in lake.groupby(level="symbol", sort=False):
    gdf = gdf.droplevel("symbol").sort_index()
    ma = gdf["close"].rolling(200, min_periods=200).mean()
    rows.append(pd.DataFrame({"symbol": sym, "date": gdf.index, "below200": gdf["close"].values < ma.values}))
mg = pd.concat(rows)
tr2 = tr.merge(mg, left_on=["symbol", "formed_at"], right_on=["symbol", "date"], how="left")
frac = tr2.groupby("ym")["below200"].mean()
frac.index = pd.to_datetime(frac.index + "-28")
ok = frac.notna() & m300_ret.notna()
c = np.corrcoef(frac[ok], m300_ret[ok])[0, 1]
print(f"④ 月度「信号在MA200下方占比」 vs 沪深300同月收益: corr={c:+.3f}")
comp = pd.DataFrame({"below200占比": (frac * 100).round(0), "hs300月%": (m300_ret * 100).round(1)}).dropna()
print(comp.resample("QE").mean().round(1).to_string())
