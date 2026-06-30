"""YFinanceClient：熔断守卫 + 数据洗净 + 空降级。"""
import pandas as pd
from data.clients.yfinance_client import YFinanceClient, yfinance_breaker, _EMPTY


def _make_raw():
    idx = pd.DatetimeIndex(["2024-01-02", "2024-01-03"])
    return pd.DataFrame({"Open": [1.0, 2.0], "High": [1.5, 2.5], "Low": [0.9, 1.9],
                         "Close": [1.2, 2.2], "Volume": [100, 200]}, index=idx)


def test_cleanse_returns_standard_columns(monkeypatch):
    client = YFinanceClient()
    monkeypatch.setattr("yfinance.download", lambda *a, **k: _make_raw())
    df = client.get_history("^GSPC", "2024-01-02", "2024-01-03")
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert len(df) == 2


def test_failure_returns_empty_df_not_raise(monkeypatch):
    """yfinance 抛错时必须返回空 DF，绝不向外抛。"""
    client = YFinanceClient()
    def boom(*a, **k):
        raise RuntimeError("network down")
    monkeypatch.setattr("yfinance.download", boom)
    # 复位熔断器内部状态，确保不被其它用例污染（与 tests/test_fetcher_resilience.py 一致范式）
    # 说明：原 brief 草案的 `while yfinance_breaker.state.value != "closed": pass` 是死代码——
    # breaker 若 OPEN 会无限自旋，CLOSED 时又是 no-op，毫无作用。直接手动复位最稳妥。
    from data.resilience import CircuitState
    yfinance_breaker._state = CircuitState.CLOSED
    yfinance_breaker._failure_count = 0
    df = client.get_history("^GSPC", "2024-01-02", "2024-01-03")
    assert df.empty


def test_cleanse_flattens_multiindex_columns(monkeypatch):
    """yfinance 多 symbol 下载会返回 MultiIndex 列（如 (Close, ^GSPC)），须扁平化为标准列名。"""
    # 构造 MultiIndex 列的原始 DataFrame，模拟 yf.download(["^GSPC", ...]) 的真实返回结构
    idx = pd.DatetimeIndex(["2024-01-02", "2024-01-03"])
    cols = pd.MultiIndex.from_tuples(
        [("Open", "^GSPC"), ("High", "^GSPC"), ("Low", "^GSPC"),
         ("Close", "^GSPC"), ("Volume", "^GSPC")]
    )
    raw = pd.DataFrame(
        [[1.0, 1.5, 0.9, 1.2, 100], [2.0, 2.5, 1.9, 2.2, 200]],
        index=idx, columns=cols,
    )
    client = YFinanceClient()
    monkeypatch.setattr("yfinance.download", lambda *a, **k: raw)
    df = client.get_history("^GSPC", "2024-01-02", "2024-01-03")
    # MultiIndex 被扁平化为一维标准列名
    assert not isinstance(df.columns, pd.MultiIndex)
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert len(df) == 2
