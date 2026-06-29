"""
data/resilience.py
==================
高可用容灾基建：熔断器（CircuitBreaker）+ 令牌桶限流器（RateLimiter）。

设计哲学（Karpathy 极简 / 显式至上）：
- 纯 Python 实现，零第三方"黑盒"依赖（不引 tenacity / circuitbreaker / redis）。
- 状态机与令牌桶逻辑全部平铺直叙，可被单测逐行验证。
- 线程安全（threading.RLock）+ async 自适应：装饰器检测被包函数是否为
  coroutine function，分别走同步或异步路径，使其既能保护「同步 fetcher」
  （走手动 allow_request/record_* API，保留"返回空 DataFrame"契约），
  也能保护「异步 gateway/notifier」（走装饰器路径，失败抛 CircuitOpenError）。

拷问边界：
- 熔断 OPEN 期间绝不触达被保护函数 —— 防止外部接口（如 Tushare 限频）
  持续打满导致连环超时与被封禁。
- 计时一律用 time.monotonic()，规避系统时间回拨造成的突发放行/误判。
"""
from __future__ import annotations

import asyncio
import functools
import threading
import time
from enum import Enum
from typing import Awaitable, Callable, TypeVar

T = TypeVar("T")


class DataFetchError(Exception):
    """数据获取基础设施异常（超时 / 429 限频 / 连接断开）。

    与"无数据"语义区分：本异常代表外部接口不可用，应被熔断器统计；
    "无数据"则正常返回空 DataFrame，不计入熔断。
    """


class CircuitState(str, Enum):
    """熔断三态：CLOSED（正常放行）/ OPEN（熔断拒绝）/ HALF_OPEN（半开试探）。"""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(Exception):
    """熔断器装饰器路径下，OPEN/半开名额满时抛出，调用方可降级处理。"""


class CircuitBreaker:
    """
    熔断器：CLOSED 下连续 failure_threshold 次 record_failure → OPEN；
    OPEN 持续 recovery_timeout 秒 → HALF_OPEN（放 half_open_max_calls 次试探）；
    半开成功 → CLOSED；半开失败 → 重回 OPEN。

    提供两种用法：
      1. 手动 API（同步 fetcher 用，保留返回空结果契约）：
         if not cb.allow_request(): return 空结果
         try: ...; cb.record_success()
         except 基础设施异常: cb.record_failure(); return 空结果
      2. 装饰器（gateway/notifier 用）：@cb  —— 失败抛 CircuitOpenError。
    """

    def __init__(
        self,
        name: str = "default",
        failure_threshold: int = 3,
        recovery_timeout: float = 60.0,
        expected_exception: type[BaseException] | tuple[type[BaseException], ...] = Exception,
        half_open_max_calls: int = 1,
        on_open: Callable[[], None] | Callable[[], Awaitable[None]] | None = None,
        on_close: Callable[[], None] | Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.expected_exception = expected_exception
        self.half_open_max_calls = half_open_max_calls
        self.on_open = on_open
        self.on_close = on_close

        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._half_open_calls = 0
        self._opened_at: float = 0.0
        self._lock = threading.RLock()

    def _now(self) -> float:
        # monotonic 不受系统时间回拨影响 —— 容灾计时必须用它
        return time.monotonic()

    def _maybe_half_open_locked(self) -> None:
        """OPEN 冷却到期 → 自动转 HALF_OPEN。须持锁。"""
        if (
            self._state == CircuitState.OPEN
            and self._now() - self._opened_at >= self.recovery_timeout
        ):
            self._state = CircuitState.HALF_OPEN
            self._half_open_calls = 0
            self._failure_count = 0

    @property
    def state(self) -> CircuitState:
        with self._lock:
            self._maybe_half_open_locked()
            return self._state

    # ---- 手动 API（fetcher 用）----
    def allow_request(self) -> bool:
        """请求前置检查。OPEN 或半开名额满 → False（不抛）；HALF_OPEN 占一个名额。"""
        with self._lock:
            self._maybe_half_open_locked()
            if self._state == CircuitState.OPEN:
                return False
            if self._state == CircuitState.HALF_OPEN:
                if self._half_open_calls >= self.half_open_max_calls:
                    return False
                self._half_open_calls += 1
            return True

    def record_success(self) -> None:
        with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                self._state = CircuitState.CLOSED
                self._failure_count = 0
                self._half_open_calls = 0
                self._fire(self.on_close)
            elif self._state == CircuitState.CLOSED:
                self._failure_count = 0

    def record_failure(self) -> None:
        with self._lock:
            self._failure_count += 1
            if self._state == CircuitState.HALF_OPEN:
                self._trip_locked()
            elif self._state == CircuitState.CLOSED:
                if self._failure_count >= self.failure_threshold:
                    self._trip_locked()

    def _trip_locked(self) -> None:
        """跳闸到 OPEN。须持锁。"""
        was_open = self._state == CircuitState.OPEN
        self._state = CircuitState.OPEN
        self._opened_at = self._now()
        self._half_open_calls = 0
        if not was_open:
            self._fire(self.on_open)

    def _fire(self, cb) -> None:
        """触发开/闭回调。同步回调直接执行；协程回调丢入事件循环不阻塞调用方。"""
        if cb is None:
            return
        if asyncio.iscoroutinefunction(cb):
            try:
                asyncio.get_running_loop().create_task(cb())
            except RuntimeError:
                # 纯同步上下文无事件循环：静默忽略，避免阻塞 fetcher 线程
                pass
        else:
            cb()

    # ---- 装饰器（gateway/notifier 用）----
    def __call__(self, func: Callable[..., T]) -> Callable[..., T]:
        if asyncio.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args, **kwargs):
                if not self.allow_request():
                    raise CircuitOpenError(f"熔断器 [{self.name}] 开启，拒绝请求")
                try:
                    result = await func(*args, **kwargs)
                except self.expected_exception:
                    self.record_failure()
                    raise
                self.record_success()
                return result

            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs):
            if not self.allow_request():
                raise CircuitOpenError(f"熔断器 [{self.name}] 开启，拒绝请求")
            try:
                result = func(*args, **kwargs)
            except self.expected_exception:
                self.record_failure()
                raise
            self.record_success()
            return result

        return sync_wrapper


