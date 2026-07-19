# -*- coding: utf-8 -*-
"""2026 止损代表性案例复现：给肉吐回（浮盈高）vs 买入即套（浮盈低）。
从 v5 trades 筛 2026+stop_loss，算每笔浮盈，挑两类极端，详细复现走势。
"""
import pandas as pd
from neckline_method_v0 import compute_atr

lake = pd.read_parquet("data_lake/a_shares_daily.parquet")
t = pd.read_csv("logs/neckline_fullscan_trades.csv")
t["signal_date"] = pd.to_datetime(t["signal_date"])
sl = t[(t["signal_date"].dt.year == 2026) & (t["exit_reason"] == "stop_loss")].reset_index(drop=True)


def max_favor(row):
    """算每笔止损前的最大浮盈%（从买入到止损）。"""
    sym = row["symbol"]
    try:
        df = lake.xs(sym, level="symbol").sort_index()
        buy_idx = df.index.get_loc(pd.to_datetime(row["buy_date"]))
        entry = row["entry"]
        risk = row["risk_pct"] / 100 * entry
        stop = entry - risk
        for i in range(buy_idx, min(buy_idx + 15, len(df))):
            if df["low"].iloc[i] <= stop:
                return (df["high"].iloc[buy_idx:i + 1].max() - entry) / entry * 100, i - buy_idx
    except Exception:
        pass
    return None, None


# 算浮盈
sl["favor"], sl["hold"] = zip(*sl.apply(max_favor, axis=1))
sl = sl.dropna(subset=["favor"])
# 挑给肉吐回（浮盈>5%）+ 买入即套（浮盈<0）各 2
give_back = sl[sl["favor"] > 5].nlargest(2, "favor")
instant_loss = sl[sl["favor"] < 0].nsmallest(2, "favor")


def replay(sym, sig_date_str):
    sig_date = pd.Timestamp(sig_date_str)
    df = lake.xs(sym, level="symbol").sort_index()
    sig_idx = df.index.get_loc(sig_date)
    W = df.iloc[sig_idx - 59: sig_idx + 1]
    atr_val = float(compute_atr(df["high"], df["low"], df["close"], window=60).iloc[sig_idx])
    highs = W["high"]
    tops = [float(highs.iloc[i]) for i in range(3, len(W) - 3)
            if highs.iloc[i] >= highs.iloc[i - 3:i].max() and highs.iloc[i] >= highs.iloc[i + 1:i + 4].max()]
    best_c, best_t = max(((c, sum(1 for x in tops if abs(x - c) <= atr_val)) for c in tops), key=lambda x: x[1])
    c_star, supp = best_c, (W["close"] < best_c).mean()
    min_price = float(W["low"].min())
    H = c_star - min_price
    buy_limit, stop = c_star + atr_val, c_star - atr_val
    tp1, tp2 = c_star + H, c_star + 2 * H
    buy_idx = None
    for i in range(sig_idx + 1, min(sig_idx + 6, len(df))):
        if df["low"].iloc[i] <= buy_limit:
            buy_idx = i; break
    print(f"  颈线{c_star:.2f} ATR{atr_val:.2f} 压制{supp*100:.0f}% 底{min_price:.2f} H/ATR{H/atr_val:.2f}")
    print(f"  突破close {df['close'].iloc[sig_idx]:.2f} | 买入{buy_limit:.2f}@{df.index[buy_idx].date() if buy_idx else '-'} | 止损{stop:.2f} 止盈1{tp1:.2f} 止盈2{tp2:.2f}")
    print(f"  买入后走势:")
    end_idx = min(buy_idx + 15, len(df) - 1)
    for i in range(buy_idx, end_idx + 1):
        row = df.iloc[i]
        m = ""
        if row["low"] <= stop: m = " ◀触止损"
        elif row["high"] >= tp2: m = " ◀触止盈2"
        elif row["high"] >= tp1: m = " ◀触止盈1"
        print(f"    {df.index[i].date()} O{row['open']:.2f} H{row['high']:.2f} L{row['low']:.2f} C{row['close']:.2f}{m}")
        if m and ("止损" in m or "止盈2" in m): break


for label, cases in [("【给肉吐回】浮盈曾>5% 但跌回止损", give_back),
                     ("【买入即套】浮盈<0 买入即被套", instant_loss)]:
    print(f"\n================= {label} =================")
    for _, r in cases.iterrows():
        print(f"\n>>> {r['symbol']} 突破{r['signal_date'].date()} | 浮盈峰值{r['favor']:+.1f}% 持有{r['hold']}天 亏损{r['avg_pnl_pct']}%")
        replay(r["symbol"], r["signal_date"].strftime("%Y-%m-%d"))
