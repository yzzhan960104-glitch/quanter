# -*- coding: utf-8 -*-
"""基准净值（沪深300 ETF 510300.SH）归一化与对齐测试。

锁定 _compute_benchmark_series 三条契约：
1) 归一化到起点 1.0（nav / 首值）
2) 三级降级全空 → 返空列表（不抛，ProChart 不画基准线）
3) 按策略 strategy_dates reindex + 前向填充（基准停牌日沿用前收，不折线断裂）
"""
from datetime import date
from unittest.mock import patch

import pandas as pd


def test_benchmark_normalizes_to_unit_start():
    """基准 close 必须归一化到起点 1.0（nav / 首值）。"""
    from server.services.backtest_service import _compute_benchmark_series

    # 模拟 ETF close：100, 102, 98 → 归一化 1.0, 1.02, 0.98
    fake_df = pd.DataFrame(
        {"close": [100.0, 102.0, 98.0]},
        index=pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"]),
    )
    strategy_dates = ["2024-01-02", "2024-01-03", "2024-01-04"]
    with patch("server.services.backtest_service.LakeDataFetcher") as MockLake:
        MockLake.return_value.fetch_ohlcv.return_value = fake_df
        bench = _compute_benchmark_series(
            start_date=date(2024, 1, 2), end_date=date(2024, 1, 4),
            strategy_dates=strategy_dates,
        )
    assert [p.date for p in bench] == strategy_dates
    assert abs(bench[0].nav - 1.0) < 1e-9          # 起点 = 1.0
    assert abs(bench[1].nav - 1.02) < 1e-9
    assert abs(bench[2].nav - 0.98) < 1e-9


def test_benchmark_empty_when_lake_missing_and_online_fails():
    """湖缺 + 在线降级也空 → 返空列表（不抛，ProChart 不画基准线）。"""
    from server.services.backtest_service import _compute_benchmark_series

    with patch("server.services.backtest_service.LakeDataFetcher") as MockLake, \
         patch("server.services.backtest_service.AKShareClient") as MockAK:
        MockLake.return_value.fetch_ohlcv.side_effect = LookupError("湖无 510300 数据")
        MockAK.return_value.fetch_daily_hist.return_value = pd.DataFrame()  # 在线也空
        bench = _compute_benchmark_series(
            start_date=date(2024, 1, 2), end_date=date(2024, 1, 4),
            strategy_dates=["2024-01-02", "2024-01-03"],
        )
    assert bench == []


def test_benchmark_reindex_forward_fills_missing_days():
    """基准按策略 strategy_dates reindex，缺失日前向填充（不折线断裂）。"""
    from server.services.backtest_service import _compute_benchmark_series

    # 基准只有 01-02、01-05 两天数据；策略日期含 01-02/03/04/05
    fake_df = pd.DataFrame(
        {"close": [100.0, 104.0]},
        index=pd.to_datetime(["2024-01-02", "2024-01-05"]),
    )
    strategy_dates = ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"]
    with patch("server.services.backtest_service.LakeDataFetcher") as MockLake:
        MockLake.return_value.fetch_ohlcv.return_value = fake_df
        bench = _compute_benchmark_series(
            start_date=date(2024, 1, 2), end_date=date(2024, 1, 5),
            strategy_dates=strategy_dates,
        )
    # 01-03、01-04 前向填充 01-02 的归一化值 1.0
    assert abs(bench[1].nav - 1.0) < 1e-9
    assert abs(bench[2].nav - 1.0) < 1e-9
    assert abs(bench[3].nav - 1.04) < 1e-9
