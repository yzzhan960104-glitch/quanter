"""熔断器单测（Task 1 中间态：仅 CircuitBreaker）。"""
import time

import pytest

from data.resilience import (
    CircuitBreaker,
    CircuitOpenError,
    CircuitState,
    DataFetchError,
)


def test_breaker_starts_closed_and_counts_failures():
    cb = CircuitBreaker(name="t", failure_threshold=3, recovery_timeout=60.0)
    assert cb.state == CircuitState.CLOSED
    cb.record_failure()
    cb.record_failure()
    assert cb.state == CircuitState.CLOSED  # 未达阈值仍 CLOSED
    cb.record_failure()
    assert cb.state == CircuitState.OPEN     # 第 3 次 → 跳闸


def test_breaker_open_rejects_then_half_open_after_cooldown(monkeypatch):
    cb = CircuitBreaker(name="t", failure_threshold=2, recovery_timeout=10.0)
    cb.record_failure()
    cb.record_failure()
    assert cb.state == CircuitState.OPEN
    assert cb.allow_request() is False       # OPEN 直接拒绝

    # 模拟冷却到期：把 _opened_at 往前拨
    cb._opened_at = time.monotonic() - 11.0
    assert cb.state == CircuitState.HALF_OPEN
    assert cb.allow_request() is True        # 半开放行 1 次试探


def test_breaker_half_open_success_closes():
    cb = CircuitBreaker(name="t", failure_threshold=1, recovery_timeout=5.0)
    cb.record_failure()
    assert cb.state == CircuitState.OPEN
    cb._opened_at = time.monotonic() - 6.0
    assert cb.allow_request() is True        # 占用半开名额
    cb.record_success()
    assert cb.state == CircuitState.CLOSED


def test_breaker_decorator_raises_when_open():
    cb = CircuitBreaker(name="t", failure_threshold=1, recovery_timeout=60.0)
    calls = {"n": 0}

    @cb
    def flaky():
        calls["n"] += 1
        raise DataFetchError("timeout")

    with pytest.raises(DataFetchError):
        flaky()           # 第 1 次：失败计数达阈值 → OPEN
    assert cb.state == CircuitState.OPEN
    with pytest.raises(CircuitOpenError):
        flaky()           # 第 2 次：OPEN 直接拒，被保护函数不再被调用
    assert calls["n"] == 1


def test_breaker_decorator_async_is_coroutine():
    import asyncio
    from data.resilience import CircuitBreaker
    cb = CircuitBreaker(name="t", failure_threshold=3)

    @cb
    async def ok():
        return 42

    assert asyncio.iscoroutinefunction(ok)
    assert asyncio.run(ok()) == 42
    assert cb.state == CircuitState.CLOSED
