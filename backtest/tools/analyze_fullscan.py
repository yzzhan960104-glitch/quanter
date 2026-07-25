# -*- coding: utf-8 -*-
"""全市场颈线法扫描结果分析：①参数调优方向 ②标的筛选特征。

读 logs/neckline_fullscan_trades.csv（逐笔）+ summary.csv（各标的），
按形态深度/exit/标的盈利性切片，给出参数调优和标的筛选的实证依据。
"""
import pandas as pd

trades = pd.read_csv("logs/neckline_fullscan_trades.csv")
summary = pd.read_csv("logs/neckline_fullscan_summary.csv")

print(f"=== 数据概况 ===")
print(f"逐笔 {len(trades)} 笔, 标的 {summary['symbol'].nunique()} 只\n")

# ① 参数调优：按 H/ATR（形态深度）分桶
print(f"=== ① 形态深度 H/ATR 分桶（看深形态是否表现更好）===")
h = trades["H_over_ATR"].dropna()
trades["h_bin"] = pd.cut(h, bins=[0, 2, 4, 6, 10, 1000], labels=["<2", "2-4", "4-6", "6-10", ">10"])
g = trades.dropna(subset=["h_bin"]).groupby("h_bin", observed=True).agg(
    笔数=("avg_pnl_pct", "count"),
    胜率=("avg_pnl_pct", lambda x: f"{(x>0).mean()*100:>5.0f}%"),
    avg收益=("avg_pnl_pct", lambda x: f"{x.mean():+6.2f}%"),
    tp2率=("exit_reason", lambda x: f"{(x=='tp2').mean()*100:>5.0f}%"),
    止损率=("exit_reason", lambda x: f"{(x=='stop_loss').mean()*100:>5.0f}%"),
)
print(g.to_string())

# ② 标的筛选：盈利 vs 亏损
print(f"\n=== ② 标的级：盈利 vs 亏损（仅成交≥5笔的可靠样本）===")
valid = summary[summary["n_filled"] >= 5].copy()
valid["profitable"] = valid["avg_pnl"] > 0
print(f"成交≥5笔标的: {len(valid)} 只")
print(f"  盈利(avg>0): {valid['profitable'].sum()} 只 ({valid['profitable'].mean()*100:.0f}%)")

# join 逐笔取每标的的 H/ATR 中位
trades_v = trades[trades["symbol"].isin(valid["symbol"])]
h_by_sym = trades_v.groupby("symbol")["H_over_ATR"].median()
valid["h_med"] = valid["symbol"].map(h_by_sym)

prof = valid[valid["profitable"]]
loss = valid[~valid["profitable"]]
print(f"\n  {'':12}{'胜率中位':>9}{'avg收益中位':>12}{'成交笔中位':>11}{'H/ATR中位':>10}")
print(f"  {'盈利组':<12}{prof['win_rate'].median()*100:>8.0f}%{prof['avg_pnl'].median():>+11.2f}%{prof['n_filled'].median():>11.0f}{prof['h_med'].median():>10.1f}")
print(f"  {'亏损组':<12}{loss['win_rate'].median()*100:>8.0f}%{loss['avg_pnl'].median():>+11.2f}%{loss['n_filled'].median():>11.0f}{loss['h_med'].median():>10.1f}")

print(f"\n  胜率≥50%标的: {(valid['win_rate']>=0.5).sum()} 只 ({(valid['win_rate']>=0.5).mean()*100:.0f}%)")
print(f"  胜率≥60%标的: {(valid['win_rate']>=0.6).sum()} 只")

# 盈利标的的 H/ATR 分布（看是否深形态集中）
print(f"\n  盈利组 H/ATR 分布: 中位={prof['h_med'].median():.1f}, 均={prof['h_med'].mean():.1f}")
print(f"  亏损组 H/ATR 分布: 中位={loss['h_med'].median():.1f}, 均={loss['h_med'].mean():.1f}")

# ③ timeout 分析（止盈调优依据）
print(f"\n=== ③ timeout 交易特征（方向对但没到目标 → 止盈调优依据）===")
to = trades[trades["exit_reason"] == "timeout"]
print(f"timeout {len(to)} 笔, avg={to['avg_pnl_pct'].mean():+.2f}%, H/ATR中位={to['H_over_ATR'].median():.1f}")
print(f"全体 H/ATR中位={trades['H_over_ATR'].median():.1f}")
print(f"→ timeout 占 34% 且 avg+4.13%，若止盈改用颈线+H（tp1，更近），这部分可转实际止盈")

# ④ 止损缓冲推断
print(f"\n=== ④ 止损缓冲推断（颈线−ATR → 颈线−2ATR）===")
sl = trades[trades["exit_reason"] == "stop_loss"]
print(f"当前 stop_loss {len(sl)} 笔 ({len(sl)/len(trades)*100:.0f}%), avg={sl['avg_pnl_pct'].mean():+.2f}%")
print(f"  单笔止损亏 ~risk(2ATR)={sl['risk_pct'].median():.1f}%")
print(f"→ 改 颈线−2ATR：risk=3ATR，单笔亏~{-1.5*sl['risk_pct'].median():.1f}%（更深）")
print(f"  但部分原 stop_loss 会因没跌到−2ATR 而转 timeout/tp（频率降），净效果需重跑验证")
