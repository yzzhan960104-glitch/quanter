# -*- coding: utf-8 -*-
"""颈线层深挖：突破质量 + 颈线压制强度 vs 真假突破。

核心痛点：全市场 50-54% 止损率（假突破多）。若突破日量能/颈线压制能区分真假突破，
就能加过滤降止损率。采样 top200，scan_symbol 已附 breakout_vol_ratio/suppression。
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pandas as pd
from neckline_backtest import scan_symbol

lake = pd.read_parquet("data_lake/a_shares_daily.parquet")
syms = lake.index.get_level_values("symbol").unique().tolist()
amt = {}
for s in syms:
    try:
        amt[s] = float(lake.xs(s, level="symbol")["amount"].tail(30).mean())
    except Exception:
        amt[s] = 0.0
top = sorted(amt, key=amt.get, reverse=True)[:200]
print(f"采样 top200，重跑（含突破质量特征）...")

all_filled = []
for sym in top:
    try:
        sym_df = lake.xs(sym, level="symbol").sort_index()
        filled, _, _ = scan_symbol(sym_df, 60)
        for r in filled:
            r["symbol"] = sym
        all_filled.extend(filled)
    except Exception:
        continue

df = pd.DataFrame(all_filled)
print(f"成交 {len(df)} 笔\n")


def show(label, sub):
    if len(sub) == 0:
        print(f"  {label}: 0"); return
    wr = (sub["avg_pnl_pct"] > 0).mean() * 100
    sl = (sub["exit_reason"] == "stop_loss").mean() * 100
    tp = (sub["exit_reason"] == "tp2").mean() * 100
    print(f"  {label:<14}: {len(sub):>5}笔 | 胜率 {wr:>5.1f}% | avg {sub['avg_pnl_pct'].mean():+6.2f}% | 止损率 {sl:>4.0f}% | tp2率 {tp:>4.0f}%")


print("=== ① 突破量比（突破日量 / 近5日均量）vs 盈亏 ===")
df["vb"] = pd.cut(df["breakout_vol_ratio"], bins=[0, 1.5, 2, 3, 1000], labels=["<1.5", "1.5-2", "2-3", ">3"])
for name, g in df.groupby("vb", observed=True):
    show(str(name), g)

print("\n=== ② 颈线压制时长（close<颈线比例）vs 盈亏 ===")
df["sb"] = pd.cut(df["suppression"], bins=[0, 0.7, 0.8, 0.9, 1.01], labels=["<0.7", "0.7-0.8", "0.8-0.9", ">0.9"])
for name, g in df.groupby("sb", observed=True):
    show(str(name), g)

print("\n=== ③ 真假突破的特征中位（tp2 vs stop_loss vs timeout）===")
for r in ["tp2", "stop_loss", "timeout"]:
    sub = df[df["exit_reason"] == r]
    if len(sub) == 0:
        continue
    print(f"  {r:10s}: 突破量比 {sub['breakout_vol_ratio'].median():.2f} | "
          f"压制 {sub['suppression'].median():.2f} | H/ATR {sub['H_over_ATR'].median():.1f}")

print("\n=== ④ 高突破质量（量比>2 且 压制>0.8）的组合 ===")
hq = df[(df["breakout_vol_ratio"] > 2) & (df["suppression"] > 0.8)]
show("高突破质量", hq)
show("其余", df[~((df["breakout_vol_ratio"] > 2) & (df["suppression"] > 0.8))])
