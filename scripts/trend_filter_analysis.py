# -*- coding: utf-8 -*-
"""趋势性筛选关键参数分析：盈利标的 vs 亏损标的 的标的特征差异。

特征（每标的全历史算）:
  年化波动率 vol      — 收益率 std × √252（稳定性）
  年化收益率 ann_ret  — 长期涨跌（方向）
  趋势 R²             — log 价格 vs 时间的线性相关²（趋势线性度，高=长期稳定趋势）

目标：找出颈线法盈利标的的共性特征 → 给出趋势性筛选规则。
注：summary.csv 当前是 v2（H/ATR≤4 版），标的有效性结论与 v1 一致且更干净。
"""
import pandas as pd
import numpy as np

summary = pd.read_csv("logs/neckline_fullscan_summary.csv")
lake = pd.read_parquet("data_lake/a_shares_daily.parquet")

print("计算各标的趋势/波动特征...")
feats = []
for sym in summary["symbol"]:
    try:
        df = lake.xs(sym, level="symbol")
        ret = df["close"].pct_change().dropna()
        vol = float(ret.std() * np.sqrt(252))
        ann_ret = float((df["close"].iloc[-1] / df["close"].iloc[0]) ** (252 / len(df)) - 1)
        y = np.log(df["close"].values)
        x = np.arange(len(y))
        r2 = float(np.corrcoef(x, y)[0, 1] ** 2) if len(y) > 2 else 0.0
        feats.append({"symbol": sym, "vol": vol, "ann_ret": ann_ret, "trend_r2": r2})
    except Exception:
        continue
feats = pd.DataFrame(feats)
df = summary.merge(feats, on="symbol")

v = df[df["n_filled"] >= 5].copy()
v["profitable"] = v["avg_pnl"] > 0
prof = v[v["profitable"]]
loss = v[~v["profitable"]]

print(f"\n=== 标的级（成交≥5笔）{len(v)} 只：盈利 {len(prof)} / 亏损 {len(loss)} ===")
print(f"\n{'特征':<12}{'盈利组中位':>12}{'亏损组中位':>12}{'方向':>10}")
for col, name in [("vol", "年化波动率"), ("ann_ret", "年化收益率"), ("trend_r2", "趋势R²"), ("win_rate", "胜率")]:
    pm, lm = prof[col].median(), loss[col].median()
    diff = "盈利↑" if pm > lm else "盈利↓"
    print(f"  {name:<10}{pm:>12.3f}{lm:>12.3f}{diff:>10}")

print(f"\n=== 按趋势R²分桶 ===")
v["r2_bin"] = pd.cut(v["trend_r2"], bins=[-0.01, 0.3, 0.6, 0.8, 1.01],
                     labels=["<0.3", "0.3-0.6", "0.6-0.8", ">0.8"])
g = v.groupby("r2_bin", observed=True).agg(
    只数=("symbol", "count"),
    盈利占比=("profitable", lambda x: f"{x.mean()*100:>4.0f}%"),
    avg收益中位=("avg_pnl", lambda x: f"{x.median():+6.2f}%"),
    胜率中位=("win_rate", lambda x: f"{x.median()*100:>4.0f}%"),
)
print(g.to_string())

print(f"\n=== 按年化波动率分桶 ===")
v["vol_bin"] = pd.cut(v["vol"], bins=[0, 0.3, 0.4, 0.5, 1.0], labels=["<30%", "30-40%", "40-50%", ">50%"])
g = v.groupby("vol_bin", observed=True).agg(
    只数=("symbol", "count"),
    盈利占比=("profitable", lambda x: f"{x.mean()*100:>4.0f}%"),
    avg收益中位=("avg_pnl", lambda x: f"{x.median():+6.2f}%"),
    胜率中位=("win_rate", lambda x: f"{x.median()*100:>4.0f}%"),
)
print(g.to_string())

print(f"\n=== 按年化收益率分桶（长期涨跌方向）===")
v["ret_bin"] = pd.cut(v["ann_ret"], bins=[-1.1, 0, 0.1, 0.3, 5], labels=["<0%(跌)", "0-10%", "10-30%", ">30%"])
g = v.groupby("ret_bin", observed=True).agg(
    只数=("symbol", "count"),
    盈利占比=("profitable", lambda x: f"{x.mean()*100:>4.0f}%"),
    avg收益中位=("avg_pnl", lambda x: f"{x.median():+6.2f}%"),
    胜率中位=("win_rate", lambda x: f"{x.median()*100:>4.0f}%"),
)
print(g.to_string())

# 组合筛选：找盈利占比最高的特征组合
print(f"\n=== 组合：趋势R²>0.6 且 波动率<40% 的标的 ===")
combo = v[(v["trend_r2"] > 0.6) & (v["vol"] < 0.4)]
if len(combo):
    print(f"  {len(combo)} 只 | 盈利占比 {(combo['avg_pnl']>0).mean()*100:.0f}% | "
          f"avg收益中位 {combo['avg_pnl'].median():+.2f}% | 胜率中位 {combo['win_rate'].median()*100:.0f}%")
