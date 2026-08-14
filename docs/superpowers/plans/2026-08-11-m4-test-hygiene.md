# M4 测试卫生实施计划（resilience 单例跨用例污染根治）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 根治 `data.resilience` 模块级 breaker/limiter 单例被测试裸写内部状态、跨用例污染的工程债，使全量跑在任意顺序下都不受单例残留态影响。

**Architecture:** 三层治疗——① 给 `CircuitBreaker`/`RateLimiter` 加公开 `reset()` 方法（恢复运行态到初始，不动配置）；② 根 `tests/conftest.py` 加 autouse fixture，每用例前 `reset()` 全部单例（治本，任何测试忘记还原也不污染）；③ 清掉测试里 4 处裸写 `_state`/`_failure_count` 的入口 reset（已被 fixture 覆盖，变冗余）+ 把 1 处刻意置 OPEN 的裸写改 `monkeypatch.setattr`（自动还原）。最后删一处死代码。

**Tech Stack:** Python 3.10（`.venv310`）、pytest、stdlib（threading/time）。无新依赖。

## Global Constraints

- 全中文注释，像素级说明 Why（CLAUDE.md）。
- Karpathy 极简：纯标准库，**不引入任何新依赖**（CLAUDE.md）。
- 行为等价红线：本计划只动测试基建 + 加两个 `reset()` 公开方法，**不改单例的运行态语义/业务行为**（`reset()` 只在测试 fixture 调用，生产路径不调）。
- TDD：每个新方法先写失败测试，再实现。
- 基准：file:line 为 2026-08-11（commit `2c7822c2` 之后）；实施前若行号漂移，按符号名定位。

## File Structure

| 文件 | 职责 | 本计划改动 |
|---|---|---|
| `data/resilience.py` | CircuitBreaker + RateLimiter + 模块级单例 | **新增** `CircuitBreaker.reset()` / `RateLimiter.reset()` |
| `tests/conftest.py` | pytest 共享 fixture | **新增** autouse `_reset_resilience_singletons` |
| `tests/test_resilience.py` | resilience 单测 | **新增** reset() 单测 + 污染 canary |
| `tests/test_fetcher_resilience.py` | fetcher 熔断测试 | **删** 3 处冗余入口 reset（裸写 `_state`） |
| `tests/test_akshare_client.py` | AKShare 熔断测试 | **删** `_reset()` helper + 调用；**改** 1 处刻意 OPEN 为 monkeypatch |
| `tests/test_sync_data_lake.py` | sync_data_lake 测试 | **删** 1 处冗余入口 reset + 失效 import |
| `tests/trading/test_engine.py` | engine 测试 | **删** 1 行死代码（`_DEFAULT_DB_OVERRIDE` 幽灵属性） |

---

### Task 1: 给 CircuitBreaker / RateLimiter 加 `reset()` 方法

**Files:**
- Modify: `data/resilience.py`（`CircuitBreaker` 类，`record_failure` 之后约 L140；`RateLimiter` 类，`acquire` 之后约 L248）
- Test: `tests/test_resilience.py`

**Interfaces:**
- Produces: `CircuitBreaker.reset(self) -> None`（恢复 `_state=CLOSED`/`_failure_count=0`/`_half_open_calls=0`/`_opened_at=0.0`，持锁，不动配置）；`RateLimiter.reset(self) -> None`（恢复 `_tokens=capacity`/`_last_refill=now`，持锁，不动配置）。Task 2 的 fixture 依赖这两个方法名。

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_resilience.py` 末尾）

```python
def test_circuit_breaker_reset_restores_closed_after_trip():
    """reset() 把已跳闸的熔断器恢复到 CLOSED 初始态（计数/时点清零，不动配置）。"""
    from data.resilience import CircuitBreaker, CircuitState
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
    from data.resilience import RateLimiter
    # refill_rate 极慢，防 _refill_locked 自愈干扰断言
    rl = RateLimiter(name="t", capacity=2, refill_rate=0.01)
    assert rl.try_acquire(2.0) is True    # 耗尽全部令牌
    assert rl.try_acquire(1.0) is False   # 空桶
    rl.reset()
    assert rl._tokens == 2
    assert rl.try_acquire(2.0) is True    # reset 后又满
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv310/Scripts/python.exe -m pytest tests/test_resilience.py::test_circuit_breaker_reset_restores_closed_after_trip tests/test_resilience.py::test_rate_limiter_reset_refills_tokens -v`
Expected: FAIL with `AttributeError: 'CircuitBreaker' object has no attribute 'reset'`（同理 RateLimiter）。

- [ ] **Step 3: 实现 `CircuitBreaker.reset()`**（`data/resilience.py`，加在 `record_failure` 方法之后、`_trip_locked` 之前）

```python
    def reset(self) -> None:
        """重置运行态到初始（CLOSED + 计数清零），不动配置（threshold/timeout 等）。

        Why：模块级单例跨用例共享运行态，测试 autouse fixture 每用例前调它防污染
        （test_fetcher_resilience / test_akshare_client 裸写 _state 无 finally 的治本层）。
        生产路径不调——仅作测试隔离 affordance。
        """
        with self._lock:
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            self._half_open_calls = 0
            self._opened_at = 0.0
