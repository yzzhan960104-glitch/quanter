# 企业级容灾基建 + 专业交易终端重构 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 quanter 项目补齐后端高可用基建（熔断/限流/实盘执行抽象/多通道预警）并把前端重构为沉浸式暗黑交易终端（全局 Grid 布局 + 专业 K 线图 + SSE 实时日志），全部接真实回测数据。

**Architecture:** 后端沿用"异步外壳 + 同步内核"现状——新增 `data/resilience.py`（纯 Python 熔断器+令牌桶；同步 fetcher 走手动 `allow_request/record_*` API 以保留"返回空 DataFrame"既有契约，gateway/notifier 走装饰器路径）、`trading/execution_gateway.py`（异步抽象基类 + 持仓对账纯函数 + Mock 参考实现）、`core/notifier.py`（httpx 异步单例多通道）。数据契约补丁：`BacktestResponse` 加 `ohlcv`+`positions`，`_serialize_backtest_result` 改签名接收 `price_data`。新增 SSE 日志端点（`StreamingResponse` + 跨线程 `LogStreamHub`）。前端：`main.ts` 强制 dark + 注册 ECharts 暗色主题，`App.vue` 重写为 CSS Grid 终端，新增 `ProChart.vue`（K线+成交量+净值叠加+买卖点）、`TerminalLogs.vue`（SSE+分色+自动滚动）、`PositionsTable.vue`。

**Tech Stack:** Python 3.10+ / FastAPI / Pydantic v2 / pandas / pytest（后端）；Vue3 `<script setup lang="ts">` / Vite6 / Element Plus 2.9 / ECharts5 + vue-echarts / axios / vue-router4（前端）。`httpx>=0.27` 已在 requirements，无需新增依赖。

## Global Constraints

- **全中文**：对话、注释、文档、commit 关键描述用中文；注释解释 Why（交易物理意图/数学/边界）。
- **极简/反黑盒**：熔断与令牌桶用纯 Python 显式实现，**禁止**引入 tenacity/circuitbreaker/redis。
- **不伪造 API**：QMT 仅抽象占位；Telegram/企业微信凭证走 `.env`/`config.py`，**绝不硬编码 token**。
- **不改回测数学**：OHLCV/positions 为纯序列化透传；fetcher 仅做"基础设施异常分类"，不动取数逻辑。
- **同步/异步边界**：fetcher 同步→同步锁；gateway/notifier/SSE 异步。日志从线程池线程进入事件循环须经 `loop.call_soon_threadsafe`。
- **后端测试**：`pytest tests/<file>.py -v`，TDD（先红后绿）。**前端测试**：项目无 vitest，前端任务以 `cd web && npm run build`（vue-tsc 类型检查 + vite 构建）为门禁 + 手动目视。
- **分支**：`feat/resilience-terminal`（已建）。每个 Task 末尾 commit。
- **Python 版本**：3.10+（用 `X | Y` 联合类型、`list[...]` 泛型）。

---

## File Structure（职责划分）

**新建（后端）**
- `data/resilience.py` — `DataFetchError`、`CircuitBreaker`（手动 API + 装饰器）、`RateLimiter`（令牌桶）、模块级单例 breaker/limiter。
- `trading/execution_gateway.py` — `PositionDrift`/`ReconciliationResult`、`reconcile()` 纯函数、`BaseExecutionGateway`（ABC）、`MockExecutionGateway`。
- `core/__init__.py`（空包标记）、`core/notifier.py` — `NotificationChannel`/`TelegramChannel`/`WeComChannel`、`NotificationManager` 单例。
- `server/api/v1/logs.py` — `LogStreamHub`（跨线程订阅）、`RingBufferLogHandler`、`GET /stream` SSE 端点。

**新建（前端）**
- `web/src/components/ProChart.vue` — K线+成交量+净值叠加+买卖点 markPoint。
- `web/src/components/TerminalLogs.vue` — SSE 实时日志、分色、自动滚动。
- `web/src/components/PositionsTable.vue` — 持仓快照表（空态兜底）。

**改动（后端）**
- `data/fetcher.py` — 4 处 except 分支分类（基础设施异常 → `record_failure`；其余返回空 DF）+ 方法体首部加限流/熔断手动调用。
- `server/schemas/backtest.py` — 新增 `OhlcvPoint`、`PositionRow`；`BacktestResponse` 加两字段。
- `server/services/backtest_service.py` — `_serialize_backtest_result` 加 `price_data` 参数，透传 ohlcv/positions；`run_single_backtest` 调用处传入。
- `server/main.py` — include logs router；lifespan 注册/卸载 `RingBufferLogHandler`。

**改动（前端）**
- `web/src/main.ts` — 引入 dark css-vars、`html.dark`、注册 ECharts 暗色主题。
- `web/src/api/backtest.ts` — `SingleBacktestResponse` 加 `ohlcv`/`positions` + 新增 `OhlcvPoint`/`PositionRow` 接口。
- `web/src/App.vue` — 重写为 CSS Grid 终端布局。

---

## Phase 1 — 后端容灾基建

### Task 1: `data/resilience.py` 熔断器（CircuitBreaker）

**Files:**
- Create: `data/resilience.py`
- Test: `tests/test_resilience.py`

**Interfaces:**
- Produces: `DataFetchError(Exception)`、`CircuitState(Enum)`、`CircuitOpenError(Exception)`、`CircuitBreaker(name, failure_threshold=3, recovery_timeout=60.0, expected_exception=Exception, half_open_max_calls=1, on_open=None, on_close=None)`；方法 `allow_request()->bool`、`record_success()`、`record_failure()`、`state` 属性；`__call__` 装饰器（同步+异步自适应）。

- [ ] **Step 1: 写失败测试（状态机 + 手动 API）**

创建 `tests/test_resilience.py`：

```python
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


def test_breaker_decorator_async_is coroutine():
    import asyncio
    from data.resilience import CircuitBreaker
    cb = CircuitBreaker(name="t", failure_threshold=3)

    @cb
    async def ok():
        return 42

    assert asyncio.iscoroutinefunction(ok)
    assert asyncio.run(ok()) == 42
    assert cb.state == CircuitState.CLOSED
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_resilience.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'data.resilience'`）

- [ ] **Step 3: 写最小实现**

创建 `data/resilience.py`：

```python
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
                except self.expected_exception as exc:
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
            except self.expected_exception as exc:
                self.record_failure()
                raise
            self.record_success()
            return result

        return sync_wrapper
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_resilience.py -v`
Expected: 5 passed（RateLimiter 相关测试 Task 2 再加，先不跑）

> 说明：Step 1 的测试里引用了 `RateLimiter`，若想 Step 4 全绿需 Task 2 一起。可在此步先注释掉 `from ... import RateLimiter`，或直接进入 Task 2 后统一跑。推荐：本 Task 先把不含 RateLimiter 的 5 个用例跑绿（临时把 import 行改成不引 RateLimiter），Task 2 完成后再恢复 import 跑全量。

- [ ] **Step 5: 提交**

```bash
git add data/resilience.py tests/test_resilience.py
git commit -m "feat(data): 纯 Python 熔断器 CircuitBreaker（手动API+装饰器+async自适应）"
```

---

### Task 2: `data/resilience.py` 令牌桶限流器（RateLimiter）

**Files:**
- Modify: `data/resilience.py`（追加 RateLimiter 与模块级单例）
- Test: `tests/test_resilience.py`（追加用例）

**Interfaces:**
- Produces: `RateLimiter(name, capacity, refill_rate)`；方法 `try_acquire(tokens=1.0)->bool`、`acquire(tokens=1.0, timeout=None)->bool`、`__call__` 装饰器；模块级单例 `tushare_rate_limiter`、`fred_rate_limiter`、`tushare_breaker`、`fred_breaker`。

- [ ] **Step 1: 写失败测试（追加到 `tests/test_resilience.py`）**

```python
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
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_resilience.py -v`
Expected: 4 个 RateLimiter 用例 FAIL（`ImportError: cannot import name 'RateLimiter'`）

- [ ] **Step 3: 追加实现（在 `data/resilience.py` 末尾追加）**

```python
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
```

- [ ] **Step 4: 运行全量确认通过**

Run: `pytest tests/test_resilience.py -v`
Expected: 9 passed（Task 1 的 5 个 + Task 2 的 4 个）

- [ ] **Step 5: 提交**

```bash
git add data/resilience.py tests/test_resilience.py
git commit -m "feat(data): 令牌桶限流器 RateLimiter + Tushare/FRED 共享单例"
```

---

### Task 3: `data/fetcher.py` 接入容灾（限流 + 手动熔断 + 异常分类）

**Files:**
- Modify: `data/fetcher.py`（顶部 import；4 处 except 分支；3 个方法首部加限流/熔断手动调用）
- Test: `tests/test_fetcher_resilience.py`

**Interfaces:**
- Consumes: `data.resilience.{tushare_rate_limiter, fred_rate_limiter, tushare_breaker, fred_breaker, DataFetchError}`
- Produces: fetcher 在基础设施异常（429/频繁/limit/timeout/connection）时调用 `breaker.record_failure()` 后**仍返回空 DataFrame**（保留既有契约，不抛）；熔断 OPEN 时方法首部 `allow_request()` 为 False → 直接返回空 DF。

> **设计决策（保留既有契约）**：fetcher 原本"任何异常都返回空 DataFrame、绝不抛"。为不破坏回测调用链（`run_single_backtest` 不 catch fetch 异常），本任务用熔断器的**手动 API**：OPEN 时快速返回空 DF；基础设施异常 `record_failure()` 后仍返回空 DF。即"加了容灾保护，但对外行为不变"。`DataFetchError` 在 fetcher 中**不抛**（仅作为 breaker 装饰器路径的类型契约保留），故回测逻辑零影响。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_fetcher_resilience.py`：

```python
"""验证 fetcher 接入熔断后：连续基础设施异常→熔断→快速返回空 DF（不抛）。"""
import pandas as pd

