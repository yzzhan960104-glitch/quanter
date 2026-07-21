# -*- coding: utf-8 -*-
"""全市场宽度（市场动能广度）：逐日站上 MA60 的标的比例。

四层动能评分 · ②A股动能层核心指标——宽度压缩（<40%）= 市场动能萎缩，颈线突破假突破
概率高。自算绕开 index_daily 只 2021-07 起的盲区，能覆盖 2018/2022 熊市（颈线法软肋）。

物理意图（Why 宽度）：
    颈线法在 2018/2022 熊市亏（-2.45%/-2.21%），但两类熊市特征不同：
    - 2018 钱荒型：Shibor 高 + 全市场普跌 → 宽度压缩（25%），②层双重信号可抓
    - 2022 宽货币型：Shibor 低 + 宽度不压缩（46%）→ ②层失效，须靠④微观动量共振
    本指标是②层的"广度温度计"，与流动性（Shibor/M2）共同构成 A 股系统性动能判据。

落 data_lake/market_breadth.parquet（DatetimeIndex，列 breadth=站上MA60比例）。
用法：PYTHONIOENCODING=utf-8 python -u scripts/market_breadth.py
"""
import os
import sys
import time

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

LAKE = "data_lake/a_shares_daily.parquet"
OUT = "data_lake/market_breadth.parquet"
MA_WINDOW = 60   # MA60（中期趋势线，与颈线识别窗口对齐）


def compute_breadth(lake_path: str = LAKE, ma_window: int = MA_WINDOW) -> pd.Series:
    """逐日宽度 = 站上 MA_WINDOW 的标的 / 当日有效标的（MA 非 NaN）。

    向量化：groupby symbol transform rolling（避免逐标的循环），dropna 过滤 MA 未成形期。
    """
    lake = pd.read_parquet(lake_path)  # MultiIndex(date, symbol)
    # 全市场每标的 MA（向量化 groupby transform，O(n) 不引入逐标的循环）
    lake["ma"] = lake.groupby("symbol")["close"].transform(
        lambda x: x.rolling(ma_window, min_periods=ma_window).mean()
    )
    lake["above"] = (lake["close"] > lake["ma"]).astype(int)
    # 逐日宽度：dropna 排除 MA 未成形期（前 ma_window-1 日无 MA）
    valid = lake.dropna(subset=["ma"])
    breadth = valid.groupby("date")["above"].mean().rename("breadth")
    return breadth


def main():
    t0 = time.time()
    breadth = compute_breadth()
    breadth.to_frame().to_parquet(OUT)
    print(f"全市场宽度 {len(breadth)} 日 用{time.time() - t0:.1f}s 落 {OUT}")
    print(f"范围 {breadth.index.min().date()} ~ {breadth.index.max().date()}\n")
    # 分年统计（对齐颈线法各年收益：2018-2.45%/2022-2.21% 熊市 vs 其他年正）
    print(f"{'年':>5}{'宽度均值':>10}{'宽度中位':>10}{'<30%天数占比':>14}  颈线法")
    nl = {2018: "-2.45%", 2019: "+1.72%", 2020: "+0.61%", 2021: "+0.71%",
          2022: "-2.21%", 2024: "+1.95%", 2025: "+1.71%", 2026: "+1.06%"}
    for yr in range(2016, 2027):
        b = breadth[breadth.index.year == yr]
        if not len(b):
            continue
        low_share = (b < 0.30).mean() * 100   # 宽度<30% 的天数占比（深度压缩频率）
        nlr = nl.get(yr, "-")
        print(f"{yr:>5}{b.mean() * 100:>9.0f}%{b.median() * 100:>9.0f}%{low_share:>13.0f}%  {nlr}")


if __name__ == "__main__":
    main()
