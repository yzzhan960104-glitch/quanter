# -*- coding: utf-8 -*-
"""验证假设：颈线法不适合大盘蓝筹股。
用 index_member 的上证50 + 沪深300 最新成分代表大盘蓝筹，
对比蓝筹 vs 非蓝筹在颈线法下的胜率/收益/exit 分布。
"""
import pandas as pd

m = pd.read_parquet("data_lake/index_member.parquet")
summary = pd.read_csv("logs/neckline_fullscan_summary.csv")
trades = pd.read_csv("logs/neckline_fullscan_trades.csv")

# 蓝筹 = 上证50 + 沪深300 最新成分
blue = set()
for idx_code, idx_name in [("000016.SH", "上证50"), ("000300.SH", "沪深300")]:
    sub = m[m["index_code"] == idx_code]
    latest = sub.index.get_level_values("date").max()
    members = set(sub.xs(latest, level="date")["con_code"])
    blue.update(members)
    print(f"{idx_name}({idx_code}) 最新成分: {len(members)} 只")
print(f"蓝筹并集: {len(blue)} 只\n")

summary["blue"] = summary["symbol"].isin(blue)
trades["blue"] = trades["symbol"].isin(blue)

print(f"=== 标的级（成交≥5笔）: 蓝筹 vs 非蓝筹 ===")
v = summary[summary["n_filled"] >= 5]
for label, sub in [("蓝筹(50+300)", v[v["blue"]]), ("非蓝筹", v[~v["blue"]])]:
    if len(sub) == 0:
        continue
    prof = (sub["avg_pnl"] > 0).mean() * 100
    print(f"  {label:<14}: {len(sub):>4}只 | 胜率中位 {sub['win_rate'].median()*100:>3.0f}% | "
          f"avg中位 {sub['avg_pnl'].median():+6.2f}% | 盈利占比 {prof:>3.0f}%")

print(f"\n=== 逐笔层面: 蓝筹 vs 非蓝筹 ===")
for label, sub in [("蓝筹", trades[trades["blue"]]), ("非蓝筹", trades[~trades["blue"]])]:
    if len(sub) == 0:
        continue
    wr = (sub["avg_pnl_pct"] > 0).mean() * 100
    print(f"  {label:<8}: {len(sub):>6}笔 | 胜率 {wr:>3.0f}% | avg {sub['avg_pnl_pct'].mean():+6.2f}% | "
          f"H/ATR中位 {sub['H_over_ATR'].median():.1f}")

print(f"\n=== exit 分布对比（蓝筹）===")
bc = trades[trades["blue"]]
tot = len(bc)
for reason in ["stop_loss", "timeout", "tp2"]:
    g = bc[bc["exit_reason"] == reason]
    if len(g):
        print(f"  {reason:10s}: {len(g):>4}笔 ({len(g)/tot*100:>3.0f}%)  avg={g['avg_pnl_pct'].mean():+6.2f}%")

print(f"\n=== exit 分布对比（非蓝筹）===")
nc = trades[~trades["blue"]]
tot = len(nc)
for reason in ["stop_loss", "timeout", "tp2"]:
    g = nc[nc["exit_reason"] == reason]
    if len(g):
        print(f"  {reason:10s}: {len(g):>4}笔 ({len(g)/tot*100:>3.0f}%)  avg={g['avg_pnl_pct'].mean():+6.2f}%")
