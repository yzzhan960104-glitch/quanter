# -*- coding: utf-8 -*-
"""stock_basic.parquet schema 兼容测试（2026-08-05：ts_code 是索引层不是列）。"""
import pandas as pd

from trading import trading_plan as tp


def _reset_cache(monkeypatch):
    monkeypatch.setattr(tp, "_NAME_MAP_CACHE", None)


def test_load_name_map_ts_code_index_level(monkeypatch):
    """parquet 以 MultiIndex(index, ts_code) 存储时按索引层取值。"""
    df = pd.DataFrame({"symbol": ["000001.SZ"], "name": ["平安银行"]})
    df.index = pd.MultiIndex.from_arrays([[0], ["000001.SZ"]], names=["index", "ts_code"])
    monkeypatch.setattr("pandas.read_parquet", lambda p: df)
    _reset_cache(monkeypatch)
    assert tp._load_name_map() == {"000001.SZ": "平安银行"}


def test_load_name_map_ts_code_column(monkeypatch):
    """parquet 以 ts_code 列存储时按列取值（向后兼容）。"""
    df = pd.DataFrame({"ts_code": ["000001.SZ"], "name": ["平安银行"]})
    monkeypatch.setattr("pandas.read_parquet", lambda p: df)
    _reset_cache(monkeypatch)
    assert tp._load_name_map() == {"000001.SZ": "平安银行"}


def test_load_name_map_symbol_column(monkeypatch):
    """parquet 只有 symbol 列时回退 symbol（无 ts_code 时仍可显示名称）。"""
    df = pd.DataFrame({"symbol": ["000001.SZ"], "name": ["平安银行"]})
    monkeypatch.setattr("pandas.read_parquet", lambda p: df)
    _reset_cache(monkeypatch)
    assert tp._load_name_map() == {"000001.SZ": "平安银行"}
