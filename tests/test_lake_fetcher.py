"""LakeDataFetcher 单元测试（回测主链路切真实数据湖的契约守护）。

覆盖：
- fetch_ohlcv 真实 symbol → 返回湖时序（与 parquet 一致）
- fetch_ohlcv 缺数据 → 抛 LookupError（service 据此降级 Mock）
- fetch_macro indicator → macro 湖中文列名映射
- dynamic_top50 → daily_active 湖活跃池首只路由
- daily_active 缺失 → dynamic_top50 抛 LookupError

隔离：用 tmp_path 临时 parquet 注入 DataLakeReader 单例，yield 后清空，不污染其他测试。
"""
from datetime import datetime

import pandas as pd
import pytest

from data.lake_reader import DataLakeReader
from data.lake_fetcher import LakeDataFetcher


@pytest.fixture
def loaded_reader(tmp_path):
    """临时建 daily/macro/daily_active 湖，注入 DataLakeReader 单例。

    三个湖的 schema 严格对齐 lake_reader.load 的分流：
    - daily / daily_active: MultiIndex(date, symbol)
    - macro: DatetimeIndex（单序列）
    """
    reader = DataLakeReader.get_instance()
    # 清空单例缓存（隔离测试，防其他测试残留）
    reader._lakes.clear()
    reader._ffills.clear()
    reader._dtypes.clear()
    reader._default_key = None

    dates = pd.date_range("2025-01-01", periods=3, freq="D")

    # daily 湖：2 只 × 3 天（600000.SH + 600010.SH）
    daily = pd.DataFrame(
        {
            "open":   [10, 11, 12,  20, 21, 22],
            "high":   [11, 12, 13,  21, 22, 23],
            "low":    [9,  10, 11,  19, 20, 21],
            "close":  [10.5, 11.5, 12.5,  20.5, 21.5, 22.5],
            "volume": [1000, 1100, 1200,  2000, 2100, 2200],
        },
        index=pd.MultiIndex.from_tuples(
            [
                (dates[0], "600000.SH"), (dates[1], "600000.SH"), (dates[2], "600000.SH"),
                (dates[0], "600010.SH"), (dates[1], "600010.SH"), (dates[2], "600010.SH"),
            ],
            names=["date", "symbol"],
        ),
    )
    daily_path = tmp_path / "daily.parquet"
    daily.to_parquet(daily_path)
    reader.load(str(daily_path), key="daily")

    # macro 湖：单序列（M2同比增长），DatetimeIndex
    macro = pd.DataFrame({"M2同比增长": [8.0, 8.1, 8.2]}, index=dates)
    macro.index.name = "date"
    macro_path = tmp_path / "macro.parquet"
    macro.to_parquet(macro_path)
    reader.load(str(macro_path), key="macro")

    # daily_active 湖：仅 600010.SH（活跃池，验证 dynamic_top50 路由）
    active = daily.xs("600010.SH", level="symbol").reset_index()
    active["symbol"] = "600010.SH"
    active = active.set_index(["date", "symbol"])
    active_path = tmp_path / "active.parquet"
    active.to_parquet(active_path)
    reader.load(str(active_path), key="daily_active")

    yield reader

    # 清理单例，防污染后续测试
    reader._lakes.clear()
    reader._ffills.clear()
    reader._dtypes.clear()
    reader._default_key = None


def test_fetch_ohlcv_real_symbol_returns_lake_data(loaded_reader):
    """真实 symbol → 返回湖时序，值与 parquet 一致。"""
    f = LakeDataFetcher()
    df = f.fetch_ohlcv("600000.SH", datetime(2025, 1, 1), datetime(2025, 1, 3), freq="1d")
    assert len(df) == 3
    assert df["close"].iloc[0] == 10.5
    assert df["close"].iloc[-1] == 12.5
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]


def test_fetch_ohlcv_missing_symbol_raises_lookup(loaded_reader):
    """湖无该 symbol → 抛 LookupError（service 据此降级 Mock）。"""
    f = LakeDataFetcher()
    with pytest.raises(LookupError):
        f.fetch_ohlcv("999999.SH", datetime(2025, 1, 1), datetime(2025, 1, 3), freq="1d")


def test_fetch_ohlcv_out_of_range_raises_lookup(loaded_reader):
    """symbol 在湖但日期区间无数据 → 抛 LookupError。"""
    f = LakeDataFetcher()
    with pytest.raises(LookupError):
        f.fetch_ohlcv("600000.SH", datetime(2024, 1, 1), datetime(2024, 6, 1), freq="1d")


def test_fetch_macro_m2_maps_chinese_column(loaded_reader):
    """fetch_macro('m2') → macro 湖的 M2同比增长 列，返回 DataFrame 列名为 'm2'。"""
    f = LakeDataFetcher()
    df = f.fetch_macro("m2", datetime(2025, 1, 1), datetime(2025, 1, 3))
    assert "m2" in df.columns  # 列名归一到请求的 indicator（与 Mock 协议一致）
    assert len(df) == 3
    assert df["m2"].iloc[0] == 8.0


def test_dynamic_top50_routes_to_active_pool_first(loaded_reader):
    """dynamic_top50 → daily_active 湖活跃池首只代表。"""
    f = LakeDataFetcher()
    df = f.fetch_ohlcv("dynamic_top50", datetime(2025, 1, 1), datetime(2025, 1, 3), freq="1d")
    # daily_active 湖里 600010.SH 的 close=20.5/21.5/22.5
    assert len(df) == 3
    assert df["close"].iloc[0] == 20.5


def test_dynamic_top50_no_active_pool_raises(loaded_reader):
    """daily_active 湖缺失时 dynamic_top50 抛 LookupError（service 降级 Mock）。"""
    loaded_reader._lakes.pop("daily_active", None)
    loaded_reader._ffills.pop("daily_active", None)
    f = LakeDataFetcher()
    with pytest.raises(LookupError):
        f.fetch_ohlcv("dynamic_top50", datetime(2025, 1, 1), datetime(2025, 1, 3), freq="1d")


def test_minute_freq_routes_to_minute_lake(loaded_reader, tmp_path):
    """freq=1m → 路由到 minute 湖（非 daily）。"""
    # 临时建 minute 湖
    dates = pd.date_range("2025-01-01", periods=3, freq="min")
    minute = pd.DataFrame(
        {"open": [1, 2, 3], "high": [2, 3, 4], "low": [0.5, 1.5, 2.5],
         "close": [1.5, 2.5, 3.5], "volume": [100, 200, 300]},
        index=pd.MultiIndex.from_product([dates, ["600000.SH"]], names=["date", "symbol"]),
    )
    minute_path = tmp_path / "minute.parquet"
    minute.to_parquet(minute_path)
    loaded_reader.load(str(minute_path), key="minute")

    f = LakeDataFetcher()
    df = f.fetch_ohlcv("600000.SH", datetime(2025, 1, 1), datetime(2025, 1, 1, 0, 2), freq="1m")
    assert len(df) == 3
