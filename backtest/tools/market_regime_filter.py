# -*- coding: utf-8 -*-
"""时点/市场状态过滤深挖：验证颈线法是否在特定市场状态下有效。

三层时点状态（每信号打标）:
  ① 大盘状态   — 沪深300 > MA60 = 多头环境（index_daily 仅 2021-07 起，覆盖部分样本）
  ② 个股状态   — 信号时标的 close > MA60 = 多头排列（全程覆盖）
  ③ 双共振     — 大盘 + 个股均多头

假设：颈线法（突破策略）在多头/趋势环境有效，在空头/震荡环境无效（假突破多）。
若多层状态拉开胜率/收益差距，则时点过滤是可提取的 edge。
"""
import pandas as pd
import numpy as np

trades = pd.read_csv("logs/neckline_fullscan_trades.csv")
trades["signal_date"] = pd.to_datetime(trades["signal_date"])
print(f"v3 全部信号: {len(trades)} 笔\n")


def show(label, sub):
    if len(sub) == 0:
        print(f"  {label}: 0 笔"); return
    wr = (sub["avg_pnl_pct"] > 0).mean() * 100
    print(f"  {label}: {len(sub):>6}笔 | 胜率 {wr:>5.1f}% | avg {sub['avg_pnl_pct'].mean():+6.2f}% | "
          f"止损率 {(sub['exit_reason']=='stop_loss').mean()*100:>4.0f}%")


# ① 大盘状态：沪深300 MA60
print(f"=== ① 大盘状态（沪深300 > MA60 = 多头）===")
idx = pd.read_parquet("data_lake/index_daily.parquet")
hs300 = idx.xs("000300.SH", level="symbol").sort_index()
hs300["ma60"] = hs300["close"].rolling(60, min_periods=60).mean()
trades["mkt_bull"] = trades["signal_date"].map((hs300["close"] > hs300["ma60"]).to_dict())
tagged = trades.dropna(subset=["mkt_bull"])
print(f"  (覆盖 {len(tagged)}/{len(trades)} 笔，index_daily 自 2021-07)\n")
for label, mask in [("多头环境", tagged["mkt_bull"] == True), ("空头环境", tagged["mkt_bull"] == False)]:
    show(label, tagged[mask])

# ② 个股状态：标的 MA60（向量化全程算）
print(f"\n=== ② 个股状态（信号时标的 close > MA60 = 多头排列）===")
lake = pd.read_parquet("data_lake/a_shares_daily.parquet").reset_index()
lake = lake.sort_values(["symbol", "date"])
lake["ma60"] = lake.groupby("symbol")["close"].transform(lambda x: x.rolling(60, min_periods=60).mean())
lake["above_ma60"] = lake["close"] > lake["ma60"]
trades = trades.merge(
    lake[["date", "symbol", "above_ma60"]].rename(columns={"date": "signal_date"}),
    on=["signal_date", "symbol"], how="left")
tagged2 = trades.dropna(subset=["above_ma60"])
print(f"  (覆盖 {len(tagged2)}/{len(trades)} 笔)\n")
for label, mask in [("多头排列(>MA60)", tagged2["above_ma60"] == True), ("空头排列(<MA60)", tagged2["above_ma60"] == False)]:
    show(label, tagged2[mask])

# ③ 大盘 + 个股双多头共振
print(f"\n=== ③ 大盘+个股双多头共振 ===")
tg = trades.dropna(subset=["mkt_bull"])  # merge 后的 trades（含 above_ma60）
both = tg[(tg["mkt_bull"] == True) & (tg["above_ma60"] == True)]
neither = tg[(tg["mkt_bull"] == False) & (tg["above_ma60"] == False)]
show("双多头共振", both)
show("双空头共振", neither)

# ④ 个股状态下的 exit 分布（多头排列 vs 空头排列）
print(f"\n=== ④ 个股多头排列 vs 空头排列 的 exit 分布 ===")
for label, sub in [("多头排列", tagged2[tagged2["above_ma60"] == True]),
                   ("空头排列", tagged2[tagged2["above_ma60"] == False])]:
    if len(sub) == 0: continue
    print(f"  {label} ({len(sub)}笔):")
    for r in ["stop_loss", "timeout", "tp2"]:
        g = sub[sub["exit_reason"] == r]
        if len(g):
            print(f"    {r:10s}: {len(g):>5}笔 ({len(g)/len(sub)*100:>4.0f}%) avg={g['avg_pnl_pct'].mean():+6.2f}%")