from data.fetcher import TushareDataFetcher
from data.resilience import tushare_breaker, CircuitState


def _force_tushare_failure(monkeypatch):
    """让 TushareDataFetcher.fetch_ohlcv 的底层 SDK 调用抛"频繁"异常。"""
    def boom(*a, **k):
        raise RuntimeError("操作过于频繁，请稍后再试 limit")
    # Tushare 实际通过 self.pro.daily 调用；此处 monkeypatch 其 pro 对象
    monkeypatch.setattr("data.fetcher.TushareDataFetcher._ensure_pro", lambda self: None, raising=False)


def test_breaker_trips_after_repeated_infra_errors(monkeypatch):
    # 复位熔断器状态，避免被先前用例污染
    tushare_breaker._state = CircuitState.CLOSED
    tushare_breaker._failure_count = 0

    fetcher = TushareDataFetcher.__new__(TushareDataFetcher)

    def fake_call(self, *a, **k):
        # 模拟限频异常（命中"频繁/limit"基础设施分支）
        raise RuntimeError("操作过于频繁 limit")

    monkeypatch.setattr(TushareDataFetcher, "_fetch_ohlcv_from_api", fake_call, raising=False)

    # 连续 3 次：每次都返回空 DF（不抛），并累计熔断计数
    for _ in range(3):
        df = fetcher.fetch_ohlcv("000001.SZ", pd.Timestamp("2024-01-01"), pd.Timestamp("2024-01-10"))
        assert isinstance(df, pd.DataFrame)

    assert tushare_breaker.state == CircuitState.OPEN

    # 第 4 次：熔断开启 → 快速返回空 DF，不再触达底层 API
    df = fetcher.fetch_ohlcv("000001.SZ", pd.Timestamp("2024-01-01"), pd.Timestamp("2024-01-10"))
    assert df.empty
```

> 注：此测试依赖 fetcher 内部把"真正调 API"的代码抽成 `_fetch_ohlcv_from_api`（见 Step 3 重构）。若实现选择不抽方法而是就地改造，相应调整测试 monkeypatch 目标。测试的核心断言不变：**连续基础设施异常后熔断 OPEN，且后续调用返回空 DF 不抛**。

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_fetcher_resilience.py -v`
Expected: FAIL（fetcher 尚未接入熔断 / 无 `_fetch_ohlcv_from_api`）

- [ ] **Step 3: 改造 `data/fetcher.py`**

**(3a) 顶部 import（在第 28 行 `import pandas as pd` 之后加）：**

```python
from data.resilience import (
    DataFetchError,
    fred_breaker,
    fred_rate_limiter,
    tushare_breaker,
    tushare_rate_limiter,
)
```

**(3b) 抽取 Tushare 真正调 API 的逻辑为 `_fetch_ohlcv_from_api`，并在 `fetch_ohlcv` 首部加限流+熔断手动调用、改造 except 分支。**

把现有 `TushareDataFetcher.fetch_ohlcv`（第 670-769 行）中"缓存未命中后真正调 `self.pro.daily(...)` 并组装 DataFrame"的代码块抽到新方法 `_fetch_ohlcv_from_api(self, symbol, start, end) -> pd.DataFrame`；`fetch_ohlcv` 改为：

```python
    def fetch_ohlcv(
        self, symbol: str, start: datetime, end: datetime, freq: str = "1d"
    ) -> pd.DataFrame:
        # 【限流】取令牌（阻塞至有令牌或超时）—— 防 Tushare 封禁
        tushare_rate_limiter.acquire(1.0)
        # 【熔断前置】OPEN 则快速返回空 DF（保留既有"不抛"契约）
        if not tushare_breaker.allow_request():
            logger.warning(f"Tushare 熔断开启，跳过日线请求：{symbol}")
            return pd.DataFrame(
                columns=["open", "high", "low", "close", "volume", "amount"],
                index=pd.DatetimeIndex([], tz="Asia/Shanghai"),
            )
        # 缓存命中检查（保持原逻辑）...
        # ... 原缓存读取代码不变 ...
        try:
            df = self._fetch_ohlcv_from_api(symbol, start, end)
            tushare_breaker.record_success()
            # ... 原缓存写入与返回逻辑不变 ...
            return df
        except Exception as e:
            error_msg = str(e)
            # 基础设施类（限频/连接）异常 → 计入熔断；积分/权限等持久异常不计（60s 内不可恢复，熔断无意义）
            if ("频繁" in error_msg or "limit" in error_msg.lower()
                    or "timeout" in error_msg.lower() or "connection" in error_msg.lower()):
                logger.error(f"Tushare API 限频/网络异常：{symbol}")
                tushare_breaker.record_failure()
            elif "积分" in error_msg or "权限" in error_msg:
                logger.error(f"Tushare 积分不足/权限受限：{symbol} - {error_msg}")
            else:
                logger.error(f"Tushare 日线拉取失败 [{symbol}]：{error_msg}")
            # 保留既有契约：统一返回空 DataFrame，不抛
            return pd.DataFrame(
                columns=["open", "high", "low", "close", "volume", "amount"],
                index=pd.DatetimeIndex([], tz="Asia/Shanghai"),
            )
```

**(3c) 同理改造 `FredDataFetcher.fetch_macro`（第 458-569 行）**：首部 `fred_rate_limiter.acquire(1.0)` + `if not fred_breaker.allow_request(): return 空 DF`；try 内成功调 `fred_breaker.record_success()`；except 第 502-505 行（429/rate limit/timeout/connection 分支）改为 `fred_breaker.record_failure()` 后返回空 DF（第 499-512 行的整体 return 空 DF 保留）。

**(3d) 同理改造 Tushare `_fetch_daily_basic_factor`（第 882-893 行）与 `_fetch_report_factor`（第 957-968 行）** 的 except 分支：命中"频繁/limit"时 `tushare_breaker.record_failure()`。这两个内部 helper 已被 `fetch_factor_data` 调用，无需重复加限流（外层 `fetch_factor_data` 可加一次 `tushare_rate_limiter.acquire(1.0)`；若实现保持 helper 各自限流亦可，二选一，勿双重计数）。

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_fetcher_resilience.py tests/test_resilience.py -v`
Expected: PASS

- [ ] **Step 5: 回归既有 fetcher 测试（若有）**

Run: `pytest tests/ -v -k "fetch or data"`
Expected: 无回归失败（fetcher 对外仍返回 DataFrame，契约未变）

- [ ] **Step 6: 提交**

```bash
git add data/fetcher.py tests/test_fetcher_resilience.py
git commit -m "feat(data): fetcher 接入限流+熔断（手动API，保留空DF契约，不改取数逻辑）"
```

---

## Phase 2 — 实盘执行抽象层

### Task 4: `trading/execution_gateway.py` 持仓对账纯函数

**Files:**
- Create: `trading/execution_gateway.py`
- Test: `tests/test_execution_gateway.py`

**Interfaces:**
- Produces: `PositionDrift(symbol, local_qty, broker_qty, delta)`、`ReconciliationResult(matched, drifted, only_local, only_broker, max_abs_drift, is_ok)`、`reconcile(local: Mapping[str,float], broker: Mapping[str,float], tolerance: float=0.0) -> ReconciliationResult`。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_execution_gateway.py`：

```python
"""持仓对账（Reconciliation）纯函数单测。"""
from trading.execution_gateway import reconcile, ReconciliationResult


def test_reconcile_all_match():
    r = reconcile({"000001.SZ": 100, "600000.SH": 200}, {"000001.SZ": 100, "600000.SH": 200})
    assert r.is_ok is True
    assert len(r.matched) == 2
    assert r.drained == [] and r.only_local == [] and r.only_broker == []


def test_reconcile_drift_detected():
    r = reconcile({"A": 100}, {"A": 90})
    assert r.is_ok is False
    assert len(r.drained) == 1
    assert r.drained[0].delta == -10.0       # broker - local
    assert r.max_abs_drift == 10.0


def test_reconcile_only_local_and_only_broker():
    # A 仅本地有（疑似未成交/丢单）；C 仅券商有（疑似外部成交/手动单）
    r = reconcile({"A": 100, "B": 50}, {"B": 50, "C": 30})
    assert r.is_ok is False
    syms_local = {d.symbol for d in r.only_local}
    syms_broker = {d.symbol for d in r.only_broker}
    assert syms_local == {"A"}
    assert syms_broker == {"C"}
    assert len(r.matched) == 1 and r.matched[0].symbol == "B"


def test_reconcile_tolerance_boundary():
    # tolerance=5：偏差 5 视为 matched，6 视为 drifted
    r = reconcile({"A": 100}, {"A": 105}, tolerance=5.0)
    assert len(r.matched) == 1
    r2 = reconcile({"A": 100}, {"A": 106}, tolerance=5.0)
    assert len(r2.drained) == 1


def test_reconcile_max_abs_drift_is_global_max():
    r = reconcile({"A": 100, "B": 200}, {"A": 80, "B": 270})
    assert r.max_abs_drift == 70.0           # max(20, 70)
```

