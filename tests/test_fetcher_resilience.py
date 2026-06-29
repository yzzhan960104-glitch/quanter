"""验证 fetcher 接入熔断后：连续基础设施异常→熔断→快速返回空 DF（不抛）。

设计意图（与 data/resilience.py 的手动 API 契约对齐）：
- fetcher 保留既有"任何异常都返回空 DataFrame、绝不抛"的对外契约；
- 仅在内部加保护：限频/网络类基础设施异常计入熔断，连续达阈值后 OPEN，
  OPEN 期间方法首部 allow_request() 为 False → 快速返回空 DF，不再触达底层 API；
- 积分/权限类持久异常不计熔断（60s 内不可恢复，熔断无意义）。

诚实性说明：原 brief 草案用 TushareDataFetcher.__new__ 绕过 __init__，
但 fetch_ohlcv 在缓存未命中分支会访问 self.pro / self._pro 等真实客户端属性，
__new__ 后这些属性不存在会抛 AttributeError 而非"基础设施异常"，污染断言。
故这里改为在 fetch_ohlcv 抽出的 _fetch_ohlcv_from_api 上 monkeypatch，
这是熔断异常分类的真实入口，断言语义不变。
"""
import pandas as pd

from data.fetcher import TushareDataFetcher, FredDataFetcher
from data.resilience import (
    tushare_breaker,
    fred_breaker,
    tushare_rate_limiter,
    fred_rate_limiter,
    CircuitState,
)


# ============ Tushare：熔断跳闸 + 快速返回空 DF ============

def test_breaker_trips_after_repeated_infra_errors(monkeypatch):
    """连续 3 次基础设施异常 → 熔断 OPEN；第 4 次返回空 DF 且不再触达底层 API。"""
    # ── 复位熔断器与限流器状态，避免被先前用例/模块加载污染 ──
    tushare_breaker._state = CircuitState.CLOSED
    tushare_breaker._failure_count = 0

    # __new__ 绕过 __init__（无需真实 Token / 网络客户端）
    fetcher = TushareDataFetcher.__new__(TushareDataFetcher)

    # 统计底层 API 是否被触达（熔断 OPEN 后应为 0 次新增调用）
    call_counter = {"n": 0}

    def fake_api_call(self, symbol, start, end):
        # 模拟 Tushare 限频异常（命中"频繁/limit"基础设施分支）
        call_counter["n"] += 1
        raise RuntimeError("操作过于频繁，请稍后再试 limit")

    # 只 patch 抽出的"真正调 API"方法 —— 既不依赖真实 SDK，也避开缓存读写
    monkeypatch.setattr(
        TushareDataFetcher, "_fetch_ohlcv_from_api", fake_api_call, raising=False
    )

    # 连续 3 次：每次都返回空 DataFrame（不抛），并累计熔断计数
    for _ in range(3):
        df = fetcher.fetch_ohlcv(
            "000001.SZ",
            pd.Timestamp("2024-01-01"),
            pd.Timestamp("2024-01-10"),
        )
        assert isinstance(df, pd.DataFrame)

    # 熔断阈值 = 3，连续 3 次失败后应跳闸到 OPEN
    assert tushare_breaker.state == CircuitState.OPEN

    calls_before = call_counter["n"]
    # 第 4 次：熔断开启 → 快速返回空 DF，不再触达底层 API
    df = fetcher.fetch_ohlcv(
        "000001.SZ",
        pd.Timestamp("2024-01-01"),
        pd.Timestamp("2024-01-10"),
    )
    assert df.empty
    # 底层 API 调用次数无增长 —— 证明请求在 allow_request() 处被拦截
    assert call_counter["n"] == calls_before


def test_permission_error_does_not_trip_breaker(monkeypatch):
    """积分/权限类持久异常不计熔断（60s 内不可恢复，熔断无意义）。"""
    tushare_breaker._state = CircuitState.CLOSED
    tushare_breaker._failure_count = 0

    fetcher = TushareDataFetcher.__new__(TushareDataFetcher)

    def fake_api_call(self, symbol, start, end):
        # 积分不足 —— 不应计入熔断
        raise RuntimeError("抱歉，您每天最多访问该接口 2 次，权限不足，请提高积分")

    monkeypatch.setattr(
        TushareDataFetcher, "_fetch_ohlcv_from_api", fake_api_call, raising=False
    )

    # 即便连续多次权限异常，熔断器仍应 CLOSED
    for _ in range(5):
        df = fetcher.fetch_ohlcv(
            "000001.SZ",
            pd.Timestamp("2024-01-01"),
            pd.Timestamp("2024-01-10"),
        )
        # 契约保留：始终返回空 DataFrame，不抛
        assert isinstance(df, pd.DataFrame)

    assert tushare_breaker.state == CircuitState.CLOSED
    assert tushare_breaker._failure_count == 0


# ============ FRED：429/timeout/connection 计熔断 ============

def test_fred_breaker_trips_on_rate_limit(monkeypatch):
    """FRED 429/限频异常连续 3 次 → 熔断 OPEN，且始终返回空 DF。"""
    fred_breaker._state = CircuitState.CLOSED
    fred_breaker._failure_count = 0

    fetcher = FredDataFetcher.__new__(FredDataFetcher)

    def boom(self, indicator, start, end):
        # fredapi 抛出的限频异常典型文案
        raise RuntimeError("429 Too Many Requests: rate limit exceeded")

    monkeypatch.setattr(FredDataFetcher, "_fetch_series_from_api", boom, raising=False)

    for _ in range(3):
        df = fetcher.fetch_macro(
            "DGS10",
            pd.Timestamp("2024-01-01"),
            pd.Timestamp("2024-01-10"),
        )
        assert isinstance(df, pd.DataFrame)

    assert fred_breaker.state == CircuitState.OPEN


# ============ 限流器不抛：acquire 即使在并发紧张下也仅阻塞/返回 ============

def test_rate_limiter_is_attached_and_callable():
    """确认限流器单例已挂载且可正常 acquire（不抛，给足补充时间确保拿到令牌）。

    设计注意：限流器是模块级共享单例，先前用例可能已耗尽令牌。故这里给较长
    超时（令牌桶 refill_rate: tushare=1.0/s, fred=0.5/s，4s 内必然补满），
    断言其"最终能拿到令牌且不抛" —— 验证接线而非瞬时容量。
    """
    assert tushare_rate_limiter.acquire(1.0, timeout=4.0) is True
    assert fred_rate_limiter.acquire(1.0, timeout=4.0) is True