```

- [ ] **Step 4: 实现 `RateLimiter.reset()`**（`data/resilience.py`，加在 `acquire` 方法之后、`__call__` 装饰器之前）

```python
    def reset(self) -> None:
        """重置运行态到初始（满令牌 + 重置补水时点），不动配置（capacity/refill_rate）。

        Why：模块级单例跨用例共享，测试 autouse fixture 每用例前调它防令牌耗尽污染。
        生产路径不调——仅作测试隔离 affordance。
        """
        with self._lock:
            self._tokens = self.capacity
            self._last_refill = time.monotonic()
```

- [ ] **Step 5: 跑测试确认通过**

Run: `.venv310/Scripts/python.exe -m pytest tests/test_resilience.py::test_circuit_breaker_reset_restores_closed_after_trip tests/test_resilience.py::test_rate_limiter_reset_refills_tokens -v`
Expected: 2 PASS。

- [ ] **Step 6: 跑 resilience 全文件确认无回归**

Run: `.venv310/Scripts/python.exe -m pytest tests/test_resilience.py -v`
Expected: 全 PASS（既有用例 + 2 新增）。

- [ ] **Step 7: Commit**

```bash
git add data/resilience.py tests/test_resilience.py
git commit -m "feat(resilience): CircuitBreaker/RateLimiter 加 reset()——测试隔离 affordance（M4 Task1）"
```

---

### Task 2: 根 conftest 加 autouse `_reset_resilience_singletons` fixture

**Files:**
- Modify: `tests/conftest.py`（加在 `_isolate_job_ledger` fixture 之后、`tmp_db` 之前，约 L165）
- Test: `tests/test_resilience.py`（加 canary）

**Interfaces:**
- Consumes: Task 1 的 `CircuitBreaker.reset()` / `RateLimiter.reset()`。
- Produces: autouse fixture `_reset_resilience_singletons`——每用例 setup 阶段 reset 全部 7 个模块级单例。Task 3 依赖它（删冗余入口 reset 后由它兜底）。

- [ ] **Step 1: 写 canary 测试**（追加到 `tests/test_resilience.py` 末尾）

```python
def test_resilience_singletons_start_clean():
    """canary：每个用例入口处，全部 resilience 模块级单例须处于初始态。

    Why：验证 conftest 的 _reset_resilience_singletons autouse fixture 生效——
    若先前用例污染了单例且 fixture 未复位，本 canary 在全量跑里会捕获（任意顺序）。
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
```

- [ ] **Step 2: 加 fixture 前先确认 canary 能抓污染（可选 sanity）**

临时在 `tests/test_fetcher_resilience.py` 末尾加一个置 OPEN 不还原的污染用例，跑 canary + 污染源（默认顺序）确认 canary FAIL；然后**删掉污染用例**。若嫌麻烦可跳过本步，直接进 Step 3（fixture 本身会在 Task 5 全量跑里被验证）。

- [ ] **Step 3: 实现 autouse fixture**（`tests/conftest.py`，加在 `_isolate_job_ledger` 之后、`tmp_db` 的 SSoT 注释块之前）

```python
# ============ M4：resilience 单例跨用例污染根治（autouse reset）============
# Why autouse：data.resilience 的 breaker/limiter 是模块级共享单例。test_fetcher_resilience
# / test_akshare_client 等测试置 OPEN 后若无 finally 还原，后续依赖单例的测试读到 OPEN
# 误判（allow_request=False → 快速返空）。本 fixture 每用例 setup 前 reset 全部单例运行态
# （不改配置），治本——任何测试忘记还原也不污染。reset() 见 CircuitBreaker/RateLimiter。
@pytest.fixture(autouse=True)
def _reset_resilience_singletons():
    from data import resilience
    for _singleton in (
        resilience.tushare_rate_limiter_basic,
        resilience.tushare_rate_limiter_special,
        resilience.fred_rate_limiter,
        resilience.akshare_limiter,
        resilience.tushare_breaker,
        resilience.fred_breaker,
        resilience.akshare_breaker,
    ):
        _singleton.reset()
    yield
```

- [ ] **Step 4: 跑 canary 确认通过**

Run: `.venv310/Scripts/python.exe -m pytest tests/test_resilience.py -v`
Expected: 全 PASS（含 canary）。canary 单跑必过（fixture 在它之前 reset）；它的价值在 Task 5 全量跑里体现。

- [ ] **Step 5: 跑既有污染源测试文件确认不回归**

Run: `.venv310/Scripts/python.exe -m pytest tests/test_fetcher_resilience.py tests/test_akshare_client.py tests/test_sync_data_lake.py tests/test_tushare_sync_quota.py -v`
Expected: 全 PASS（fixture reset 不破坏既有用例——它们入口本就 reset，现在 fixture 先 reset 一次，等价）。

- [ ] **Step 6: Commit**

```bash
git add tests/conftest.py tests/test_resilience.py
git commit -m "test(hygiene): conftest 加 autouse _reset_resilience_singletons——跨用例污染治本（M4 Task2）"
```

---

### Task 3: 清掉 4 处裸写单例内部状态

> Task 2 的 autouse fixture 已在每用例前 reset 全部单例，故测试内的「入口 reset」变冗余——删除；唯一例外是刻意置 OPEN 验证 OPEN-path 的用例，改 monkeypatch（自动还原，且不裸写私有字段）。

**Files:**
- Modify: `tests/test_fetcher_resilience.py`（删 3 处入口 reset）
- Modify: `tests/test_akshare_client.py`（删 `_reset()` helper + 调用；改 1 处 OPEN 为 monkeypatch）
- Modify: `tests/test_sync_data_lake.py`（删 1 处入口 reset + 失效 import）

**Interfaces:** 无新接口；消费 Task 2 的 fixture 兜底。

#### 3a. `tests/test_fetcher_resilience.py`——删 3 处冗余入口 reset

- [ ] **Step 1: 删 `test_breaker_trips_after_repeated_infra_errors` 入口 reset**

删掉（约 L31-33，含注释 + 两行赋值）：
```python
    # ── 复位熔断器与限流器状态，避免被先前用例/模块加载污染 ──
    tushare_breaker._state = CircuitState.CLOSED
    tushare_breaker._failure_count = 0
```
（fixture 现在每用例前 reset，入口 reset 冗余。）

- [ ] **Step 2: 删 `test_permission_error_does_not_trip_breaker` 入口 reset**

删掉（约 L77-78）：
```python
    tushare_breaker._state = CircuitState.CLOSED
    tushare_breaker._failure_count = 0
```

- [ ] **Step 3: 删 `test_fred_breaker_trips_on_rate_limit` 入口 reset**

删掉（约 L108-109）：
```python
    fred_breaker._state = CircuitState.CLOSED
    fred_breaker._failure_count = 0
```

- [ ] **Step 4: 顶部 import 若 `CircuitState` 变未使用则一并删**

检查 `tests/test_fetcher_resilience.py` 顶部 import（L18-24）：删入口 reset 后，`CircuitState` 是否还在文件内被引用（断言 `== CircuitState.OPEN`/`CLOSED` 处仍用）？若仍用则保留；若全删则从 import 列表移除 `CircuitState`。
Run: `grep -n "CircuitState" tests/test_fetcher_resilience.py` 确认引用点。

- [ ] **Step 5: 跑该文件确认绿**

Run: `.venv310/Scripts/python.exe -m pytest tests/test_fetcher_resilience.py -v`
Expected: 全 PASS（fixture 兜底，入口 reset 删除后行为等价）。

#### 3b. `tests/test_akshare_client.py`——删 helper + 改 OPEN 为 monkeypatch

- [ ] **Step 6: 删 `_reset()` helper 及其全部调用**

删 helper（L13-16）：
```python
def _reset():
    """复位熔断器内部状态，确保不被其它用例污染（与 test_fetcher_resilience 同范式）。"""
    akshare_breaker._state = CircuitState.CLOSED
    akshare_breaker._failure_count = 0
```
删文件内所有 `_reset()` 调用（L21、L38、L54、L71、L88、L102 各一行 `_reset()`）。fixture 现在兜底。

- [ ] **Step 7: 改 `test_circuit_open_returns_empty_without_calling_ak` 的刻意 OPEN 为 monkeypatch**

把（约 L107-108）：
```python
    akshare_breaker._state = CircuitState.OPEN
    akshare_breaker._opened_at = _time.monotonic()
```
改为（该测试签名已有 `monkeypatch`，确认 `def test_circuit_open_returns_empty_without_calling_ak(monkeypatch):`）：
```python
    # 刻意置 OPEN 验证 OPEN-path：用 monkeypatch 自动还原，不裸写私有字段
    # （fixture 在本用例前已 reset 为 CLOSED，monkeypatch 退出时还原回 CLOSED）
    monkeypatch.setattr(akshare_breaker, "_state", CircuitState.OPEN)
    monkeypatch.setattr(akshare_breaker, "_opened_at", _time.monotonic())
```

- [ ] **Step 8: 顶部 import 清理**

删 helper 后，`CircuitState` 是否还被引用（Step 7 的 monkeypatch 仍用 `CircuitState.OPEN`）→ 保留 `from data.resilience import CircuitState`。`akshare_breaker` 仍被 Step 7 引用 → 保留 `from data.clients.akshare_client import AKShareClient, akshare_breaker`。无需改 import。

- [ ] **Step 9: 跑该文件确认绿**

Run: `.venv310/Scripts/python.exe -m pytest tests/test_akshare_client.py -v`
Expected: 全 PASS。

#### 3c. `tests/test_sync_data_lake.py`——删 1 处入口 reset + 失效 import

- [ ] **Step 10: 删 `test_fetch_qfq_empty_daily_returns_empty` 入口 reset + 失效 import**

删掉（约 L108-110）：
```python
    from data.resilience import CircuitState, tushare_breaker
    tushare_breaker._state = CircuitState.CLOSED
    tushare_breaker._failure_count = 0
```
确认：该用例函数体（`_FakePro` / `fetch_qfq` / `df.empty` 断言）不再引用 `CircuitState`/`tushare_breaker` → 局部 import 失效，一并删。文件内**上一用例** `test_fetch_qfq_reconstructs_forward_adjusted_prices` 的 finally（L102-103）仍用 `CircuitState`/`tushare_breaker`，其 import 来源是文件顶部或该函数自己的局部 import——**不动它**（那个 finally 是 belt-and-suspenders，保留无害；如要彻底可同样删，但非本任务必需）。

- [ ] **Step 11: 跑该文件确认绿**

Run: `.venv310/Scripts/python.exe -m pytest tests/test_sync_data_lake.py -v`
Expected: 全 PASS。

- [ ] **Step 12: Commit（3a/3b/3c 合并）**

```bash
git add tests/test_fetcher_resilience.py tests/test_akshare_client.py tests/test_sync_data_lake.py
git commit -m "test(hygiene): 清掉 4 处裸写 resilience 单例内部状态——fixture 兜底后冗余（M4 Task3）"
```

---

### Task 4: 删死代码 `state_store._DEFAULT_DB_OVERRIDE`

**Files:**
- Modify: `tests/trading/test_engine.py:854-855`

**Interfaces:** 无。

- [ ] **Step 1: 确认 `_DEFAULT_DB_OVERRIDE` 是幽灵属性**

Run: `grep -rn "_DEFAULT_DB_OVERRIDE" --include="*.py" .`
Expected: 仅命中 `tests/trading/test_engine.py:855`（赋值点），`trading/state_store.py` 无此属性定义 → 确认是死代码（赋值给不存在的属性，`_connect` 读的是 `_DEFAULT_DB`，本测试 L858-859 已正确 patch `_DEFAULT_DB`）。

- [ ] **Step 2: 确认 `monkeypatch_attr` 是否也死**

Run: `grep -n "monkeypatch_attr" tests/trading/test_engine.py`
Expected: 若仅命中 L854（赋值 `= None`）无读取点 → 同为死代码，一并删；若有读取点 → 只删 L855。

- [ ] **Step 3: 删死代码行**

删（至少 L855）：
```python
    state_store._DEFAULT_DB_OVERRIDE = db_path  # 局部覆盖（_connect 读 _DEFAULT_DB）
```
若 Step 2 确认 `monkeypatch_attr` 无读取点，连 L854 一起删：
```python
    monkeypatch_attr = None  # 纯 DB 测试，无需 monkeypatch module attr
```

- [ ] **Step 4: 跑该测试函数确认绿**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_engine.py -k "is_trade_confirmed" -v`（按该用例名片段；若不确定全名，跑整个文件）
Expected: PASS（删死代码不影响行为——`_DEFAULT_DB_OVERRIDE` 从未被读）。

- [ ] **Step 5: Commit**

```bash
git add tests/trading/test_engine.py
git commit -m "test(hygiene): 删 _DEFAULT_DB_OVERRIDE 幽灵属性死代码（M4 Task4）"
```

---

### Task 5: 全量回归 + 顺序无关性验证

**Files:** 无改动（验证 task）。

- [ ] **Step 1: 全量跑（默认顺序）**

Run: `.venv310/Scripts/python.exe -m pytest tests/ -q`
Expected: 全绿，数量与近期基线一致（~1684 passed，T13-B 基线；本计划只加 3 测 + 删冗余，净 +3 左右）。canary `test_resilience_singletons_start_clean` 必须绿。

- [ ] **Step 2: 顺序无关性验证（若有 pytest-randomly）**

Run: `.venv310/Scripts/python.exe -m pytest tests/ -q -p randomly`（或若 randomly 默认开，跑两次确认种子不同都绿）
Expected: 全绿。若**没装** pytest-randomly（`grep randomly .venv310/.../site-packages/ | head` 或跑 `pip show pytest-randomly` 无输出），跳过本步——canary + 默认顺序全量绿即足够证明 fixture 生效。
Why：M4 的核心交付就是「任意顺序下不污染」。randomly 是最强证明；无则 canary 是次强证据。

- [ ] **Step 3: 验证治理点全部落地**

逐项确认：
- `grep -rn "_state = CircuitState\|_failure_count = \|_opened_at = " tests/` → 仅剩 `tests/test_sync_data_lake.py:102-103`（上一用例的 finally，保留）+ `tests/test_akshare_client.py` 的 monkeypatch（非裸赋值）。裸写私有字段已清。
- `grep -n "def reset" data/resilience.py` → 2 处（CircuitBreaker / RateLimiter）。
- `grep -n "_reset_resilience_singletons" tests/conftest.py` → 1 处 fixture。
- `grep -rn "_DEFAULT_DB_OVERRIDE" tests/` → 0 命中。

- [ ] **Step 4: 更新 tech-debt 文档订正（§0.4 第 4 项）**

`docs/architecture/06-tech-debt.md:79` 的 M4 行：污染源已定位 + 已治理。把「污染源未定位」改为「✅ 已治理（M4：reset() + autouse fixture + 裸写清理，2026-08-11）」。

- [ ] **Step 5: Commit**

```bash
git add docs/architecture/06-tech-debt.md
git commit -m "docs(tech-debt): M4 测试卫生治理收口——污染源定位+根治（06-tech-debt.md）"
```

---

## Self-Review

**1. Spec 覆盖（对照 spec §2.3 M4）**：
- ✅ 修 4 处裸写点（Task 3：fetcher×3 含 fred / akshare OPEN / sync_data_lake）—— spec §2.3 列 3 文件 4 点，全覆盖。
- ✅ 根 conftest 加 autouse `_reset_resilience_singletons`（Task 2）。
- ✅ 删 `test_engine.py:855` 死代码（Task 4）。
- ✅ reset 全部 6 个 resilience 单例（Task 2 fixture 列 7 个：4 limiter + 3 breaker；`tushare_rate_limiter` 是 basic 的别名，reset basic 即覆盖）。

**2. Placeholder 扫描**：无 TBD/TODO；每步含实际代码或确切 grep/pytest 命令。Task 3 Step 4/8/10 的 import 清理是条件性的（给了判定 grep），非占位。

**3. 类型/命名一致**：`reset()` 签名两处一致（`def reset(self) -> None`）；fixture 引用的单例名与 `data/resilience.py:282-306` 一致（`tushare_rate_limiter_basic/special`/`fred_rate_limiter`/`akshare_limiter`/`tushare_breaker`/`fred_breaker`/`akshare_breaker`）；canary 引用同。

**4. 行为等价**：`reset()` 仅 fixture 调（生产路径 grep 确认无调用）；删入口 reset 后 fixture 先 reset 一次，等价；monkeypatch 替裸赋值，语义同 + 自动还原。无业务行为变更。

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-08-11-m4-test-hygiene.md`. Two execution options:

1. **Subagent-Driven (recommended)** - 我每个 Task 派一个 fresh subagent，Task 间 review，快速迭代。
2. **Inline Execution** - 在本 session 用 executing-plans 批量执行 + 检查点。

哪种？（M4 是 5 个小 Task、纯测试基建、风险低，Inline 也合适；Subagent-Driven 更隔离。）
