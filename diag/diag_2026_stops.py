# -*- coding: utf-8 -*-
"""2026 年止损交易详尽日志：每笔的突破价/买入价/止损价/持有天数/期间高低。
供用户分析止损原因（追高 / 快速回撤 / 磨到死）。
"""
import pandas as pd

t = pd.read_csv("logs/neckline_fullscan_trades.csv")
t["signal_date"] = pd.to_datetime(t["signal_date"])
t["buy_date"] = pd.to_datetime(t["buy_date"])
sl26 = t[(t["signal_date"].dt.year == 2026) & (t["exit_reason"] == "stop_loss")].reset_index(drop=True)
print(f"2026 年止损交易: {len(sl26)} 笔（v3 全市场，H/ATR≤4 + 止盈2H）\n")

lake = pd.read_parquet("data_lake/a_shares_daily.parquet")

rows = []
for _, r in sl26.iterrows():
    sym = r["symbol"]
    try:
        sdf = lake.xs(sym, level="symbol").sort_index()
    except Exception:
        continue
    sig_date, buy_date = r["signal_date"], r["buy_date"]
    if pd.isna(buy_date):
        continue
    try:
        sig_idx = sdf.index.get_loc(sig_date)
        buy_idx = sdf.index.get_loc(buy_date)
    except Exception:
        continue
    entry = r["entry"]
    risk_pct = r["risk_pct"] / 100
    risk = risk_pct * entry            # = 2·ATR
    atr = risk / 2
    stop = entry - risk                # = 颈线 − ATR
    neckline = r["neckline"]
    sig_close = float(sdf["close"].iloc[sig_idx])

    # 复现止损触发日（买入后首个 low ≤ stop）
    stop_day = None
    for i in range(buy_idx, min(buy_idx + 15, len(sdf))):
        if float(sdf["low"].iloc[i]) <= stop:
            stop_day = i
            break
    if stop_day is None:
        continue
    hold = stop_day - buy_idx
    seg = sdf.iloc[buy_idx: stop_day + 1]
    max_high = float(seg["high"].max())
    min_low = float(seg["low"].min())
    stop_date = sdf.index[stop_day].date()

    # 买入价相对颈线：追高程度 = (entry - neckline) / neckline
    chase = (entry - neckline) / neckline * 100
    # 期间最大浮盈（从买入到止损前的最高）
    max_favor = (max_high - entry) / entry * 100

    rows.append({
        "symbol": sym, "突破日": sig_date.date(), "买入日": buy_date.date(),
        "止损日": stop_date, "持有天": hold,
        "颈线": round(neckline, 2), "ATR": round(atr, 2), "止损价": round(stop, 2),
        "买入价": round(entry, 2), "突破close": round(sig_close, 2),
        "追高%": round(chase, 1), "期间最高": round(max_high, 2),
        "最大浮盈%": round(max_favor, 1), "期间最低": round(min_low, 2),
        "H/ATR": r["H_over_ATR"], "亏损%": r["avg_pnl_pct"],
    })

out = pd.DataFrame(rows)
pd.set_option("display.width", 250)
pd.set_option("display.max_columns", 20)
print(out.to_string(index=False))
print(f"\n=== 汇总 ===")
print(f"平均持有天数: {out['持有天'].mean():.1f} 天")
print(f"追高% 中位: {out['追高%'].median():.1f}%  （买入价离颈线多远）")
print(f"最大浮盈% 中位: {out['最大浮盈%'].median():.1f}%  （止损前最大正向波动）")
print(f"  其中浮盈始终<1%（买入即套/磨蹭死）: {(out['最大浮盈%']<1).sum()} 笔")
print(f"  其中浮盈曾>3%（给过肉又吐回）: {(out['最大浮盈%']>3).sum()} 笔")
out.to_csv("logs/diag_2026_stops.csv", index=False, encoding="utf-8-sig")
print(f"\n详表 → logs/diag_2026_stops.csv")