> 注：测试里用了 `r.drained`（拼写为 drained）。**实现必须把字段命名为 `drifted`**——此处测试为暴露"类型一致性"陷阱故意写错，实现时请把测试里的 `drained` 全部改为 `drifted` 后再跑（这是计划作者留给执行者的提醒：字段名以实现 Task 4 接口契约为准，即 `drifted`）。**正确做法：Step 1 落盘测试时直接用 `drifted`，删除本提示。**

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_execution_gateway.py -v`
Expected: FAIL（`ModuleNotFoundError`）

- [ ] **Step 3: 写实现**

创建 `trading/execution_gateway.py`（先只放对账部分，Task 5 再加 ABC）：

```python
"""
trading/execution_gateway.py
============================
实盘执行抽象层。

职责切分：
- 本 Task 实现「持仓对账」纯函数 reconcile()：无副作用、无 I/O、可独立单测。
  用于把「本地系统理论持仓」与「券商真实持仓」比对，暴露敞口偏差——
  这是实盘风控的核心：drifted（数量漂移）、only_local（疑似未成交/丢单）、
  only_broker（疑似外部成交/手动单）三类差异各自指向不同的风险场景。
- Task 5 再加异步抽象基类 BaseExecutionGateway 与 Mock 参考实现。

设计哲学：对账逻辑用纯函数 + dataclass 平铺实现，不引入事件/ORM 黑盒。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping


@dataclass(frozen=True)
class PositionDrift:
    """单个标的的持仓偏差快照。"""

    symbol: str
    local_qty: float       # 本地系统记录的理论持仓
    broker_qty: float      # 券商真实持仓
    delta: float           # broker_qty - local_qty（正=券商多，负=券商少）


@dataclass(frozen=True)
class ReconciliationResult:
    """对账结果聚合。is_ok=True 当且仅当无任何漂移与单边差异。"""

    matched: list[PositionDrift]        # |delta| <= tolerance
    drifted: list[PositionDrift]        # |delta| > tolerance（数量漂移）
    only_local: list[PositionDrift]     # 券商无、本地有（疑似未成交/丢单）
    only_broker: list[PositionDrift]    # 券商有、本地无（疑似外部成交/手动单）
    max_abs_drift: float                # 全局最大绝对偏差（敞口红线监控用）
    is_ok: bool


def reconcile(
    local: Mapping[str, float],
    broker: Mapping[str, float],
    tolerance: float = 0.0,
) -> ReconciliationResult:
    """
    比对本地与券商持仓，返回分类差异。

    边界：
    - tolerance=0 表示零容忍（实盘默认）。
    - 标的并集为 local ∪ broker；只在一侧出现的标的归入 only_*。
    - 不对 NaN/None 做特殊处理——调用方应保证 Mapping 值为有限数值。
    """
    matched: list[PositionDrift] = []
    drifted: list[PositionDrift] = []
    only_local: list[PositionDrift] = []
    only_broker: list[PositionDrift] = []
    max_abs = 0.0

    for symbol in set(local) | set(broker):
        local_qty = float(local.get(symbol, 0.0))
        broker_qty = float(broker.get(symbol, 0.0))
        delta = broker_qty - local_qty
        max_abs = max(max_abs, abs(delta))
        drift = PositionDrift(symbol, local_qty, broker_qty, delta)

        if symbol not in broker:
            only_local.append(drift)
        elif symbol not in local:
            only_broker.append(drift)
        elif abs(delta) <= tolerance:
            matched.append(drift)
        else:
            drifted.append(drift)

    is_ok = not drifted and not only_local and not only_broker
    return ReconciliationResult(matched, drifted, only_local, only_broker, max_abs, is_ok)
```

- [ ] **Step 4: 运行确认通过（先把测试里的 `drained` 改为 `drifted`）**

Run: `pytest tests/test_execution_gateway.py -v`
Expected: 5 passed

- [ ] **Step 5: 提交**

```bash
git add trading/execution_gateway.py tests/test_execution_gateway.py
git commit -m "feat(trading): 持仓对账纯函数 reconcile() + PositionDrift/ReconciliationResult"
```

---

### Task 5: `BaseExecutionGateway` 抽象基类 + `MockExecutionGateway`

**Files:**
- Modify: `trading/execution_gateway.py`（追加 ABC 与 Mock 实现）
- Test: `tests/test_execution_gateway.py`（追加用例）

**Interfaces:**
- Consumes: `trading.order_state.{OrderState, OrderStateMachine}`、Task 4 的 `reconcile/ReconciliationResult`。
- Produces: `OrderRequest`/`OrderResult` dataclass；`BaseExecutionGateway`（ABC：`connect/disconnect/submit_order/cancel_order/sync_positions/_fetch_broker_positions`）；`MockExecutionGateway`。

- [ ] **Step 1: 写失败测试（追加）**

```python
import pytest

from trading.execution_gateway import (
    BaseExecutionGateway,
    MockExecutionGateway,
    OrderRequest,
)
from trading.order_state import OrderState


@pytest.mark.asyncio
async def test_mock_gateway_submit_then_reconcile_clean():
    gw = MockExecutionGateway()
    await gw.connect()
    # 本地下一单 100 股，Mock 券商同步成交
    res = await gw.submit_order(OrderRequest(symbol="000001.SZ", qty=100, side="buy"))
    assert res.state == OrderState.FILLED
    # 对账：本地记录与券商一致 → is_ok
    result = await gw.sync_positions({"000001.SZ": 100})
    assert result.is_ok is True


@pytest.mark.asyncio
async def test_mock_gateway_reconcile_detects_drift():
    gw = MockExecutionGateway(initial_broker_positions={"000001.SZ": 100})
    await gw.connect()
    # 本地认为是 100，但券商实际 90（注入漂移）→ drifted
    result = await gw.sync_positions({"000001.SZ": 100})
    assert result.is_ok is False
    assert len(result.drifted) == 1


def test_base_gateway_is_abstract():
    with pytest.raises(TypeError):
        BaseExecutionGateway()  # type: ignore[abstract]
```

> **依赖**：`pytest-asyncio`。若 requirements 未含，本 Step 先 `pip install pytest-asyncio` 并在 `tests/` 加 `pytest.ini`/`conftest` 配置 `asyncio_mode = auto`。若不希望引入，可改用 `asyncio.run(...)` 包装断言（避免新依赖）——二选一，推荐 `asyncio.run` 以保持极简：

```python
def test_mock_gateway_submit_then_reconcile_clean():
    import asyncio
    async def run():
        gw = MockExecutionGateway()
        await gw.connect()
        res = await gw.submit_order(OrderRequest(symbol="000001.SZ", qty=100, side="buy"))
        assert res.state == OrderState.FILLED
        result = await gw.sync_positions({"000001.SZ": 100})
        assert result.is_ok is True
    asyncio.run(run())
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_execution_gateway.py -v`
Expected: 新增用例 FAIL（`BaseExecutionGateway` 等未定义）

- [ ] **Step 3: 追加实现（在 `trading/execution_gateway.py` 末尾）**

```python
from abc import ABC, abstractmethod

from trading.order_state import OrderState


@dataclass(frozen=True)
class OrderRequest:
    """下单请求（与具体券商解耦的最小契约）。"""

    symbol: str
    qty: float
    side: str            # "buy" / "sell"
    price: float | None = None   # None=市价；有值=限价
    order_id: str | None = None


@dataclass(frozen=True)
class OrderResult:
    """下单/撤单结果，复用既有 OrderState 状态机契约。"""

    order_id: str
    state: OrderState
    filled_qty: float = 0.0
    avg_price: float | None = None
    message: str = ""


class BaseExecutionGateway(ABC):
    """
    实盘执行网关抽象基类（全异步）。

    拷问边界（CLAUDE.md 接口与状态机红线）：
    - submit_order/cancel_order 必须幂等可重试，部分成交（PARTIAL_FILLED）
      须经 OrderStateMachine 合法迁移，不得越权改状态。
    - sync_positions 是风控核心：先取券商真实持仓，再与本地理论持仓对账，
      返回 ReconciliationResult 供上层决策（差异超阈值 → 触发 notifier）。
    - 真实 QMT 适配由子类实现 _fetch_broker_positions 与底层下单；
      本基类**不含**任何券商 API 调用，杜绝幻觉参数。
    """

    @abstractmethod
    async def connect(self) -> None:
        """建立并保活券商连接（含断线重连策略）。"""

    @abstractmethod
    async def disconnect(self) -> None:
        """优雅断开，释放连接资源。"""

    @abstractmethod
    async def submit_order(self, order: OrderRequest) -> OrderResult:
        """提交订单，返回含 OrderState 的结果。"""

    @abstractmethod
    async def cancel_order(self, order_id: str) -> OrderResult:
        """撤单。已成交单应返回当前状态而非报错。"""

    @abstractmethod
    async def _fetch_broker_positions(self) -> Mapping[str, float]:
        """子类实现：从券商拉取真实持仓 {symbol: qty}。"""

    async def sync_positions(
        self,
        local_positions: Mapping[str, float],
        tolerance: float = 0.0,
    ) -> ReconciliationResult:
        """对账模板方法：取券商持仓 → 与本地比对。子类无需重写。"""
        broker_positions = await self._fetch_broker_positions()
        return reconcile(local_positions, broker_positions, tolerance)


class MockExecutionGateway(BaseExecutionGateway):
    """
    Mock 参考实现：用内存 dict 模拟券商持仓，可注入漂移用于测试对账逻辑。
    生产环境用 QMTExecutionGateway(BaseExecutionGateway) 替换——其底层对接
    xtquant（同步+回调），子类内用 run_in_executor 包裹同步调用即可，
    但具体 API 参数须以 QMT 官方文档为准，本计划不臆造。
    """

    def __init__(self, initial_broker_positions: Mapping[str, float] | None = None) -> None:
        # 券商侧持仓（可被测试注入初始漂移）
        self._broker_positions: dict[str, float] = dict(initial_broker_positions or {})
        self._connected = False
        self._seq = 0

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def _fetch_broker_positions(self) -> Mapping[str, float]:
        return dict(self._broker_positions)

    async def submit_order(self, order: OrderRequest) -> OrderResult:
        if not self._connected:
            return OrderResult(order_id=order.order_id or "", state=OrderState.REJECTED, message="未连接")
        # Mock 假设全额成交（真实场景须经 OrderStateMachine 处理部分成交）
        delta = order.qty if order.side == "buy" else -order.qty
        self._broker_positions[order.symbol] = self._broker_positions.get(order.symbol, 0.0) + delta
        self._seq += 1
        return OrderResult(
            order_id=order.order_id or f"MOCK-{self._seq}",
            state=OrderState.FILLED,
            filled_qty=order.qty,
            avg_price=order.price,
        )

    async def cancel_order(self, order_id: str) -> OrderResult:
        # Mock 不支持撤已成交单
        return OrderResult(order_id=order_id, state=OrderState.CANCELLED, message="mock 撤单")
```

> **前置确认**：执行前需 Read `trading/order_state.py` 确认 `OrderState` 枚举含 `FILLED/REJECTED/CANCELLED`（探查报告已确认存在）。若枚举值大小写不同，按实际调整。

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_execution_gateway.py -v`
Expected: 8 passed（Task 4 的 5 个 + Task 5 的 3 个）

- [ ] **Step 5: 提交**

```bash
git add trading/execution_gateway.py tests/test_execution_gateway.py
git commit -m "feat(trading): BaseExecutionGateway 异步抽象 + MockExecutionGateway 参考实现"
```

---

## Phase 3 — 多通道预警通知

### Task 6: `core/notifier.py` 异步单例通知管理器

**Files:**
- Create: `core/__init__.py`、`core/notifier.py`
- Test: `tests/test_notifier.py`

**Interfaces:**
- Produces: `NotificationChannel`(ABC)、`TelegramChannel`、`WeComChannel`、`NotificationManager`（单例 `get_default()`、`add_channel()`、`async notify_risk_event(msg, level)`）、`build_default_manager()`（按 `.env` 装配，缺凭证则跳过）。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_notifier.py`：

```python
"""通知管理器：多通道并发 + 单通道软降级 + 级别前缀 + 单例。"""
import asyncio

from core.notifier import NotificationManager, NotificationChannel


class _FakeChannel(NotificationChannel):
    def __init__(self, name, fail=False):
        self.name = name
        self.fail = fail
        self.sent = []

    async def send(self, text: str) -> None:
        if self.fail:
            raise RuntimeError(f"{self.name} 发送失败")
        self.sent.append(text)


def test_notify_fans_out_to_all_channels():
    mgr = NotificationManager()
    a, b = _FakeChannel("a"), _FakeChannel("b")
    mgr.add_channel(a)
    mgr.add_channel(b)
    asyncio.run(mgr.notify_risk_event("Tushare 熔断", "ERROR"))
    assert len(a.sent) == 1 and len(b.sent) == 1
    assert "ERROR" in a.sent[0] and "Tushare 熔断" in a.sent[0]


def test_notify_soft_fails_one_channel_without_blocking_others():
    mgr = NotificationManager()
    ok, bad = _FakeChannel("ok"), _FakeChannel("bad", fail=True)
    mgr.add_channel(ok)
    mgr.add_channel(bad)
    # bad 抛异常，但 ok 仍应收到，且不向外抛
    asyncio.run(mgr.notify_risk_event("x", "WARN"))
    assert len(ok.sent) == 1


def test_level_prefix():
    mgr = NotificationManager()
    ch = _FakeChannel("c")
    mgr.add_channel(ch)
    asyncio.run(mgr.notify_risk_event("最大回撤触红线", "CRITICAL"))
    assert "CRITICAL" in ch.sent[0]


def test_singleton():
    a = NotificationManager.get_default()
    b = NotificationManager.get_default()
    assert a is b
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_notifier.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'core'`）

- [ ] **Step 3: 写实现**

创建空文件 `core/__init__.py`：

```python
"""core 包：跨领域的全局基础设施（通知、日志流等）。"""
```

创建 `core/notifier.py`：

```python
"""
core/notifier.py
================
异步单例多通道预警通知管理器。

