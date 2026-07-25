# -*- coding: utf-8 -*-
"""宏观多指标共振分析：验证多指标共振是否比单一沪深300更强（做厚薄edge）。

三指标（每信号日打标）:
  ① 沪深300趋势  — close > MA60（index_daily, 2021-07 起）
  ② 全市场宽度   — 当日站上自身MA60的股票占比 > 50%（a_shares_daily 全程算）
  ③ 融资余额趋势 — 全市场融资余额 > 其MA60（margin, 2023-07 起，杠杆扩张）

对比：单指标 / 双共振 / 三共振 的胜率/avg/止损率。
"""
import pandas as pd
import numpy as np

trades = pd.read_csv("logs/neckline_fullscan_trades.csv")
trades["signal_date"] = pd.to_datetime(trades["signal_date"])

# ① 沪深300趋势
idx = pd.read_parquet("data_lake/index_daily.parquet")
hs300 = idx.xs("000300.SH", level="symbol").sort_index()
hs300["ma60"] = hs300["close"].rolling(60, min_periods=60).mean()
trades["hs300_bull"] = trades["signal_date"].map((hs300["close"] > hs300["ma60"]).to_dict())

# ② 全市场宽度
print("算全市场宽度（每标的MA60 → 每天站上MA60比例）...")
lake = pd.read_parquet("data_lake/a_shares_daily.parquet").reset_index()
lake = lake.sort_values(["symbol", "date"])
lake["ma60"] = lake.groupby("symbol")["close"].transform(lambda x: x.rolling(60, min_periods=60).mean())
lake["above"] = lake["close"] > lake["ma60"]
width = lake.groupby("date")["above"].mean()
trades["width_bull"] = trades["signal_date"].map((width > 0.5).to_dict())

# ③ 融资余额趋势
margin = pd.read_parquet("data_lake/margin.parquet")
margin_total = margin.groupby("date")["rzye"].sum().sort_index()
margin_ma60 = margin_total.rolling(60, min_periods=60).mean()
trades["margin_up"] = trades["signal_date"].map((margin_total > margin_ma60).to_dict())


def show(label, sub):
    if len(sub) == 0:
        print(f"  {label}: 0 笔"); return
    wr = (sub["avg_pnl_pct"] > 0).mean() * 100
    sl = (sub["exit_reason"] == "stop_loss").mean() * 100
    print(f"  {label:<14}: {len(sub):>6}笔 | 胜率 {wr:>5.1f}% | avg {sub['avg_pnl_pct'].mean():+6.2f}% | 止损率 {sl:>4.0f}%")


print(f"\n=== 单指标 ===")
print("沪深300趋势:");        show("多头", trades[trades.hs300_bull == True]);   show("空头", trades[trades.hs300_bull == False])
print("全市场宽度:");          show("扩张>50%", trades[trades.width_bull == True]); show("收缩<50%", trades[trades.width_bull == False])
print("融资余额:");            show("上行", trades[trades.margin_up == True]);   show("下行", trades[trades.margin_up == False])

print(f"\n=== 双共振（沪深300多头 + 宽度扩张）===")
t2 = trades.dropna(subset=["hs300_bull", "width_bull"])
show("双多头共振", t2[(t2.hs300_bull) & (t2.width_bull)])
show("仅大盘多",   t2[(t2.hs300_bull) & (~t2.width_bull)])
show("仅宽度扩",   t2[(~t2.hs300_bull) & (t2.width_bull)])
show("双空头",     t2[(~t2.hs300_bull) & (~t2.width_bull)])

print(f"\n=== 三共振（沪深300+宽度+融资 三多头，仅 2023-07 后样本）===")
t3 = trades.dropna(subset=["hs300_bull", "width_bull", "margin_up"])
show("三共振全多", t3[(t3.hs300_bull) & (t3.width_bull) & (t3.margin_up)])
show("三共振全空", t3[(~t3.hs300_bull) & (~t3.width_bull) & (~t3.margin_up)])
show("双多无融资", t3[(t3.hs300_bull) & (t3.width_bull) & (~t3.margin_up)])
