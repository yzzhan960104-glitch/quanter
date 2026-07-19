# -*- coding: utf-8 -*-
"""颈线法 v0 诊断：分步统计各关卡通过率，定位 0 命中的瓶颈。

复用 neckline_method_v0 的底层函数（compute_atr/search_neckline/local_minima），
但每步独立计数，看候选被哪一层挡掉。同时打印接近命中的案例数值，辅助判断
参数是否过严。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import pandas as pd
from neckline_method_v0 import compute_atr, search_neckline, local_minima, DEFAULTS


def main():
    lake = pd.read_parquet("data_lake/a_shares_daily.parquet")
    symbol = "000001.SZ"
    sym_df = lake.xs(symbol, level="symbol").sort_index()
    window = DEFAULTS["window"]

    # 各关卡计数
    stats = {"total": 0, "neck": 0, "bottom": 0, "breakout": 0, "vol": 0, "rr": 0, "hit": 0}
    # 密度分布采样（每 200 个时点采一次，看密度量级）
    density_samples = []
    # 接近命中的案例（过了颈线+底部，看后续卡哪）
    near_miss = []

    for i in range(window, len(sym_df)):
        sub = sym_df.iloc[: i + 1]
        stats["total"] += 1
        if len(sub) < window:
            continue
        W = sub.tail(window)
        atr_val = float(compute_atr(sub["high"], sub["low"], sub["close"]).iloc[-1])
        if pd.isna(atr_val) or atr_val <= 0:
            continue
        highs = W["high"].values
        lows = W["low"]

        # 关卡1：颈线搜索
        c_star, density = search_neckline(highs, atr_val)
        if i % 200 == 0:
            density_samples.append((W.index[-1].date(), round(density, 3), round(atr_val, 3)))
        if c_star is None or density < DEFAULTS["neck_density_min"]:
            continue
        stats["neck"] += 1

        # 关卡2：底部
        min_price = float(lows.min())
        local_lows = local_minima(lows.values, DEFAULTS["local_extrema_window"])
        bottoms = [l for l in local_lows if min_price <= l <= min_price + atr_val]
        bottom_set = {round(min_price, 4)}
        bottom_set.update(round(b, 4) for b in bottoms)
        if len(bottom_set) < DEFAULTS["min_bottoms"]:
            continue
        stats["bottom"] += 1

        # 关卡3：突破
        close_T = float(W["close"].iloc[-1])
        if close_T <= c_star:
            # 过了颈线+底部但未突破——记录 near miss
            near_miss.append((W.index[-1].date(), "未突破", round(c_star, 2), round(close_T, 2)))
            continue
        stats["breakout"] += 1

        # 关卡4：带量
        vol_T = float(W["volume"].iloc[-1])
        vol5 = float(W["volume"].tail(5).mean())
        if vol5 > 0 and vol_T < DEFAULTS["breakout_vol_mult"] * vol5:
            near_miss.append((W.index[-1].date(), "未带量", round(c_star, 2), round(close_T, 2),
                              round(vol_T / vol5, 2) if vol5 else 0))
            continue
        stats["vol"] += 1

        # 关卡5：rr
        H = c_star - min_price
        risk = close_T - min_price
        rr = (c_star + H - close_T) / risk if risk > 0 and H > 0 else -1
        if rr < DEFAULTS["min_rr"]:
            near_miss.append((W.index[-1].date(), f"rr={rr:.2f}", round(c_star, 2),
                              round(close_T, 2), round(min_price, 2)))
            continue
        stats["rr"] += 1
        stats["hit"] += 1

    print(f"标的={symbol} 窗口={window} 总时点={stats['total']}\n")
    print("=== 各关卡通过数（累计）===")
    for k in ["total", "neck", "bottom", "breakout", "vol", "rr", "hit"]:
        v = stats[k]
        pct = f"{v/stats['total']*100:.1f}%" if stats['total'] else "0"
        print(f"  {k:10s}: {v:>5}  ({pct})")

    print("\n=== 密度采样（看 neck_density_min=5% 是否合理）===")
    for d in density_samples[:15]:
        print(f"  {d[0]} 密度={d[1]} ATR={d[2]}")

    print(f"\n=== 接近命中案例（过颈线+底部，卡在后续）共 {len(near_miss)} 个，前 12 个===")
    for nm in near_miss[:12]:
        print(f"  {nm}")


if __name__ == "__main__":
    main()
