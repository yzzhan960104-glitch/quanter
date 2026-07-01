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


# --------------------------------------------------------------
# 单索引 DatetimeIndex 湖支持（修复跨任务硬阻塞 T11 审查发现）
# --------------------------------------------------------------

def test_load_single_index_datetime_lake(tmp_path):
    """单索引 DatetimeIndex 湖（如宏观指标）须能载入 _lakes（不被 MultiIndex 校验拒绝）。

    红线：sync_macro_credit(T5) 落盘的 macro 湖是纯 DatetimeIndex（宏观指标是全市场
    级别单序列，无 symbol 层）。若 load() 因"非 MultiIndex"拒绝 → _lakes["macro"]
    永远空 → CreditRegime._load_from_lake 拿空 DF → compute() 永远返 0 → 宏观否决
    失效。本测试锁死"单索引湖能载入"这一硬性契约。
    """
    idx = pd.date_range("2024-01-02", periods=5, freq="B")
    macro = pd.DataFrame(
        {"shrzgm": [1, 2, 3, 4, 5], "M1M2_gap": [0.1] * 5, "dr007": [2.0] * 5},
        index=idx,
    )
    macro.index.name = "date"
    p = tmp_path / "macro.parquet"
    macro.to_parquet(p)
    r = DataLakeReader()
    r.load(str(p), key="macro")
    assert "macro" in r.lakes()              # 载入成功（不被拒绝）
    assert r.loaded is True
    # CreditRegime 式直读：reader._lakes["macro"].loc[:date]
    df = r._lakes["macro"]
    assert len(df.loc[:pd.Timestamp("2024-01-04")]) == 3   # 含 01-02/03/04


def test_multiindex_lake_still_works_after_single_index_support(tmp_path):
    """向后兼容：MultiIndex 湖载入 + 价格 ffill 不被单索引支持破坏。

    锁死零回归红线——新增单索引分支不得影响既有 MultiIndex 价格湖的载入与
    get_cross_section 价格 ffill / volume 不 ffill 契约。
    """
    idx = pd.MultiIndex.from_tuples(
        [("2024-01-02", "000001.SZ"), ("2024-01-03", "000001.SZ")],
        names=["date", "symbol"],
    )
    daily = pd.DataFrame(
        {
            "open": [10, 11],
            "high": [11, 12],
            "low": [9, 10],
            "close": [10.5, float("nan")],
            "volume": [100, 0],
        },
        index=idx,
    )
    p = tmp_path / "daily.parquet"
    daily.to_parquet(p)
    r = DataLakeReader()
    r.load(str(p), key="daily")
    sec = r.get_cross_section("2024-01-03", lake="daily")
    assert sec.loc["000001.SZ", "close"] == 10.5   # 价格 ffill
    assert sec.loc["000001.SZ", "volume"] == 0     # volume 不 ffill
