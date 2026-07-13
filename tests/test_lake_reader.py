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


def test_get_lake_returns_df_and_none(tmp_path):
    """#4：get_lake 公开读返指定湖原始 df（替代穿透 _lakes）；missing key → None。"""
    path = tmp_path / "lake.parquet"
    _make_lake_df().to_parquet(path)
    r = DataLakeReader()
    r.load(str(path), key="daily")
    df = r.get_lake("daily")
    assert df is not None and len(df) == 4
    assert r.get_lake("nonexistent") is None   # 无此 key → None（调用方自行降级）


def test_get_timeseries_on_unsorted_parquet(tmp_path):
    # Important 1：date 层级乱序的 parquet —— 验证 load() 已对索引排序，
    # get_timeseries 在 start 早于数据最早日期、end 晚于最晚日期的范围内切片不抛 KeyError，
    # 且返回该标的全部行（slice bound 可正确解析）。
    # 故意打乱行顺序：日期倒序写入 parquet，模拟上游同步脚本未排序产物。
    idx = pd.MultiIndex.from_tuples([
        ("2024-01-05", "000001.SZ"), ("2024-01-02", "000001.SZ"),
        ("2024-01-04", "000001.SZ"), ("2024-01-03", "000001.SZ"),
        ("2024-01-05", "600000.SH"), ("2024-01-02", "600000.SH"),
    ], names=["date", "symbol"])
    df = pd.DataFrame({
        "open":   [13.0, 10.0, 12.0, 11.0, 6.0, 5.0],
        "high":   [13.5, 10.5, 12.5, 11.5, 6.5, 5.5],
        "low":    [12.8, 9.8, 11.8, 10.8, 5.8, 4.8],
        "close":  [13.2, 10.2, 12.1, 11.1, 6.1, 5.1],
        "volume": [1300, 1000, 1200, 1100, 600, 500],
    }, index=idx)
    path = tmp_path / "lake_unsorted.parquet"
    df.to_parquet(path)

    r = DataLakeReader()
    r.load(str(path))
    # start 早于数据最早日期、end 晚于最晚日期，区间包含全部该标的行
    ts = r.get_timeseries("000001.SZ", "2024-01-01", "2024-01-31")
    assert len(ts) == 4
    # 切片结果应按日期升序（排序后）
    assert list(ts.index) == ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"]


def test_cross_section_with_tz_aware_datetime_index(tmp_path):
    # Important 2：date 层级为 tz-aware datetime（含时区）的 parquet —— 验证 load()
    # 已把 date 层级去时区并 normalize 到午夜，_norm_date 查询键同步 normalize，
    # get_cross_section(date_str) 不因 tz/时间 mismatch 而返回空。
    dates = pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-02", "2024-01-03"])
    # 故意带 UTC 时区 + 非零时间（13:00），模拟上游带 tz 的脏数据
    dates_tz = dates.tz_localize("UTC") + pd.Timedelta("13h")
    idx = pd.MultiIndex.from_arrays(
        [dates_tz, ["000001.SZ", "000001.SZ", "600000.SH", "600000.SH"]],
        names=["date", "symbol"],
    )
    df = pd.DataFrame({
        "open":   [10.0, 11.0, 5.0, float("nan")],
        "high":   [10.5, 11.5, 5.5, float("nan")],
        "low":    [9.8, 10.8, 4.8, float("nan")],
        "close":  [10.2, 11.1, 5.1, float("nan")],
        "volume": [1000, 1100, 500, 0],
    }, index=idx)
    path = tmp_path / "lake_tz.parquet"
    df.to_parquet(path)

    r = DataLakeReader()
    r.load(str(path))
    # 用纯日期字符串查询；底层已 normalize 到午夜、去时区，应正确命中 2024-01-02 截面
    sec = r.get_cross_section("2024-01-02")
    assert len(sec) == 2
    assert "000001.SZ" in sec.index
    assert "600000.SH" in sec.index
