# -*- coding: utf-8 -*-
"""data_lake 接入测试：DataLakeReader.symbols() 全市场枚举。"""
import pandas as pd
import pytest

from data.lake_reader import DataLakeReader


def _make_reader_with_daily(tmp_path, monkeypatch) -> DataLakeReader:
    """构造一个已 load 小样本 daily 湖的 reader（不污染全局单例）。

    小样本 MultiIndex(date,symbol)，3 个 symbol × 2 日，amount 故意用小值（千元口径）
    便于后续 _load_price_data 测试验证 ×1000 转元。
    """
    df = pd.DataFrame(
        {"open": [10, 11, 20, 21, 30, 31],
         "high": [11, 12, 22, 23, 33, 34],
         "low": [9, 10, 18, 19, 27, 28],
         "close": [10.5, 11.5, 21, 22, 31, 32],
         "volume": [1000, 1100, 2000, 2100, 3000, 3100],
         "amount": [100.0, 110.0, 200.0, 210.0, 300.0, 310.0]},   # 千元口径
        index=pd.MultiIndex.from_tuples(
            [(pd.Timestamp("2024-01-02"), "000001.SZ"),
             (pd.Timestamp("2024-01-03"), "000001.SZ"),
             (pd.Timestamp("2024-01-02"), "600000.SH"),
             (pd.Timestamp("2024-01-03"), "600000.SH"),
             (pd.Timestamp("2024-01-02"), "920982.BJ"),
             (pd.Timestamp("2024-01-03"), "920982.BJ")],
            names=["date", "symbol"],
        ),
    )
    path = tmp_path / "daily_sample.parquet"
    df.to_parquet(path)
    reader = DataLakeReader()
    reader.load(str(path), key="daily")
    return reader


def test_symbols_returns_all_unique_symbols(tmp_path, monkeypatch):
    """symbols() 返回 daily 湖全部唯一 symbol（封装 _lakes 私有，全市场枚举入口）。"""
    reader = _make_reader_with_daily(tmp_path, monkeypatch)
    syms = reader.symbols()
    assert set(syms) == {"000001.SZ", "600000.SH", "920982.BJ"}
    assert len(syms) == 3


def test_symbols_empty_when_no_lake_loaded():
    """无任何湖 load 时 symbols() 返空列表（离线降级，不抛）。"""
    reader = DataLakeReader()   # 全新实例，未 load
    assert reader.symbols() == []


def test_symbols_respects_lake_arg(tmp_path, monkeypatch):
    """symbols(lake=X) 仅返回指定湖的 symbol。"""
    reader = _make_reader_with_daily(tmp_path, monkeypatch)
    # daily 湖有 3 个 symbol
    assert len(reader.symbols("daily")) == 3
    # 不存在的湖返空
    assert reader.symbols("nonexistent") == []
