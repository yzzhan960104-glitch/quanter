"""多湖缓存：lake= 参数 + 向后兼容 + 价格ffill/不ffill量。"""
import pandas as pd
from data.lake_reader import DataLakeReader


def _df(start="2024-01-02", close=10.0, sym="000001.SZ"):
    idx = pd.MultiIndex.from_tuples([("2024-01-02", sym), ("2024-01-03", sym)], names=["date", "symbol"])
    return pd.DataFrame({"open": [close, close], "high": [close, close], "low": [close, close],
                         "close": [close, float("nan")], "volume": [100, 0], "amount": [1e6, 0]}, index=idx)


def test_multilake_load_and_query_by_key(tmp_path):
    daily = tmp_path / "daily.parquet"
    minute = tmp_path / "minute.parquet"
    _df(close=10.0).to_parquet(daily)
    _df(close=20.0).to_parquet(minute)
    r = DataLakeReader()
    r.load(str(daily), key="daily")
    r.load(str(minute), key="minute")
    assert set(r.lakes()) == {"daily", "minute"}
    assert r.loaded is True
    # 按 lake 查询，互不串味
    assert r.get_cross_section("2024-01-02", lake="daily").loc["000001.SZ", "close"] == 10.0
    assert r.get_cross_section("2024-01-02", lake="minute").loc["000001.SZ", "close"] == 20.0


def test_default_lake_backward_compat(tmp_path):
    """不传 lake → 用默认湖（首次 load 的 key）。"""
    daily = tmp_path / "daily.parquet"
    _df(close=10.0).to_parquet(daily)
    r = DataLakeReader()
    r.load(str(daily), key="daily")
    # 不传 lake，走默认湖
    assert r.get_cross_section("2024-01-02").loc["000001.SZ", "close"] == 10.0
    assert r.get_timeseries("000001.SZ", "2024-01-01", "2024-01-31").iloc[0]["close"] == 10.0


def test_multilake_ffill_only_prices(tmp_path):
    """多湖各自仅价格 ffill、volume 不 ffill。"""
    p = tmp_path / "d.parquet"
    _df(close=5.0).to_parquet(p)
    r = DataLakeReader()
    r.load(str(p), key="daily")
    sec = r.get_cross_section("2024-01-03", lake="daily")
    assert sec.loc["000001.SZ", "close"] == 5.0   # 停牌日价格 ffill
    assert sec.loc["000001.SZ", "volume"] == 0    # volume 不 ffill
