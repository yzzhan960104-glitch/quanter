"""熔断器/限流器单测。"""
import time

import pytest

from data.resilience import (
    CircuitBreaker,
    CircuitOpenError,
    CircuitState,
    DataFetchError,
    RateLimiter,
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


def test_rate_limiter_burst_then_refill():
    # 桶容量 3、每秒补 1 个令牌
    rl = RateLimiter(name="t", capacity=3, refill_rate=1.0)
    assert rl.try_acquire(1.0) is True
    assert rl.try_acquire(1.0) is True
    assert rl.try_acquire(1.0) is True   # 突发放完 3 个
    assert rl.try_acquire(1.0) is False  # 桶空


def test_rate_limiter_acquire_blocks_until_token(monkeypatch):
    rl = RateLimiter(name="t", capacity=1, refill_rate=100.0)  # 100/s 很快补
    assert rl.acquire(1.0, timeout=1.0) is True
    # 令牌刚耗尽，但 refill 极快，acquire 应在很短时间内拿到
    assert rl.acquire(1.0, timeout=2.0) is True


def test_rate_limiter_acquire_timeout_returns_false():
    rl = RateLimiter(name="t", capacity=1, refill_rate=0.0 + 1e-6)  # 几乎不补
    rl.try_acquire(1.0)
    # 桶空且几乎不补充 → 超时返回 False（而非永久阻塞）
    assert rl.acquire(1.0, timeout=0.2) is False


def test_rate_limiter_decorator_throttles():
    rl = RateLimiter(name="t", capacity=2, refill_rate=1000.0)
    called = []

    @rl
    def hit():
        called.append(1)

    hit()
    hit()
    # 容量 2 已用完，第 3 次会阻塞至补充（refill 极快，应很快返回）
    hit()
    assert len(called) == 3


def test_breaker_half_open_failure_reopens():
    # 半开试探失败 → 重回 OPEN（熔断器最关键的"自愈回退"路径）
    cb = CircuitBreaker(name="t", failure_threshold=1, recovery_timeout=5.0)
    cb.record_failure()                      # CLOSED→OPEN
    assert cb.state == CircuitState.OPEN
    cb._opened_at = time.monotonic() - 6.0   # 冷却到期→HALF_OPEN
    assert cb.allow_request() is True        # 占半开名额
    cb.record_failure()                      # 半开试探失败→重回 OPEN
    assert cb.state == CircuitState.OPEN
    assert cb.allow_request() is False       # OPEN 拒绝


# ============ M4：reset() 测试隔离 affordance ============

def test_circuit_breaker_reset_restores_closed_after_trip():
    """reset() 把已跳闸的熔断器恢复到 CLOSED 初始态（计数/时点清零，不动配置）。"""
    cb = CircuitBreaker(name="t", failure_threshold=2, recovery_timeout=60.0)
    cb.record_failure()
    cb.record_failure()  # 达阈值 2 → 跳闸 OPEN
    assert cb.state == CircuitState.OPEN
    cb.reset()
    assert cb.state == CircuitState.CLOSED
    assert cb._failure_count == 0
    assert cb._opened_at == 0.0
    assert cb._half_open_calls == 0


def test_rate_limiter_reset_refills_tokens():
    """reset() 把耗尽的令牌桶恢复到满（capacity），不动配置。"""
    # refill_rate 极慢，防 _refill_locked 自愈干扰断言
    rl = RateLimiter(name="t", capacity=2, refill_rate=0.01)
    assert rl.try_acquire(2.0) is True    # 耗尽全部令牌
    assert rl.try_acquire(1.0) is False   # 空桶
    rl.reset()
    assert rl._tokens == 2
    assert rl.try_acquire(2.0) is True    # reset 后又满


def test_resilience_singletons_start_clean():
    """canary：每个用例入口处，全部 resilience 模块级单例须处于初始态。

    Why：验证 conftest 的 _reset_resilience_singletons autouse fixture 生效——
    若先前用例污染了单例且 fixture 未复位，本 canary 在全量跑里会捕获（任意顺序）。
    单文件单跑 trivially 绿；价值在全量跑（Task5）里体现。
    """
    from data.resilience import (
        CircuitState, tushare_breaker, fred_breaker, akshare_breaker,
        tushare_rate_limiter_basic, tushare_rate_limiter_special,
        fred_rate_limiter, akshare_limiter,
    )
    for cb in (tushare_breaker, fred_breaker, akshare_breaker):
        assert cb.state == CircuitState.CLOSED, f"{cb.name} 未复位为 CLOSED"
        assert cb._failure_count == 0, f"{cb.name} _failure_count 残留"
    for rl in (tushare_rate_limiter_basic, tushare_rate_limiter_special,
               fred_rate_limiter, akshare_limiter):
        assert rl._tokens == rl.capacity, f"{rl.name} 令牌未复位为 capacity"
