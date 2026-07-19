# -*- coding: utf-8 -*-
"""002882.SZ 颈线法案例详尽复现（2026-01-13 突破）。
输出：窗口区间 / 命中颈线的顶部K线日期 / 颈线压制 / 突破日 / 买入日 / 买卖止盈止损 / 后续走势。
"""
import pandas as pd
from neckline_method_v0 import compute_atr, local_minima, DEFAULTS

lake = pd.read_parquet("data_lake/a_shares_daily.parquet")
df = lake.xs("002882.SZ", level="symbol").sort_index()

sig_date = pd.Timestamp("2026-01-13")
sig_idx = df.index.get_loc(sig_date)
window = DEFAULTS["window"]
W = df.iloc[sig_idx - window + 1: sig_idx + 1]   # 窗口（含突破日）

atr_full = compute_atr(df["high"], df["low"], df["close"])
atr_val = float(atr_full.iloc[sig_idx])

print(f"========== 002882.SZ 颈线法识别（突破日 {sig_date.date()}）==========")
print(f"时间区间（窗口 {window} 天）: {W.index[0].date()} → {W.index[-1].date()}")
print(f"ATR({sig_date.date()}): {atr_val:.3f}")

# 顶部高点（局部极大，左右各3根）
highs = W["high"]
w = DEFAULTS["local_extrema_window"]
tops = []
for i in range(w, len(W) - w):
    if highs.iloc[i] >= highs.iloc[i - w:i].max() and highs.iloc[i] >= highs.iloc[i + 1:i + w + 1].max():
        tops.append((W.index[i], float(highs.iloc[i])))
print(f"\n窗口内顶部高点（局部极大）: {len(tops)} 个")
for d, p in tops:
    print(f"  {d.date()}: {p:.2f}")

# 颈线搜索：顶部聚集最多的价位
top_prices = [p for _, p in tops]
best_c, best_touches, hit_tops = None, 0, []
for c in top_prices:
    touches = sum(1 for t in top_prices if abs(t - c) <= atr_val)
    if touches > best_touches:
        best_c, best_touches, hit_tops = c, touches, [(d, p) for d, p in tops if abs(p - c) <= atr_val]
c_star = best_c
print(f"\n=== 颈线 c* = {c_star:.2f}（{best_touches} 个顶部聚集）===")
print(f"命中颈线的 K 线（顶部在 c*±ATR = {c_star:.2f}±{atr_val:.2f}）:")
for d, p in hit_tops:
    print(f"  {d.date()}: high={p:.2f} (偏离颈线 {p - c_star:+.2f})")

# 压制时长 + 底部
suppression = float((W["close"] < c_star).mean())
lows = W["low"]
min_price = float(lows.min())
H = c_star - min_price
print(f"\n压制时长: {suppression*100:.0f}% 的交易日 close < 颈线（{c_star:.2f}）")
print(f"谷底 min: {min_price:.2f}  →  H = 颈线−底 = {H:.2f}  →  H/ATR = {H/atr_val:.2f}")

# 突破
close_T = float(W["close"].iloc[-1])
print(f"\n突破日 {sig_date.date()}: close = {close_T:.2f}  >  颈线 {c_star:.2f}  ✓")

# 交易要素
buy_limit = c_star + atr_val
stop = c_star - atr_val
tp1 = c_star + H
tp2 = c_star + 2 * H
print(f"\n========== 交易要素 ==========")
print(f"  颈线 c*        : {c_star:.2f}")
print(f"  ATR            : {atr_val:.2f}")
print(f"  挂买单(颈线+ATR): {buy_limit:.2f}   ← 回踩触发即按此价成交")
print(f"  止损 (颈线−ATR) : {stop:.2f}")
print(f"  止盈1(颈线+H)   : {tp1:.2f}   ← 50% 仓")
print(f"  止盈2(颈线+2H)  : {tp2:.2f}   ← 50% 仓")

# 买入日（突破后5天内首个 low ≤ 挂单价）
buy_idx = None
for i in range(sig_idx + 1, min(sig_idx + 6, len(df))):
    if float(df["low"].iloc[i]) <= buy_limit:
        buy_idx = i
        break
print(f"\n  买入日: {df.index[buy_idx].date() if buy_idx else '未回踩'}"
      f"  （T+1 起 5 天内首个 low ≤ {buy_limit:.2f}）")
print(f"  买入价: {buy_limit:.2f}（挂单价成交）")

# 后续走势（买入后到止损/止盈2）
print(f"\n========== 买入后 K 线走势 ==========")
end_idx = min(buy_idx + 15, len(df) - 1)
print(f"{'日期':<12}{'开':>7}{'高':>7}{'低':>7}{'收':>7}  {'标记':<20}")
for i in range(buy_idx, end_idx + 1):
    row = df.iloc[i]
    marks = []
    if row["low"] <= stop:
        marks.append("触止损✗")
    if row["high"] >= tp1:
        marks.append("触止盈1")
    if row["high"] >= tp2:
        marks.append("触止盈2✓")
    print(f"{df.index[i].date()!s:<12}{row['open']:>7.2f}{row['high']:>7.2f}{row['low']:>7.2f}{row['close']:>7.2f}  {' '.join(marks)}")
    if row["low"] <= stop or row["high"] >= tp2:
        break

print(f"\n（参考：止损{stop:.2f} / 止盈1 {tp1:.2f} / 止盈2 {tp2:.2f}）")
