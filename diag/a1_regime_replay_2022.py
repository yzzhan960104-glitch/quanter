# -*- coding: utf-8 -*-
"""A1 回放验收：regime 闸在 2022 单边熊（应 BEAR）与 2024 结构牛（应 BULL）的历史表现。

物理意图：判据有效性实证——若 2022 年 1-4 月（HS300 单边下杀 -20%+）闸不能全 BEAR，
或 2024 下半年（9-24 行情结构牛）不能转 BULL，则阈值（MA200/0.5）需回 ADR 重议。
指数湖 2021-01 起：2022-01 时 MA200 恰有 ~244 根（边缘可行），2021 内年份不回放。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

import pandas as pd
from trading.compute.regime import classify
from trading.data_ctx import load_regime_frames

idx, daily = load_regime_frames()
rows = []
for mstart in pd.date_range("2022-01-01", "2024-12-01", freq="MS"):
    asof = (mstart + pd.offsets.MonthEnd(0)).strftime("%Y-%m-%d")
    st = classify(index_df=idx, daily_df=daily, asof=asof)
    rows.append((asof, st.state, st.reason[:46]))
for asof, state, reason in rows:
    print(f"{asof}  {state:<7} {reason}")
bear_2022_h1 = sum(1 for a, s, _ in rows if a < "2022-07" and s == "BEAR")
bear_2022_all = sum(1 for a, s, _ in rows if a < "2023-01" and s == "BEAR")
bull_2024_h2 = sum(1 for a, s, _ in rows if a >= "2024-07" and s == "BULL")
print(f"\n2022 上半年 BEAR 占 {bear_2022_h1}/6（验收 ≥5/6）")
print(f"2022 全年 BEAR 占 {bear_2022_all}/12（观测项——下半年反弹穿插属正常）")
print(f"2024 下半年 BULL 占 {bull_2024_h2}/6（验收 ≥4/6——9-24 前阴跌期 BEAR 属正确判定）")