通道解耦：NotificationChannel 抽象 → TelegramChannel / WeComChannel 具体实现。
NotificationManager.notify_risk_event(msg, level) 用 asyncio.gather 并发推送所有通道，
单通道异常软降级（记日志、不阻塞其它通道）——避免一个 IM 故障导致整条预警链失效。

凭证来源：.env / 系统环境变量，**绝不硬编码 token**。
触发场景（由调用方决定，本模块只负责可靠投递）：
  - 熔断器 on_open：API 持续不可用
  - 对账 is_ok=False：持仓敞口偏差
  - 回测/实盘最大回撤触及红线、重大滑点
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
from abc import ABC, abstractmethod
from typing import Literal

import httpx

logger = logging.getLogger(__name__)

RiskLevel = Literal["INFO", "WARN", "ERROR", "CRITICAL"]

# 级别 → 前缀（emoji + 标签），便于手机端一眼分级
_LEVEL_PREFIX: dict[RiskLevel, str] = {
    "INFO": "ℹ️ [INFO]",
    "WARN": "⚠️ [WARN]",
    "ERROR": "❌ [ERROR]",
    "CRITICAL": "🚨 [CRITICAL]",
}


class NotificationChannel(ABC):
    """通知通道抽象。子类实现 send（async）。"""

    @abstractmethod
    async def send(self, text: str) -> None:
        """发送一条文本消息。失败应抛异常，由 Manager 统一软降级。"""


class TelegramChannel(NotificationChannel):
    """Telegram Bot 推送。凭证：bot token + chat id。"""

    def __init__(self, bot_token: str, chat_id: str) -> None:
        self._token = bot_token
        self._chat_id = chat_id

    async def _http_post(self, url: str, payload: dict) -> None:
        """真实 HTTP 投递（测试可 monkeypatch 本方法以脱离网络）。"""
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()

    async def send(self, text: str) -> None:
        await self._http_post(
            f"https://api.telegram.org/bot{self._token}/sendMessage",
            {"chat_id": self._chat_id, "text": text, "parse_mode": "Markdown"},
        )


class WeComChannel(NotificationChannel):
    """企业微信群机器人 Webhook。凭证：完整 webhook url。"""

    def __init__(self, webhook_url: str) -> None:
        self._url = webhook_url

    async def _http_post(self, url: str, payload: dict) -> None:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()

    async def send(self, text: str) -> None:
        await self._http_post(
            self._url,
            {"msgtype": "text", "text": {"content": text}},
        )


class NotificationManager:
    """异步单例：并发投递所有通道，单通道失败软降级。"""

    _instance: "NotificationManager | None" = None
    _singleton_lock = threading.Lock()

    def __init__(self) -> None:
        self._channels: list[NotificationChannel] = []

    @classmethod
    def get_default(cls) -> "NotificationManager":
        """双重检查锁单例，线程安全。"""
        if cls._instance is None:
            with cls._singleton_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def add_channel(self, channel: NotificationChannel) -> None:
        self._channels.append(channel)

    def clear_channels(self) -> None:
        """测试用：清空通道，避免跨用例污染单例。"""
        self._channels.clear()

    async def notify_risk_event(self, msg: str, level: RiskLevel = "INFO") -> list:
        """并发推送所有通道；单通道异常被捕获记日志，不向外抛。"""
        prefix = _LEVEL_PREFIX.get(level, "")
        text = f"{prefix} {msg}" if prefix else msg
        if not self._channels:
            logger.debug("NotificationManager 无可用通道，跳过：%s", text)
            return []
        # return_exceptions=True → 单通道失败不中断其它
        results = await asyncio.gather(
            *(ch.send(text) for ch in self._channels), return_exceptions=True
        )
        for ch, res in zip(self._channels, results):
            if isinstance(res, Exception):
                logger.error("通知通道 %s 投递失败：%s", type(ch).__name__, res)
        return results


def build_default_manager() -> NotificationManager:
    """
    按 .env / 环境变量装配默认通道。缺凭证则跳过该通道（不报错）。
    环境变量：TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID / WECOM_WEBHOOK
    """
    mgr = NotificationManager.get_default()
    tg_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    tg_chat = os.getenv("TELEGRAM_CHAT_ID", "")
    if tg_token and tg_chat:
        mgr.add_channel(TelegramChannel(tg_token, tg_chat))
    wecom = os.getenv("WECOM_WEBHOOK", "")
    if wecom:
        mgr.add_channel(WeComChannel(wecom))
    return mgr
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_notifier.py -v`
Expected: 4 passed

- [ ] **Step 5: 提交**

```bash
git add core/__init__.py core/notifier.py tests/test_notifier.py
git commit -m "feat(core): 异步单例 NotificationManager + Telegram/企业微信通道（凭证走env）"
```

---

## Phase 4 — 数据契约补丁（OHLCV/positions 透传 + SSE 日志）

### Task 7: `server/schemas/backtest.py` 新增 OhlcvPoint / PositionRow 字段

**Files:**
- Modify: `server/schemas/backtest.py`（第 179 行 `TradeRecord` 之后、第 182 行 `BacktestResponse` 之前插入两个模型；`BacktestResponse` 加两字段）
- Test: `tests/test_backtest_schema.py`

**Interfaces:**
- Produces: `OhlcvPoint(date, open, high, low, close, volume)`、`PositionRow(symbol, qty, market_value)`；`BacktestResponse` 新增 `ohlcv: List[OhlcvPoint]`、`positions: List[PositionRow]`。

> **设计修正（相对 spec）**：`ohlcv` 仅加单资产 `BacktestResponse`（组合多资产单一 K 线序列语义不成立）；`positions` 同样先只加单资产。`PortfolioResponse` 本轮不动，组合持仓作为后续迭代。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_backtest_schema.py`：

```python
"""BacktestResponse 新增 ohlcv / positions 字段的契约测试。"""
from server.schemas.backtest import BacktestResponse, OhlcvPoint, PositionRow


def test_backtest_response_accepts_ohlcv_and_positions():
    resp = BacktestResponse(
        metrics={
            "initial_capital": 1e6, "final_nav": 1.1e6, "total_return": 0.1,
            "annual_return": 0.1, "annual_volatility": 0.15, "max_drawdown": -0.05,
            "sharpe_ratio": 1.2, "calmar_ratio": 2.0, "win_rate": 0.6,
            "profit_loss_ratio": 1.5, "n_trades": 10, "n_failed_trades": 1,
        },
        nav_series=[],
        drawdown_series=[],
        trades=[],
        ohlcv=[OhlcvPoint(date="2024-01-02", open=10.0, high=10.5, low=9.8, close=10.2, volume=100000)],
        positions=[PositionRow(symbol="000001.SZ", qty=100, market_value=1020.0)],
    )
    assert resp.ohlcv[0].close == 10.2
    assert resp.positions[0].symbol == "000001.SZ"
```

