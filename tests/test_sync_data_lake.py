"""数据湖同步：universe 过滤 ST、空数据跳过、断点续传。"""
import pandas as pd
from scripts.sync_data_lake import load_universe, fetch_qfq, build_multiindex


class _FakePro:
    def stock_basic(self, **kwargs):
        return pd.DataFrame({
            "ts_code": ["000001.SZ", "000002.SZ", "600000.SH"],
            "name": ["平安银行", "万科A", "ST 浦发"],
            "list_date": ["19910403", "19910129", "19991110"],
        })

    def pro_bar(self, **kwargs):
        # 仅 000001.SZ 返回数据，其它返回空，覆盖"空数据跳过"
        if kwargs.get("ts_code") == "000001.SZ":
            return pd.DataFrame({
                "trade_date": ["20240102", "20240103"],
                "open": [10.0, 11.0], "high": [10.5, 11.5], "low": [9.8, 10.8],
                "close": [10.2, 11.1], "vol": [1000, 1100], "amount": [1e7, 1.1e7],
            })
        return pd.DataFrame()


def test_load_universe_excludes_st():
    codes = load_universe(_FakePro())
    assert "000001.SZ" in codes
    assert "000002.SZ" in codes
    assert "600000.SH" not in codes  # 名称含 ST 被剔除


def test_fetch_qfq_cleanses_columns():
    df = fetch_qfq(_FakePro(), "000001.SZ", "2024-01-01", "2024-01-31")
    assert list(df.columns) == ["open", "high", "low", "close", "volume", "amount"]
    assert len(df) == 2


def test_build_multiindex(tmp_path):
    shard = tmp_path / "000001.SZ.parquet"
    fetch_qfq(_FakePro(), "000001.SZ", "2024-01-01", "2024-01-31").to_parquet(shard)
    out = tmp_path / "lake.parquet"
    build_multiindex(str(tmp_path), str(out))
    lake = pd.read_parquet(out)
    assert isinstance(lake.index, pd.MultiIndex)
    assert "000001.SZ" in lake.index.get_level_values("symbol").unique()


def test_fetch_qfq_persistent_error_not_counted_into_breaker(monkeypatch):
    """积分/权限持久态异常不计入熔断（与限频瞬时态区分）。

    Why 持久态不熔断：积分不足是账户配置问题，60s 冷却内不可自愈；若计入熔断，
    全市场 5000+ 标的逐只拉取时连续 3 只积分不足即 OPEN，后续 60s 全返空、
    shard 大面积缺失——熔断不仅无益反而放大故障半径。故持久态仅记日志、不触达熔断器。
    """
    from data.resilience import CircuitState, tushare_breaker
    # 复位熔断器（模块级单例，防其它用例污染）
    tushare_breaker._state = CircuitState.CLOSED
    tushare_breaker._failure_count = 0

    class _QuotaPro:
        def pro_bar(self, **kwargs):
            raise Exception("对不起, 您的积分不足, 请充值!")
    try:
        df = fetch_qfq(_QuotaPro(), "000001.SZ", "2024-01-01", "2024-01-31")
        assert df.empty                                   # 仍返回空不抛
        assert tushare_breaker._failure_count == 0        # ★ 持久态不计熔断
    finally:
        # 末尾复位，避免污染后续用例
        tushare_breaker._state = CircuitState.CLOSED
        tushare_breaker._failure_count = 0


def test_fetch_qfq_transient_error_counted_into_breaker(monkeypatch):
    """限频瞬时态异常计入熔断（冷却可自愈，熔断能止损）。"""
    from data.resilience import CircuitState, tushare_breaker
    tushare_breaker._state = CircuitState.CLOSED
    tushare_breaker._failure_count = 0

    class _LimitPro:
        def pro_bar(self, **kwargs):
            raise Exception("frequency limit exceeded, too many requests")
    try:
        df = fetch_qfq(_LimitPro(), "000001.SZ", "2024-01-01", "2024-01-31")
        assert df.empty
        assert tushare_breaker._failure_count == 1        # ★ 瞬时态计入
    finally:
        tushare_breaker._state = CircuitState.CLOSED
        tushare_breaker._failure_count = 0
