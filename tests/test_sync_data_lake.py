"""sync_data_lake 单元测试（代理 daily+adj_factor 重建前复权，纯逻辑不依赖网络）。

覆盖：
- build_multiindex：合并 shard → MultiIndex(date, symbol) 纯函数
- load_universe：mock pro.stock_basic，验证 ST/退过滤
- fetch_qfq：mock pro.daily + pro.adj_factor，验证前复权重建公式
    price_qfq = price_raw × adj_factor / adj_factor_latest
"""
import pandas as pd
import pytest

from scripts.sync_data_lake import build_multiindex, fetch_qfq, load_universe


def test_build_multiindex_merges_shards_to_multiindex(tmp_path):
    """合并 shard → MultiIndex(date, symbol)，date 为 datetime 且排序。"""
    dates = pd.date_range("2025-01-01", periods=3, freq="D")
    s1 = pd.DataFrame(
        {"open": [1, 2, 3], "high": [2, 3, 4], "low": [0.5, 1.5, 2.5],
         "close": [1.5, 2.5, 3.5], "volume": [100, 200, 300]},
        index=dates,
    )
    s1.index.name = "date"
    s1.to_parquet(tmp_path / "600000.SH.parquet")
    s2 = pd.DataFrame(
        {"open": [10, 20, 30], "close": [10.5, 20.5, 30.5], "volume": [1000, 2000, 3000]},
        index=dates,
    )
    s2.index.name = "date"
    s2.to_parquet(tmp_path / "600010.SH.parquet")

    out = str(tmp_path / "daily.parquet")
    build_multiindex(str(tmp_path), out)

    df = pd.read_parquet(out)
    assert isinstance(df.index, pd.MultiIndex)
    assert df.index.names == ["date", "symbol"]
    assert df.index.is_monotonic_increasing
    assert df.xs("600000.SH", level="symbol")["close"].tolist() == [1.5, 2.5, 3.5]


def test_build_multiindex_raises_on_empty_shard_dir(tmp_path):
    with pytest.raises(RuntimeError, match="shard 目录无数据"):
        build_multiindex(str(tmp_path), str(tmp_path / "out.parquet"))


def test_load_universe_filters_st():
    """load_universe：pro.stock_basic 返回，剔 ST/退。"""
    class _FakePro:
        def stock_basic(self, **kwargs):
            return pd.DataFrame({
                "ts_code": ["600000.SH", "000001.SZ", "600001.SH", "000002.SZ"],
                "symbol": ["600000", "000001", "600001", "000002"],
                "name": ["浦发银行", "平安银行", "*ST金田", "退市美都"],
                "list_date": ["19991110", "19910403", "20000101", "19910129"],
            })

    codes = load_universe(_FakePro())
    assert "600000.SH" in codes and "000001.SZ" in codes
    assert "600001.SH" not in codes  # *ST 剔除
    assert "000002.SZ" not in codes  # 退 剔除
    assert len(codes) == 2


def test_fetch_qfq_reconstructs_forward_adjusted_prices():
    """fetch_qfq: daily × adj_factor/adj_latest 重建前复权（基准日价不变，历史价下调）。"""
    from data.resilience import CircuitState, tushare_breaker
    tushare_breaker._state = CircuitState.CLOSED
    tushare_breaker._failure_count = 0

    # 模拟除权：最后一日 adj_factor 翻倍（20 vs 历史 10）
    daily = pd.DataFrame({
        "trade_date": ["20240102", "20240103", "20240104"],
        "open": [10, 11, 12], "high": [11, 12, 13], "low": [9, 10, 11],
        "close": [10.0, 11.0, 12.0], "vol": [1000, 1100, 1200], "amount": [1e7, 1.1e7, 1.2e7],
    })
    adj = pd.DataFrame({
        "trade_date": ["20240102", "20240103", "20240104"],
        "adj_factor": [10.0, 10.0, 20.0],
        "ts_code": ["600000.SH"] * 3,
    })

    class _FakePro:
        def daily(self, **kw):
            return daily

        def adj_factor(self, **kw):
            return adj

    try:
        df = fetch_qfq(_FakePro(), "600000.SH", "2024-01-01", "2024-01-31")
        # 前复权：基准 = 最新 adj = 20。
        # 历史日 close=10.0 × 10/20 = 5.0；基准日 close=12.0 × 20/20 = 12.0（不变）
        assert df["close"].iloc[0] == pytest.approx(5.0)
        assert df["close"].iloc[1] == pytest.approx(5.5)   # 11 × 10/20
        assert df["close"].iloc[-1] == pytest.approx(12.0)  # 基准日不变
        # volume 不复权（保持原值）
        assert df["volume"].iloc[0] == 1000
    finally:
        tushare_breaker._state = CircuitState.CLOSED
        tushare_breaker._failure_count = 0


def test_fetch_qfq_empty_daily_returns_empty():
    """daily 返空 → 空 DF（不抛，停牌/无行情正常态）。"""
    from data.resilience import CircuitState, tushare_breaker
    tushare_breaker._state = CircuitState.CLOSED
    tushare_breaker._failure_count = 0

    class _FakePro:
        def daily(self, **kw):
            return pd.DataFrame()

        def adj_factor(self, **kw):
            return pd.DataFrame()

    df = fetch_qfq(_FakePro(), "600000.SH", "2024-01-01", "2024-01-31")
    assert df.empty