class RateLimiter:
    """
    令牌桶限流器：capacity 为桶容量上限，refill_rate 为 token/秒匀速补充。
    acquire(tokens) 在令牌不足时阻塞等待（至多 timeout 秒），超时返回 False。
    线程安全。适用于限频 API（防 429 / 封禁）。

    算法（第一性原理）：
      每次取令牌前，按"距上次补充的墙钟秒数 × refill_rate"线性补令牌，
      封顶 capacity；再判断是否够扣除。无锁队列、无黑盒。
    """

    def __init__(self, name: str, capacity: float, refill_rate: float) -> None:
        if capacity <= 0 or refill_rate <= 0:
            raise ValueError("capacity 与 refill_rate 必须为正数")
        self.name = name
        self.capacity = capacity
        self.refill_rate = refill_rate
        self._tokens = capacity
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()

    def _refill_locked(self) -> None:
        """按墙钟时间线性补令牌，封顶 capacity。须持锁。"""
        now = time.monotonic()
        elapsed = now - self._last_refill
        if elapsed > 0:
            self._tokens = min(self.capacity, self._tokens + elapsed * self.refill_rate)
            self._last_refill = now

    def try_acquire(self, tokens: float = 1.0) -> bool:
        """非阻塞尝试：有令牌则扣减返回 True，否则返回 False。"""
        with self._lock:
            self._refill_locked()
            if self._tokens >= tokens:
                self._tokens -= tokens
                return True
            return False

    def acquire(self, tokens: float = 1.0, timeout: float | None = None) -> bool:
        """阻塞至令牌可用或超时。超时返回 False（调用方可降级处理）。"""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._lock:
                self._refill_locked()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return True
                deficit = tokens - self._tokens
                wait = deficit / self.refill_rate
            # 自旋退避：以 10ms 粒度兼顾响应性与 CPU 占用
            if deadline is not None and time.monotonic() + min(wait, 0.01) > deadline:
                return False
            time.sleep(min(wait, 0.01))

    def __call__(self, func: Callable[..., T]) -> Callable[..., T]:
        """装饰器：每次调用前 acquire(1)。同步阻塞 / 异步让出事件循环。"""
        if asyncio.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args, **kwargs):
                # 异步路径：用 asyncio.sleep 让出事件循环，不阻塞线程
                while not self.try_acquire(1.0):
                    await asyncio.sleep(0.01)
                return await func(*args, **kwargs)

            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs):
            self.acquire(1.0)
            return func(*args, **kwargs)

        return sync_wrapper


# ============ 模块级单例（fetcher 共享，避免每次新建桶/熔断器）============
# 限频策略：突发容量 + 持续 QPS，依据各数据源官方限频量级保守取值。
tushare_rate_limiter = RateLimiter(name="tushare", capacity=5, refill_rate=1.0)
fred_rate_limiter = RateLimiter(name="fred", capacity=2, refill_rate=0.5)

# 熔断器：连续 3 次基础设施异常 → 熔断 60s（expected_exception 仅装饰器路径生效）
tushare_breaker = CircuitBreaker(
    name="tushare", failure_threshold=3, recovery_timeout=60.0, expected_exception=DataFetchError
)
fred_breaker = CircuitBreaker(
    name="fred", failure_threshold=3, recovery_timeout=60.0, expected_exception=DataFetchError
)
