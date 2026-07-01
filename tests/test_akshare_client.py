"""AKShareClient：手动熔断+限流，失败返空 DF 不抛；wrapper 洗净列。

拷问边界（对齐 yfinance_client 范式）：
- 熔断 OPEN 期间绝不触达底层 ak.* —— 防止限频连环超时被封禁。
- 任何异常（网络/限频/解析/空返回）一律 catch → 返回空 DF，绝不外抛。
- 日线返回值须为标准 schema（open/high/low/close/volume/amount + DatetimeIndex）。
"""
import pandas as pd
from data.clients.akshare_client import AKShareClient, akshare_breaker
from data.resilience import CircuitState


def _reset():
    """复位熔断器内部状态，确保不被其它用例污染（与 test_fetcher_resilience 同范式）。"""
    akshare_breaker._state = CircuitState.CLOSED
    akshare_breaker._failure_count = 0


def test_fetch_daily_hist_cleanses(monkeypatch):
    """日线返回须洗净中文列名为标准 schema（open/high/low/close/volume/amount）。"""
    _reset()
    fake = pd.DataFrame({"日期": ["2024-01-02"], "开盘": [10], "最高": [11], "最低": [9],
                         "收盘": [10.5], "成交量": [1000], "成交额": [1e7]})
    monkeypatch.setattr("akshare.stock_zh_a_hist", lambda *a, **k: fake)
    df = AKShareClient().fetch_daily_hist("000001.SZ", "2024-01-02", "2024-01-03")
    # 标准列名前 6 列固定顺序（amount 在 turnover 之前，turnover 可选）
    assert list(df.columns)[:6] == ["open", "high", "low", "close", "volume", "amount"]
    assert len(df) == 1


def test_failure_returns_empty_df(monkeypatch):
    """底层 ak.* 抛错时必须返回空 DF，绝不向外抛（红线契约）。"""
    _reset()

    def boom(*a, **k):
        raise RuntimeError("network down")
    monkeypatch.setattr("akshare.stock_zh_a_hist", boom)
    df = AKShareClient().fetch_daily_hist("000001.SZ", "2024-01-02", "2024-01-03")
    assert df.empty   # 绝不抛


def test_circuit_open_returns_empty_without_calling_ak(monkeypatch):
    """熔断 OPEN 期间须快速返回空 DF，且绝不触达底层 ak.*（防连环超时）。"""
    import time as _time
    _reset()
    # 强制将熔断器置为 OPEN，且 _opened_at 设为【当前 monotonic】（刚跳闸），
    # 这样 _now - opened_at ≈ 0 < recovery_timeout(60s)，冷却必然未到期，
    # _maybe_half_open_locked 不会转 HALF_OPEN，allow_request 直接返回 False。
    # （注意：若设为 float('-inf')，_now - opened_at 为巨大正数会触发半开放行，反例。）
    akshare_breaker._state = CircuitState.OPEN
    akshare_breaker._opened_at = _time.monotonic()

    called = {"n": 0}

    def should_not_be_called(*a, **k):
        called["n"] += 1
        raise AssertionError("熔断 OPEN 期间不应触达底层 ak.*")

    monkeypatch.setattr("akshare.stock_zh_a_hist", should_not_be_called)
    df = AKShareClient().fetch_daily_hist("000001.SZ", "2024-01-02", "2024-01-03")
    assert df.empty
    assert called["n"] == 0   # 熔断守卫生效，底层未被触达