> 注：`MetricsResponse` 是否允许 dict 构造取决于其字段。若 Pydantic 严格，改用关键字参数逐字段传入。执行者按实际 MetricsResponse 字段调整构造方式，断言目标不变（ohlcv/positions 可读）。

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_backtest_schema.py -v`
Expected: FAIL（`OhlcvPoint` 未定义 / `ohlcv` 非法字段）

- [ ] **Step 3: 改 schema**

在 `server/schemas/backtest.py` 第 179 行 `TradeRecord` 类之后插入：

```python
class OhlcvPoint(BaseModel):
    """单根 K 线（开高低收量），用于前端 ProChart 绘制蜡烛图。"""

    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float


class PositionRow(BaseModel):
    """持仓快照行（取回测末态），用于前端 PositionsTable。"""

    symbol: str
    qty: float
    market_value: float
```

把 `BacktestResponse`（第 182-188 行）改为：

```python
class BacktestResponse(BaseModel):
    """单资产回测完整响应"""

    metrics: MetricsResponse
    nav_series: List[NavPoint]
    drawdown_series: List[DrawdownPoint]
    trades: List[TradeRecord]
    ohlcv: List[OhlcvPoint]            # K 线序列（ProChart 消费）
    positions: List[PositionRow]       # 末态持仓快照（PositionsTable 消费）
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_backtest_schema.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add server/schemas/backtest.py tests/test_backtest_schema.py
git commit -m "feat(server): BacktestResponse 新增 ohlcv / positions 字段（纯透传，零逻辑改动）"
```

---

### Task 8: `server/services/backtest_service.py` 透传 OHLCV / positions

**Files:**
- Modify: `server/services/backtest_service.py`（`_serialize_backtest_result` 加 `price_data` 参数 + 两个抽取 helper；`run_single_backtest` 第 117 行调用处传入 `price_data`）
- Test: `tests/test_backtest_serialize.py`

**Interfaces:**
- Produces: `_extract_ohlcv(price_data: dict[str,pd.DataFrame]) -> list[OhlcvPoint]`、`_extract_positions(daily_records: pd.DataFrame, symbol: str) -> list[PositionRow]`；`_serialize_backtest_result(result, price_data)`。

> **核心障碍**：`_serialize_backtest_result` 原签名只收 `result`，拿不到 OHLCV（引擎 daily_records 不含开高低收量）。故改签名加 `price_data`，由 `run_single_backtest`（第 84 行已有 `price_data = {req.symbol: df_clean}`）传入。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_backtest_serialize.py`：

```python
"""OHLCV / positions 透传单测（针对抽取 helper，避开重型引擎）。"""
import pandas as pd

from server.services.backtest_service import _extract_ohlcv, _extract_positions


def test_extract_ohlcv_from_price_data():
    df = pd.DataFrame(
        {
            "open": [10.0, 10.2],
            "high": [10.5, 10.6],
            "low": [9.8, 10.0],
            "close": [10.2, 10.4],
            "volume": [1000, 1500],
        },
        index=pd.DatetimeIndex(
            ["2024-01-02", "2024-01-03"], tz="Asia/Shanghai", name="date"
        ),
    )
    out = _extract_ohlcv({"000001.SZ": df})
    assert len(out) == 2
    assert out[0].open == 10.0 and out[1].volume == 1500
    assert out[0].date.startswith("2024-01-02")


def test_extract_ohlcv_empty_when_no_data():
    assert _extract_ohlcv({}) == []


def test_extract_positions_from_last_record():
    daily = pd.DataFrame(
        {
            "nav": [1.0e6, 1.01e6],
            "position": [0, 100],
            "position_value": [0.0, 1020.0],
            "price": [10.2, 10.2],
        },
        index=pd.DatetimeIndex(["2024-01-02", "2024-01-03"], tz="Asia/Shanghai"),
    )
    out = _extract_positions(daily, symbol="000001.SZ")
    assert len(out) == 1                       # 仅末态快照
    assert out[0].symbol == "000001.SZ"
    assert out[0].qty == 100
    assert out[0].market_value == 1020.0


def test_extract_positions_empty_when_no_records():
    assert _extract_positions(pd.DataFrame(), symbol="X") == []
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_backtest_serialize.py -v`
Expected: FAIL（`_extract_ohlcv` 未定义）

- [ ] **Step 3: 改 service**

在 `server/services/backtest_service.py` 顶部补 import（若缺）：`from server.schemas.backtest import OhlcvPoint, PositionRow`（在现有 `from server.schemas.backtest import ...` 行追加这两个名字）。

在 `_serialize_backtest_result` 之前新增两个 helper（紧邻 `_safe_float` 附近）：

```python
def _extract_ohlcv(price_data: dict[str, "pd.DataFrame"]) -> list[OhlcvPoint]:
    """
    从 price_data 透传 OHLCV（单资产：取唯一 symbol 的 df）。
    列名沿用 fetcher 小写英文；日期按 Asia/Shanghai 索引格式化为 ISO。
    纯序列化，不做任何数学变换。
    """
    if not price_data:
        return []
    df = next(iter(price_data.values()))
    if df is None or df.empty:
        return []
    dates = df.index.strftime("%Y-%m-%d").tolist()
    points: list[OhlcvPoint] = []
    for i, d in enumerate(dates):
        points.append(
            OhlcvPoint(
                date=d,
                open=float(df["open"].iloc[i]),
                high=float(df["high"].iloc[i]),
                low=float(df["low"].iloc[i]),
                close=float(df["close"].iloc[i]),
                volume=float(df["volume"].iloc[i]),
            )
        )
    return points


def _extract_positions(daily_records: "pd.DataFrame", symbol: str) -> list[PositionRow]:
    """取回测末态持仓快照（单资产：用末行 position / position_value）。"""
    if daily_records is None or daily_records.empty:
        return []
    last = daily_records.iloc[-1]
    qty = float(last.get("position", 0) or 0)
    # 优先用引擎已算好的 position_value；缺失则用 position*price 兜底
    if "position_value" in daily_records.columns and last.get("position_value") is not None:
        mv = float(last["position_value"])
    else:
        price = float(last.get("price", 0) or 0)
        mv = qty * price
    if qty == 0 and mv == 0:
        return []
    return [PositionRow(symbol=symbol, qty=qty, market_value=mv)]
```

修改 `_serialize_backtest_result` 签名（第 138 行）与返回（第 273-292 行）：

```python
def _serialize_backtest_result(
    result: Dict[str, Any], price_data: dict[str, "pd.DataFrame"]
) -> BacktestResponse:
```

在函数内取到 `daily_records`（第 163 行附近已有 `daily_records = result.get("daily_records", pd.DataFrame())`）后，新增：

```python
    symbol = next(iter(price_data), "")
    ohlcv = _extract_ohlcv(price_data)
    positions = _extract_positions(daily_records, symbol)
```

返回的 `BacktestResponse(...)` 增加 `ohlcv=ohlcv, positions=positions` 两参数。

修改 `run_single_backtest` 第 117 行调用：

```python
    return _serialize_backtest_result(result, price_data)
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_backtest_serialize.py tests/test_backtest_schema.py -v`
Expected: PASS

- [ ] **Step 5: 回归既有回测单测**

Run: `pytest tests/ -v -k "backtest"`
Expected: 无回归（若既有测试直接调 `_serialize_backtest_result(result)` 旧签名，需同步补 `price_data` 参数）

- [ ] **Step 6: 提交**

```bash
git add server/services/backtest_service.py tests/test_backtest_serialize.py
git commit -m "feat(server): _serialize_backtest_result 透传 ohlcv/positions（加 price_data 参数）"
```

---

### Task 9: `server/api/v1/logs.py` SSE 实时日志端点 + 跨线程 LogStreamHub

**Files:**
- Create: `server/api/v1/logs.py`
- Modify: `server/main.py`（include router + lifespan 注册/卸载 handler）
- Test: `tests/test_logs_stream.py`

**Interfaces:**
- Produces: `LogStreamHub`（`subscribe()->asyncio.Queue`、`unsubscribe(q)`、`publish(dict)`、跨线程 `call_soon_threadsafe`）、`RingBufferLogHandler(logging.Handler)`、`router`（`GET /stream`，前缀 `/logs`）、模块单例 `log_stream_hub`。最终端点 `GET /api/v1/logs/stream`。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_logs_stream.py`：

```python
"""SSE 日志管线：跨线程 hub 投递 + handler 入缓冲。"""
import asyncio
import logging

from server.api.v1.logs import LogStreamHub, RingBufferLogHandler, log_stream_hub


def test_hub_publish_reaches_subscriber():
    async def run():
        q = log_stream_hub.subscribe()
        log_stream_hub.publish({"level": "INFO", "message": "hello"})
        rec = await asyncio.wait_for(q.get(), timeout=1.0)
        assert rec["message"] == "hello"
        log_stream_hub.unsubscribe(q)

    asyncio.run(run())


def test_ring_buffer_handler_feeds_hub():
    hub = LogStreamHub()
    handler = RingBufferLogHandler(hub, level=logging.INFO)
    logger = logging.getLogger("test.ring")
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.info("回测启动")
    # handler.emit 同步把记录写入 hub 缓冲
    assert any("回测启动" in r["message"] for r in list(hub._buffer))


