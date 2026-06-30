"""DataLakeReader：ffill 仅价格、截面/时序查询、离线降级。"""
import pandas as pd
from data.lake_reader import DataLakeReader


def _make_lake_df():
    # MultiIndex(date, symbol)；构造一只含停牌（NaN）的标的
    idx = pd.MultiIndex.from_tuples([
        ("2024-01-02", "000001.SZ"), ("2024-01-03", "000001.SZ"),
        ("2024-01-02", "600000.SH"), ("2024-01-03", "600000.SH"),
    ], names=["date", "symbol"])
    df = pd.DataFrame({
        "open":   [10.0, 11.0, 5.0, float("nan")],
        "high":   [10.5, 11.5, 5.5, float("nan")],
        "low":    [9.8, 10.8, 4.8, float("nan")],
        "close":  [10.2, 11.1, 5.1, float("nan")],
        "volume": [1000, 1100, 500, 0],
    }, index=idx)
    return df


def test_ffill_only_prices_not_volume(tmp_path):
    path = tmp_path / "lake.parquet"
    _make_lake_df().to_parquet(path)
    r = DataLakeReader()
    r.load(str(path))
    # 600000.SH 在 2024-01-03 停牌：价格应 ffill 为 01-02 的值，volume 必须保持 0
    sec = r.get_cross_section("2024-01-03")
    assert sec.loc["600000.SH", "close"] == 5.1  # ffill 价格
    assert sec.loc["600000.SH", "volume"] == 0   # volume 不 ffill


def test_timeseries_returns_raw(tmp_path):
    path = tmp_path / "lake.parquet"
    _make_lake_df().to_parquet(path)
    r = DataLakeReader()
    r.load(str(path))
    ts = r.get_timeseries("000001.SZ", "2024-01-01", "2024-01-31")
    assert len(ts) == 2
    assert list(ts.columns)[:1] == ["open"]


def test_offline_mode_when_parquet_missing(tmp_path):
    r = DataLakeReader()
    r.load(str(tmp_path / "nope.parquet"))
    assert r.loaded is False
    assert r.get_cross_section("2024-01-02").empty
