# -*- coding: utf-8 -*-
"""定点诊断：平安银行 2024 年颈线法为什么没识别。
对 2024 年关键时点、不同窗口，打印颈线搜索中间结果（c/压制/触及/突破）。
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pandas as pd
from strategies.neckline.method_v0 import compute_atr, search_neckline

lake = pd.read_parquet("data_lake/a_shares_daily.parquet")
df = lake.xs("000001.SZ", level="symbol").sort_index()

print("=== 平安银行 2024 年颈线搜索诊断 ===\n")
for t in ["2024-08-19", "2024-09-20", "2024-09-30", "2024-10-15", "2024-10-31"]:
    for window in [60, 120]:
        sub = df.loc[:t]
        if len(sub) < window:
            continue
        W = sub.tail(window)
        atr_val = float(compute_atr(sub["high"], sub["low"], sub["close"]).iloc[-1])
        highs = W["high"].values
        closes_w = W["close"].values
        close_T = float(W["close"].iloc[-1])
        # 严格（当前参数：≥2 触及，≥0.6 压制）
        c_strict, supp_strict = search_neckline(highs, closes_w, atr_val, 2, 0.6)
        # 放松压制到 0（看最优颈线长啥样）
        c_loose, supp_loose = search_neckline(highs, closes_w, atr_val, 2, 0.0)
        if c_loose is not None:
            touches = int(((highs >= c_loose - atr_val) & (highs <= c_loose + atr_val)).sum())
            breakout = close_T > c_loose
            print(f"{t} win={window:>3} ATR={atr_val:.3f} close={close_T:.3f}")
            print(f"   严格(≥2触,≥0.6压): c={c_strict}, supp={round(supp_strict,3) if c_strict else None}")
            print(f"   放松(≥2触,≥0压):   c={round(c_loose,3)}, supp={round(supp_loose,3)}, 触及={touches}, 突破={breakout}")
        else:
            print(f"{t} win={window}: 连放松阈值都找不到 ≥2 触及的颈线")
    print()