def test_hub_buffer_replays_to_new_subscriber():
    hub = LogStreamHub()
    hub.publish({"level": "INFO", "message": "历史"})
    async def run():
        q = hub.subscribe()
        rec = await asyncio.wait_for(q.get(), timeout=1.0)
        assert rec["message"] == "历史"
    asyncio.run(run())
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_logs_stream.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'server.api.v1.logs'`）

- [ ] **Step 3: 写实现**

创建 `server/api/v1/logs.py`：

```python
"""
server/api/v1/logs.py
=====================
实时日志 SSE 端点（Server-Sent Events，单向 server→client）。

为什么用 SSE 而非 WebSocket：日志是单向推送场景，SSE 更轻（HTTP 长连接 +
text/event-stream），契合 Karpathy 极简；WebSocket 的双向/帧协议对本需求属过度设计。

跨线程关键点：回测业务跑在线程池（run_in_threadpool）里，logging.emit 发生在
工作线程；而 SSE 消费在事件循环线程。asyncio.Queue 不是线程安全的，故 publish
必须用 loop.call_soon_threadsafe 把 put_nowait 投递到订阅者所在的事件循环，
否则会破坏事件循环的内部状态。
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections import deque

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

router = APIRouter(prefix="/logs", tags=["实时日志"])


class LogStreamHub:
    """日志扇出中枢：环形缓冲 + 多订阅者队列，跨线程安全投递。"""

    def __init__(self, maxlen: int = 1000) -> None:
        self._buffer: deque[dict] = deque(maxlen=maxlen)
        # 订阅者 = (队列, 其所在事件循环)，便于跨线程 call_soon_threadsafe
        self._subs: set[tuple[asyncio.Queue, asyncio.AbstractEventLoop]] = set()
        self._lock = threading.Lock()

    def subscribe(self) -> asyncio.Queue:
        """在事件循环线程内调用：注册一个队列，并回放历史缓冲。"""
        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        with self._lock:
            for rec in list(self._buffer):
                q.put_nowait(rec)
            self._subs.add((q, loop))
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with self._lock:
            self._subs = {(sq, l) for (sq, l) in self._subs if sq is not q}

    def publish(self, record: dict) -> None:
        """可被任意线程调用：写缓冲，并向每个订阅者的事件循环投递。"""
        with self._lock:
            self._buffer.append(record)
            subs = list(self._subs)
        for (q, loop) in subs:
            try:
                loop.call_soon_threadsafe(_safe_put, q, record)
            except RuntimeError:
                # 订阅者事件循环已关闭：忽略
                pass


def _safe_put(q: asyncio.Queue, rec: dict) -> None:
    """在订阅者事件循环内执行：满则丢新条目，绝不阻塞事件循环。"""
    try:
        q.put_nowait(rec)
    except asyncio.QueueFull:
        pass


class RingBufferLogHandler(logging.Handler):
    """把 Python logging 记录转成 dict 喂给 LogStreamHub。"""

    def __init__(self, hub: LogStreamHub, level: int = logging.INFO) -> None:
        super().__init__(level=level)
        self._hub = hub

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record) if self.formatter else record.getMessage()
            self._hub.publish(
                {
                    "ts": record.created,
                    "level": record.levelname,
                    "logger": record.name,
                    "message": message,
                }
            )
        except Exception:
            self.handleError(record)


# 模块级单例（main.py lifespan 挂到 root logger）
log_stream_hub = LogStreamHub()


@router.get("/stream", summary="实时日志 SSE 流")
async def stream_logs() -> StreamingResponse:
    """SSE：每条日志为一帧 `data: {json}\\n\\n`。客户端用 EventSource 订阅。"""

    async def event_gen():
        q = log_stream_hub.subscribe()
        try:
            while True:
                record = await q.get()
                yield f"data: {json.dumps(record, ensure_ascii=False)}\n\n"
        finally:
            log_stream_hub.unsubscribe(q)

    return StreamingResponse(event_gen(), media_type="text/event-stream")
```

> 注：源码中 `\\n\\n` 为 f-string 内的两个换行转义（SSE 帧分隔符），落盘时为 `\n\n` 字面。

- [ ] **Step 4: 改 `server/main.py`**

**(4a) import 区（第 19-28 行附近）加：**

```python
import logging

from server.api.v1.logs import (
    RingBufferLogHandler,
    log_stream_hub,
    router as logs_router,
)
```

**(4b) 路由挂载（第 70-72 行后加一行）：**

```python
app.include_router(logs_router, prefix="/api/v1")
```

**(4c) lifespan（第 31-44 行）：startup 注册 handler、shutdown 卸载，避免泄漏。** 把 `yield` 前后改为：

```python
    # 启动：扫描策略注册到 app.state
    loader = StrategyLoader()
    loader.scan()
    app.state.strategy_loader = loader

    # 启动：挂载 SSE 日志 handler 到 root logger
    log_handler = RingBufferLogHandler(log_stream_hub)
    log_handler.setFormatter(logging.Formatter("%(name)s | %(message)s"))
    app.state.log_handler = log_handler
    logging.getLogger().addHandler(log_handler)

    yield

    # 销毁：卸载日志 handler
    logging.getLogger().removeHandler(app.state.log_handler)
    # 销毁：模块④在此追加 scheduler.shutdown()
```

- [ ] **Step 5: 运行确认通过**

Run: `pytest tests/test_logs_stream.py -v`
Expected: 3 passed

- [ ] **Step 6: 端到端冒烟（可选，手动）**

启动 `uvicorn server.main:app --reload`，浏览器/`curl -N http://localhost:8000/api/v1/logs/stream`，触发一次回测，确认有 `data: {...}` 帧推送。

- [ ] **Step 7: 提交**

```bash
git add server/api/v1/logs.py server/main.py tests/test_logs_stream.py
git commit -m "feat(server): SSE 实时日志端点 + 跨线程 LogStreamHub（call_soon_threadsafe）"
```

---

## Phase 5 — 前端专业交易终端

> 前端无 vitest，本阶段每个 Task 的门禁为 `cd web && npm run build`（vue-tsc 类型检查 + vite 构建通过）+ 手动目视（启动 `npm run dev` 跑一次回测看渲染）。

### Task 10: `web/src/main.ts` 强制暗黑 + ECharts 暗色主题

**Files:**
- Create: `web/src/theme/echarts-terminal-dark.ts`
- Modify: `web/src/main.ts`

**Interfaces:**
- Produces: `initTerminalDarkTheme()`（用 echarts `registerTheme('terminal-dark', ...)`，A 股红涨绿跌配色）；`main.ts` 引入 dark css-vars + `html.dark` + 调用注册。

- [ ] **Step 1: 新建主题文件**

创建 `web/src/theme/echarts-terminal-dark.ts`：

```typescript
// ECharts 暗色终端主题：A 股惯例红涨绿跌（candlestick color=红涨 / color0=绿跌）
import { registerTheme } from 'echarts/core'

export function initTerminalDarkTheme(): void {
  registerTheme('terminal-dark', {
    backgroundColor: '#0d1117',
    textStyle: { color: '#c9d1d9' },
    title: { textStyle: { color: '#e6edf3' }, subtextStyle: { color: '#8b949e' } },
    legend: { textStyle: { color: '#c9d1d9' } },
    tooltip: {
      backgroundColor: 'rgba(22,27,34,0.95)',
      borderColor: '#30363d',
      textStyle: { color: '#c9d1d9' },
    },
    categoryAxis: {
      axisLine: { lineStyle: { color: '#30363d' } },
      axisLabel: { color: '#8b949e' },
      splitLine: { show: false },
    },
    valueAxis: {
      axisLine: { lineStyle: { color: '#30363d' } },
      axisLabel: { color: '#8b949e' },
      splitLine: { lineStyle: { color: '#21262d' } },
    },
    candlestick: {
      itemStyle: {
        color: '#ef5350',         // 阳线（涨）—— 红
        color0: '#26a69a',        // 阴线（跌）—— 绿
        borderColor: '#ef5350',
        borderColor0: '#26a69a',
      },
    },
    color: ['#58a6ff', '#f78166', '#3fb950', '#d29922', '#bc8cff'],
  })
}
```

- [ ] **Step 2: 改 `web/src/main.ts`（整体替换为）**

```typescript
import { createApp } from 'vue'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import 'element-plus/theme-chalk/dark/css-vars.css'
import App from './App.vue'
import router from './router'
import { initTerminalDarkTheme } from './theme/echarts-terminal-dark'

// 全局强制暗黑终端模式
document.documentElement.classList.add('dark')
// 注册 ECharts 暗色主题（ProChart/NavChart 用 theme="terminal-dark"）
initTerminalDarkTheme()

const app = createApp(App)
app.use(ElementPlus)
app.use(router)
app.mount('#app')
```

- [ ] **Step 3: 构建验证**

Run: `cd web && npm run build`
Expected: 构建通过，无 TS 错误

- [ ] **Step 4: 提交**

```bash
git add web/src/theme/echarts-terminal-dark.ts web/src/main.ts
git commit -m "feat(web): 全局强制 dark mode + ECharts terminal-dark 主题（A股红涨绿跌）"
```

---

### Task 11: `web/src/api/backtest.ts` 类型同步 ohlcv / positions

**Files:**
- Modify: `web/src/api/backtest.ts`（第 95-100 行 `SingleBacktestResponse` + 新增两个接口）

**Interfaces:**
- Produces: `OhlcvPoint`、`PositionRow` 接口；`SingleBacktestResponse` 加 `ohlcv`、`positions`。

- [ ] **Step 1: 改类型**

在 `web/src/api/backtest.ts` 第 92 行 `TradeRecord` 接口之后新增：

```typescript
export interface OhlcvPoint {
  date: string
  open: number
  high: number
  low: number
  close: number
  volume: number
}

export interface PositionRow {
  symbol: string
  qty: number
  market_value: number
}
```

把 `SingleBacktestResponse`（第 95-100 行）改为：

```typescript
export interface SingleBacktestResponse {
  metrics: Metrics
  nav_series: NavPoint[]
  drawdown_series: DrawdownPoint[]
  trades: TradeRecord[]
  ohlcv: OhlcvPoint[]
  positions: PositionRow[]
}
```

- [ ] **Step 2: 构建验证**

Run: `cd web && npm run build`
Expected: 构建通过

- [ ] **Step 3: 提交**

```bash
git add web/src/api/backtest.ts
git commit -m "feat(web): SingleBacktestResponse 同步 ohlcv / positions 类型"
```

---

### Task 12: `web/src/components/ProChart.vue` 专业 K 线图

**Files:**
- Create: `web/src/components/ProChart.vue`

**Interfaces:**
- Consumes: `OhlcvPoint`、`NavPoint`、`TradeRecord`（来自 `@/api/backtest`）。
- Props: `ohlcv: OhlcvPoint[]`、`navSeries: NavPoint[]`、`trades: TradeRecord[]`。

- [ ] **Step 1: 写组件**

创建 `web/src/components/ProChart.vue`：

```vue
<script setup lang="ts">
/**
 * 专业 K 线图：主图蜡烛（OHLCV）+ 净值叠加线（右轴）+ 副图成交量 +
 * 买卖点 markPoint（trades.direction）。主副图 dataZoom 联动。
 * 暗色主题由 main.ts 注册的 'terminal-dark' 提供。
 */
import { computed } from 'vue'
import VChart from 'vue-echarts'
import { use } from 'echarts/core'
import { CandlestickChart, LineChart, BarChart } from 'echarts/charts'
import {
  GridComponent,
  TooltipComponent,
  LegendComponent,
  DataZoomComponent,
  MarkPointComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import type { OhlcvPoint, NavPoint, TradeRecord } from '@/api/backtest'

use([
  CandlestickChart, LineChart, BarChart,
  GridComponent, TooltipComponent, LegendComponent,
  DataZoomComponent, MarkPointComponent, CanvasRenderer,
])

const props = defineProps<{
  ohlcv: OhlcvPoint[]
  navSeries: NavPoint[]
  trades: TradeRecord[]
}>()

// 净值按日期建索引，对齐到 K 线 x 轴（缺失日给 null，折线自然断开）
const navByDate = computed(() => {
  const m = new Map<string, number>()
  for (const p of props.navSeries) m.set(p.date, p.nav)
  return m
})

const option = computed(() => {
  const dates = props.ohlcv.map((o) => o.date)
  // ECharts 蜡烛数据顺序：[open, close, low, high]
  const candles = props.ohlcv.map((o) => [o.open, o.close, o.low, o.high])
  const volumes = props.ohlcv.map((o) => o.volume)
  const navLine = props.ohlcv.map((o) => navByDate.value.get(o.date) ?? null)

  // 买卖点：coord=[日期, 价格]，B 绿/S 红
  const markPoints = props.trades
    .filter((t) => t.direction === 'buy' || t.direction === 'sell')
    .map((t) => ({
      coord: [t.date, t.price],
      value: t.direction === 'buy' ? 'B' : 'S',
      itemStyle: { color: t.direction === 'buy' ? '#3fb950' : '#ef5350' },
      label: { color: '#fff', fontSize: 10 },
    }))

  return {
    animation: false,
    legend: { top: 0, data: ['K线', '净值', '成交量'] },
    tooltip: { trigger: 'axis', axisPointer: { type: 'cross' } },
    axisPointer: { link: [{ xAxisIndex: 'all' }] },
    grid: [
      { left: '6%', right: '6%', top: '8%', height: '58%' },   // 主图
      { left: '6%', right: '6%', top: '74%', height: '18%' },  // 成交量副图
    ],
    xAxis: [
      { type: 'category', data: dates, scale: true, boundaryGap: true, axisLine: { onZero: false }, splitLine: { show: false }, min: 'dataMin', max: 'dataMax' },
      { type: 'category', gridIndex: 1, data: dates, show: false, min: 'dataMin', max: 'dataMax' },
    ],
    yAxis: [
      { scale: true, splitArea: { show: false } },             // 价格（左）
      { scale: true, splitLine: { show: false } },             // 净值（右，同主图）
      { scale: true, gridIndex: 1, splitNumber: 2 },           // 成交量
    ],
    dataZoom: [
      { type: 'inside', xAxisIndex: [0, 1], start: 60, end: 100 },
      { type: 'slider', xAxisIndex: [0, 1], top: '94%', height: 16, start: 60, end: 100 },
    ],
    series: [
      {
        name: 'K线', type: 'candlestick', data: candles, xAxisIndex: 0, yAxisIndex: 0,
        markPoint: { data: markPoints, symbolSize: 28 },
      },
      {
        name: '净值', type: 'line', data: navLine, xAxisIndex: 0, yAxisIndex: 1,
        smooth: true, symbol: 'none', lineStyle: { width: 1.5, color: '#58a6ff' },
      },
      {
        name: '成交量', type: 'bar', data: volumes, xAxisIndex: 1, yAxisIndex: 2,
        itemStyle: { color: '#30363d' },
      },
    ],
  }
})
</script>

<template>
  <v-chart class="pro-chart" :option="option" theme="terminal-dark" autoresize />
</template>

<style scoped>
.pro-chart { width: 100%; height: 100%; }
</style>
```

- [ ] **Step 2: 构建验证**

Run: `cd web && npm run build`
Expected: 构建通过

- [ ] **Step 3: 提交**

```bash
git add web/src/components/ProChart.vue
git commit -m "feat(web): ProChart 专业K线（蜡烛+净值叠加+成交量+买卖点markPoint）"
```

---

### Task 13: `web/src/components/TerminalLogs.vue` 沉浸式日志终端

**Files:**
- Create: `web/src/components/TerminalLogs.vue`

**Interfaces:**
- 无 props；内部用 `EventSource('/api/v1/logs/stream')` 订阅 SSE，自维护 logs 数组。

- [ ] **Step 1: 写组件**

创建 `web/src/components/TerminalLogs.vue`：

```vue
<script setup lang="ts">
/**
 * 沉浸式日志终端：订阅后端 SSE /api/v1/logs/stream，按级别分色高亮，
 * 自动滚动到底（用户上翻则暂停跟随，回到底部自动恢复）。EventSource 自带断线重连。
 */
import { ref, nextTick, onMounted, onBeforeUnmount } from 'vue'

interface LogEntry { ts: number; level: string; logger: string; message: string }

const logs = ref<LogEntry[]>([])
const follow = ref(true)
const containerRef = ref<HTMLDivElement | null>(null)
let es: EventSource | null = null

// 后端 logging 级别：INFO/WARNING/ERROR/CRITICAL（DEBUG 归 info）
function levelClass(level: string): string {
  switch (level) {
    case 'ERROR':
    case 'CRITICAL': return 'lv-error'
    case 'WARNING': return 'lv-warn'
    case 'SUCCESS': return 'lv-success'
    default: return 'lv-info'
  }
}

function formatTs(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString('zh-CN', { hour12: false })
}

async function scrollToBottom() {
  await nextTick()
  if (follow.value && containerRef.value) {
    containerRef.value.scrollTop = containerRef.value.scrollHeight
  }
}

function onScroll() {
  // 离底 >40px 视为用户主动上翻 → 暂停跟随；回到底部 → 恢复
  const el = containerRef.value
  if (!el) return
  follow.value = el.scrollHeight - el.scrollTop - el.clientHeight < 40
}

onMounted(() => {
  es = new EventSource('/api/v1/logs/stream')
  es.onmessage = (ev) => {
    try {
      const rec = JSON.parse(ev.data) as LogEntry
      logs.value.push(rec)
      // 防爆内存：保留最近 2000 条
      if (logs.value.length > 2000) logs.value.splice(0, logs.value.length - 2000)
      scrollToBottom()
    } catch {
      /* 忽略坏帧 */
    }
  }
  // es.onerror 由浏览器自动重连，无需手动处理
})

onBeforeUnmount(() => {
  es?.close()
})
</script>

<template>
  <div ref="containerRef" class="term-logs" @scroll="onScroll">
    <div v-for="(l, i) in logs" :key="i" class="log-line">
      <span class="ts">{{ formatTs(l.ts) }}</span>
      <span :class="['lv', levelClass(l.level)]">[{{ l.level }}]</span>
      <span class="msg">{{ l.message }}</span>
    </div>
    <div v-if="!logs.length" class="empty">等待日志流…（提交回测后此处实时滚动）</div>
  </div>
</template>

<style scoped>
.term-logs {
  width: 100%; height: 100%;
  background: #010409;            /* 比面板更深的纯黑，强化终端感 */
  color: #c9d1d9;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px; line-height: 1.5;
  padding: 6px 8px; overflow-y: auto;
}
.log-line { white-space: pre-wrap; word-break: break-all; }
.ts { color: #6e7681; margin-right: 6px; }
.lv { font-weight: 600; margin-right: 6px; }
.lv-info { color: #8b949e; }
.lv-success { color: #3fb950; }
.lv-warn { color: #d29922; }
.lv-error { color: #f85149; }
.msg { color: #c9d1d9; }
.empty { color: #6e7681; padding: 8px; }
</style>
```

- [ ] **Step 2: 构建验证**

Run: `cd web && npm run build`
Expected: 构建通过

- [ ] **Step 3: 提交**

```bash
git add web/src/components/TerminalLogs.vue
git commit -m "feat(web): TerminalLogs SSE实时日志终端（分色高亮+自动跟随滚动）"
```

---

### Task 14: `web/src/components/PositionsTable.vue` 持仓表

**Files:**
- Create: `web/src/components/PositionsTable.vue`

**Interfaces:**
- Consumes: `PositionRow`（来自 `@/api/backtest`）。
- Props: `positions: PositionRow[]`。

- [ ] **Step 1: 写组件**

创建 `web/src/components/PositionsTable.vue`：

```vue
<script setup lang="ts">
/** 末态持仓快照表。空数组显示占位（组合回测持仓未接入前的兜底）。 */
import type { PositionRow } from '@/api/backtest'

defineProps<{ positions: PositionRow[] }>()
</script>

<template>
  <div class="pos-card">
    <div class="title">持仓快照</div>
    <el-table :data="positions" size="small" empty-text="暂无持仓" :border="false">
      <el-table-column prop="symbol" label="标的" min-width="90" />
      <el-table-column prop="qty" label="数量" width="70" align="right" />
      <el-table-column label="市值" width="90" align="right">
        <template #default="{ row }">
          {{ row.market_value.toLocaleString('zh-CN', { maximumFractionDigits: 0 }) }}
        </template>
      </el-table-column>
    </el-table>
  </div>
</template>

<style scoped>
.pos-card { background: #161b22; border: 1px solid #30363d; border-radius: 6px; padding: 6px; }
.title { color: #8b949e; font-size: 12px; margin-bottom: 4px; }
</style>
```

- [ ] **Step 2: 构建验证**

Run: `cd web && npm run build`
Expected: 构建通过

- [ ] **Step 3: 提交**

```bash
git add web/src/components/PositionsTable.vue
git commit -m "feat(web): PositionsTable 持仓快照表（空态兜底）"
```

---

### Task 15: `web/src/App.vue` 全局终端 Grid 布局 + 状态打通

**Files:**
- Create: `web/src/composables/useTerminalState.ts`
- Modify: `web/src/App.vue`（整体重写为终端 Grid 布局）

**Interfaces:**
- Consumes: `ParamForm.vue`（`props: mode/loading`，`emit: submit`）、`ProChart`、`TerminalLogs`、`MetricCards`（`props: metrics`）、`PositionsTable`；`@/api/backtest` 的 `runSingleBacktest`。
- Produces: `useTerminalState()` 组合式（模块级 reactive 单例，无 Pinia）：`{ loading, result, error, execute(req) }`。

> **无 Pinia 的状态共享**：用模块级 `reactive` 单例组合式（Vue 官方推荐的轻量模式）。App.vue 持布局并触发回测，各面板组合式读取同一份响应。
> **风险提示（执行者必读）**：ParamForm 的 `@submit` emit payload 形状需与 `runSingleBacktest(req)` 的 `BacktestRequest` 对齐——执行前先 Read 现有 `web/src/views/SingleBacktest.vue`（或同名视图），把其中"ParamForm payload → BacktestRequest → API 调用"的桥接逻辑搬到 `useTerminalState.execute`。本 Task 把 `/` 路由的回测编排上提到 App.vue；`/portfolio` 路由的适配作为后续迭代（当前右栏 PositionsTable 在组合页显示空态）。

- [ ] **Step 1: 新建组合式**

创建 `web/src/composables/useTerminalState.ts`：

```typescript
/**
 * 终端全局状态（模块级 reactive 单例，替代 Pinia）。
 * App.vue 触发 execute(req)，各面板读取 result 实时刷新。
 */
import { reactive, toRefs } from 'vue'
import {
  runSingleBacktest,
  type SingleBacktestResponse,
} from '@/api/backtest'

interface TerminalState {
  loading: boolean
  result: SingleBacktestResponse | null
  error: string
}

const state = reactive<TerminalState>({
  loading: false,
  result: null,
  error: '',
})

export function useTerminalState() {
  // req 形状与 api/backtest.ts 的 BacktestRequest 一致；执行者按实际类型补
  async function execute(req: any) {
    state.loading = true
    state.error = ''
    try {
      state.result = await runSingleBacktest(req)
    } catch (e: any) {
      state.error = e?.message || '回测执行失败'
      state.result = null
    } finally {
      state.loading = false
    }
  }

  return { ...toRefs(state), execute }
}
```

> 注：`runSingleBacktest` 的导出名与参数类型以 `web/src/api/backtest.ts` 实际为准（探查确认存在 apiClient 封装与单资产调用），执行者按实际 import 名调整。

- [ ] **Step 2: 重写 `web/src/App.vue`**

```vue
<script setup lang="ts">
/**
 * 全局终端布局（CSS Grid，100vh 无滚动）：
 *   左 300px = ParamForm；中央 = 上 ProChart(70%) + 下 TerminalLogs(30%)；右 250px = MetricCards + PositionsTable
 * 暗黑由 main.ts 强制开启。回测编排经 useTerminalState 组合式共享。
 */
import ParamForm from './components/ParamForm.vue'
import ProChart from './components/ProChart.vue'
import TerminalLogs from './components/TerminalLogs.vue'
import MetricCards from './components/MetricCards.vue'
import PositionsTable from './components/PositionsTable.vue'
import { useTerminalState } from './composables/useTerminalState'

const { loading, result, error, execute } = useTerminalState()
</script>

<template>
  <div class="terminal-shell">
    <aside class="panel panel-left">
      <ParamForm mode="single" :loading="loading" @submit="execute" />
    </aside>

    <main class="panel-center">
      <section class="center-chart">
        <ProChart
          v-if="result"
          :ohlcv="result.ohlcv"
          :nav-series="result.nav_series"
          :trades="result.trades"
        />
        <el-empty v-else description="提交左侧参数后在此显示 K 线与买卖点" :image-size="80" />
      </section>
      <section class="center-logs">
        <TerminalLogs />
      </section>
    </main>

    <aside class="panel panel-right">
      <MetricCards :metrics="result?.metrics ?? null" />
      <PositionsTable :positions="result?.positions ?? []" />
      <div v-if="error" class="err-tip">{{ error }}</div>
    </aside>
  </div>
</template>

<style scoped>
.terminal-shell {
  display: grid;
  grid-template-columns: 300px 1fr 250px;
  width: 100vw;
  height: 100vh;
  overflow: hidden;
  background: #0d1117;
}
.panel { overflow: auto; }
.panel-left { border-right: 1px solid #30363d; }
.panel-right {
  border-left: 1px solid #30363d;
  display: flex; flex-direction: column; gap: 8px; padding: 8px;
}
.panel-center {
  display: grid;
  grid-template-rows: 70% 30%;
  overflow: hidden;
}
.center-chart { border-bottom: 1px solid #30363d; padding: 4px; overflow: hidden; }
.center-logs { overflow: hidden; }
.err-tip { color: #f85149; font-size: 12px; padding: 4px; }
</style>
```

> **集成注意**：App.vue 不再含 `<router-view/>`，`/` 路由的回测编排被吸收到 App.vue。若需保留多路由，可在 `.panel-center` 内按需放回 `<router-view/>` 并把 ProChart/TerminalLogs 移入对应视图组件——属等价改法，执行者二选一，保证 `npm run build` 通过即可。

- [ ] **Step 3: 构建验证**

Run: `cd web && npm run build`
Expected: 构建通过（TS 类型无误）

- [ ] **Step 4: 端到端目视**

Run: `cd web && npm run dev`（后端 `uvicorn server.main:app --reload`）
操作：左侧提交一次回测 → 中央 K 线 + 买卖点渲染、下方日志流滚动、右侧指标/持仓更新、全局暗黑。
Expected: 视觉符合终端布局，无控制台报错。

- [ ] **Step 5: 提交**

```bash
git add web/src/composables/useTerminalState.ts web/src/App.vue
git commit -m "feat(web): App.vue 全局暗黑终端 Grid 布局 + useTerminalState 状态打通"
```

---

## Self-Review（计划作者自检）

**1. Spec 覆盖**：逐条对照 spec——
- ① resilience 熔断+限流 → Task 1/2/3 ✓
- ② execution_gateway 抽象+对账 → Task 4/5 ✓
- ③ notifier 多通道异步单例 → Task 6 ✓
- ④ 全局暗黑 Grid 布局 → Task 10/15 ✓
- ⑤ ProChart K线 → Task 12 ✓
- ⑥ TerminalLogs SSE → Task 9（后端）+13（前端）✓
- 地基补丁 OHLCV/positions → Task 7/8 ✓
- PositionsTable（布局引用，spec 触及文件未列）→ Task 14 ✓（补齐）

**2. 类型一致性**：
- `drifted` 字段：Task 4 实现 + Task 5 测试均用 `drifted`（Task 4 Step 1 测试模板里的 `drained` 已显式标注为陷阱并要求改为 `drifted`）✓
- `BacktestResponse.ohlcv/positions`：Task 7（schema）↔ Task 8（service 填充）↔ Task 11（前端类型）↔ Task 12/14/15（消费）一致 ✓
- `ReconciliationResult.sync_positions` 复用 `reconcile()`：Task 4 定义、Task 5 调用 ✓
- `OrderState` 复用既有 `trading.order_state`：Task 5 已注明执行前确认枚举值 ✓

**3. 占位符扫描**：无 TBD/TODO；所有代码块完整；唯一"按实际调整"处（Task 15 ParamForm payload 桥接、Task 6 Metrics 构造、Task 8 既有测试旧签名）已显式标注原因与处理方式，非占位。

**4. 风险与执行者前置确认项（已在对应 Task 标注）**：
- Task 3：fetcher 抽取 `_fetch_ohlcv_from_api` 时须保留原缓存读写逻辑。
- Task 5：`OrderState` 枚举值大小写确认。
- Task 8：既有直接调旧签名 `_serialize_backtest_result(result)` 的测试需补 `price_data`。
- Task 15：ParamForm emit payload → BacktestRequest 桥接须 Read SingleBacktest.vue 移植。

**5. 范围**：15 个 Task，分 5 Phase；每个 Task 自带测试/构建门禁与 commit，可独立审阅与回滚。后端 Phase 1-4 可先独立交付（纯后端可测），前端 Phase 5 依赖 Task 7/8 的契约字段。

---

## Execution Handoff

计划已完成并保存至 `docs/superpowers/plans/2026-06-30-resilience-terminal.md`。

**两种执行方式：**

**1. Subagent-Driven（推荐）** —— 我每个 Task 派一个全新 subagent 实现，两阶段 review（实现 → 审查），任务间快速迭代，上下文隔离干净。

**2. Inline Execution** —— 在当前会话用 executing-plans 逐 Task 批量执行，带 checkpoint 供你审查。

**请选择执行方式。** 选定后我会按 Phase 顺序推进（建议先后端 Phase 1-4 跑通全绿，再前端 Phase 5）。
