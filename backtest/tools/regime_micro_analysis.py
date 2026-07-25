# -*- coding: utf-8 -*-
"""层2+3：①最佳组合分年度稳健性 ②微观 HV/流动性换维度 ③时点×标的交叉。

层2：双多+融资去杠杆（沪深300多+宽度扩+融资下行）分年度，验证 +0.71% 是否稳健。
层3：微观 HV（波动率）/流动性 amount30d 分桶，及多头环境下 HV 高低差异（交叉）。
"""
import pandas as pd
import numpy as np

trades = pd.read_csv("logs/neckline_fullscan_trades.csv")
trades["signal_date"] = pd.to_datetime(trades["signal_date"])
trades["year"] = trades["signal_date"].dt.year

# 宏观标签（复用 macro 逻辑）
idx = pd.read_parquet("data_lake/index_daily.parquet")
hs300 = idx.xs("000300.SH", level="symbol").sort_index()
hs300["ma60"] = hs300["close"].rolling(60, min_periods=60).mean()
trades["hs300_bull"] = trades["signal_date"].map((hs300["close"] > hs300["ma60"]).to_dict())

print("算宽度 + 微观 HV/流动性...")
lake = pd.read_parquet("data_lake/a_shares_daily.parquet").reset_index().sort_values(["symbol", "date"])
lake["ma60"] = lake.groupby("symbol")["close"].transform(lambda x: x.rolling(60, min_periods=60).mean())
lake["above"] = lake["close"] > lake["ma60"]
width = lake.groupby("date")["above"].mean()
trades["width_bull"] = trades["signal_date"].map((width > 0.5).to_dict())

margin = pd.read_parquet("data_lake/margin.parquet")
mt = margin.groupby("date")["rzye"].sum().sort_index()
mt_ma = mt.rolling(60, min_periods=60).mean()
trades["margin_up"] = trades["signal_date"].map((mt > mt_ma).to_dict())

# 微观特征：HV(20日) + amount30d（每信号日，向量化按标的）
lake["ret"] = lake.groupby("symbol")["close"].pct_change()
lake["hv"] = lake.groupby("symbol")["ret"].transform(lambda x: x.rolling(20).std() * np.sqrt(252))
lake["amt30"] = lake.groupby("symbol")["amount"].transform(lambda x: x.rolling(30).mean())
trades = trades.merge(
    lake[["date", "symbol", "hv", "amt30"]].rename(columns={"date": "signal_date"}),
    on=["signal_date", "symbol"], how="left")


def show(label, sub):
    if len(sub) == 0:
        print(f"  {label}: 0 笔"); return
    wr = (sub["avg_pnl_pct"] > 0).mean() * 100
    sl = (sub["exit_reason"] == "stop_loss").mean() * 100
    print(f"  {label:<16}: {len(sub):>6}笔 | 胜率 {wr:>5.1f}% | avg {sub['avg_pnl_pct'].mean():+6.2f}% | 止损率 {sl:>4.0f}%")


# ===== 层2：最佳组合分年度稳健性 =====
print(f"\n=== 层2：最佳组合（沪深300多+宽度扩+融资下行）分年度 ===")
best = trades[(trades.hs300_bull == True) & (trades.width_bull == True) & (trades.margin_up == False)]
for yr, g in best.groupby("year"):
    show(f"{yr}年", g)
show("合计", best)

# ===== 层3a：微观 HV 分桶 =====
print(f"\n=== 层3a：按个股 HV（20日年化波动率）分桶 ===")
v = trades.dropna(subset=["hv"])
v["hv_bin"] = pd.cut(v["hv"], bins=[0, 0.3, 0.4, 0.5, 0.7, 5], labels=["<30%", "30-40%", "40-50%", "50-70%", ">70%"])
for name, g in v.groupby("hv_bin", observed=True):
    show(str(name), g)

# ===== 层3b：流动性 amount30d 分桶（amount 千元；1亿=1e5千元）=====
print(f"\n=== 层3b：按流动性 amount30d 分桶 ===")
v["amt_bin"] = pd.cut(v["amt30"], bins=[0, 1e5, 5e5, 2e6, 1e9], labels=["<1亿", "1-5亿", "5-20亿", ">20亿"])
for name, g in v.groupby("amt_bin", observed=True):
    show(str(name), g)

# ===== 层3c：时点×标的交叉（多头环境下 HV/流动性 高低）=====
print(f"\n=== 层3c：多头环境下（沪深300多）的 HV 高低差异 ===")
bull = v[v.hs300_bull == True]
show("多头+低HV(<40%)", bull[bull.hv < 0.4])
show("多头+高HV(>50%)", bull[bull.hv > 0.5])

print(f"\n=== 层3d：多头环境下 流动性 高低差异 ===")
show("多头+高流动性(>5亿)", bull[bull.amt30 > 5e5])
show("多头+低流动性(<2亿)", bull[bull.amt30 < 2e5])
