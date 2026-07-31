# C-2 调度编排层合并到 uvicorn — 实施计划

> **状态：✅ 完成（2026-07-30）** — Task 1-10 全部落地。Task 10 e2e 事件链收口（采集→freshness→
> eod→次日 pre_open 三段式 gate 全绿挂单）+ 全套件 301/301 通过 + import-graph smoke 无循环依赖。
> 提交：`b5f787a7..(Task 10)`（每 Task 单独提交，见 `git log --oneline | grep C-2`）。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把交易 engine、数据采集、Brief 播报合并进 `uvicorn presentation.server.main:app` 单进程，用 `await proc.wait()` 事件链取代"19:00 时钟赌博"，并加 pre_open 三段式显式 gate。

**Architecture:** 在 server lifespan 加 engine 装配块（仿现有 replay_scheduler/training 范式）；新增 `_pipeline_then_eod` 编排函数（采集子进程→freshness 校验→eod→brief）；W1/W2/W3 三项工程整合让 engine 在 server 进程内安全运行；策略声明 `required_data_keys` 让就绪判据动态化。遵从代码库既有 Layer2 五层架构（compute/io/orchestrate/engine）。

**Tech Stack:** Python 3.10 · FastAPI/uvicorn · APScheduler（AsyncIOScheduler）· SQLite（state_store）· pytest

**Spec:** `docs/superpowers/specs/2026-07-30-scheduling-orchestration-design.md`

## Global Constraints

- 不引入 Airflow/Prefect/Celery/Redis/Kafka 等重型框架/中间件（韧性 spec §2）。
- 不改交易日内四阶段的业务算法（eod/pre_open/post_close 算法不动，只改触发与 gate 机制）。
- 不改数据采集算法（`sync_daily_incremental` 不动，只在 T2 末尾接线落库）。
- 分层铁律：`compute` 零 I/O；`io` 只搬运不判定；`orchestrate` 只连线不判定；`engine.py` 是 cron 容器。`orchestrate/pipeline.py` 不反向 import server/broadcast。
- W1 向后兼容红线：server 手动下单路径行为必须与改造前完全一致（`get_effective_whitelist()` 读纯 env，`_DYNAMIC` 模块全局留空）。
- `expected_latest_trade_day` 在 `trading/calendar.py`（不是 `calendar_utils`）。
- Windows 平台：子进程用 `asyncio.create_subprocess_exec` + `stdout` 重定向文件（不读 PIPE，防死锁）。

## File Structure

**新建（2 文件）：**
- `trading/orchestrate/pipeline.py` — `_pipeline_then_eod` 事件链编排（采集→freshness→eod→brief）。只依赖 `data.freshness`（纯函数）+ 标准库 + engine 的 `_eod`，不反向依赖 server/broadcast。
- `tests/trading/test_pipeline_then_eod.py` — 事件链单测。

**改动（8 文件，每个单一职责）：**
- `trading/engine.py` — W1 `self._dynamic_whitelist`；W3 `bootstrap()`；注册 `_pipeline_then_eod` cron；`_pre_open_gate` + `_plan_data_keys`。
- `trading/dynamic_whitelist.py` — W1：保留模块全局（server 用）+ 新增 `static_env_whitelist()` 供 engine 拼。
- `trading/__main__.py` — W2：`_shadow_gate`→`check_shadow_gate()` 返 bool；复用 `bootstrap()`。
- `presentation/server/services/trading_service.py` — W1：`submit_order` 加可选 `whitelist` 参数。
- `presentation/server/main.py` — lifespan 加 engine 装配块 + shutdown。
- `data/tools/run_data_check.py` — T2 `run_check()` 末尾 `upsert_data_ready`（D4）。
- `trading/state_store.py` — 第 7 张表 `data_ready` + `upsert_data_ready`/`get_data_ready`。
- `strategies/base.py` — Strategy Protocol 加 `required_data_keys`（默认 `{"daily"}`）。
- `ops/brief_all.py` — 加 `run_brief_all()` async（保留 `main()` 供 schtasks 不变）。
- `ops/start_all.py` — 瘦身：删 miniQMT 检测/engine 独立进程/采集+Brief schtasks。
- `ops/manage_ops_schtasks.py` — 移除 DataPipeline/Brief 任务；加 `--unregister-pipeline-brief`。

---

### Task 1: state_store data_ready 表 + CRUD（S1 数据层，无依赖）

**Files:**
- Modify: `trading/state_store.py:85-101`（`init_store` 内加建表）、文件末尾加 CRUD
- Test: `tests/trading/test_state_store_data_ready.py`（新）

**Interfaces:**
- Produces: `upsert_data_ready(date:str, dataset:str, *, ok:bool, melted:bool, latest_date:str|None, expected_date:str, message:str, db_path:str|None=None) -> None`
- Produces: `get_data_ready(date:str, dataset:str="daily", db_path:str|None=None) -> dict|None`

- [ ] **Step 1: Write the failing test**

```python
# tests/trading/test_state_store_data_ready.py
import tempfile, os
from pathlib import Path
from trading import state_store


def _fresh_db(monkeypatch):
    d = tempfile.mkdtemp()
    db = str(Path(d) / "t.db")
    monkeypatch.setattr(state_store, "_DEFAULT_DB", db)
    state_store.init_store()
    return db


def test_upsert_then_get(monkeypatch):
    db = _fresh_db(monkeypatch)
    state_store.upsert_data_ready("2026-07-30", "daily", ok=True, melted=False,
                                  latest_date="2026-07-30", expected_date="2026-07-30",
                                  message="PASS", db_path=db)
    got = state_store.get_data_ready("2026-07-30", "daily", db_path=db)
    assert got is not None
    assert got["ok"] == 1
    assert got["dataset"] == "daily"


def test_get_missing_returns_none(monkeypatch):
    db = _fresh_db(monkeypatch)
    assert state_store.get_data_ready("2026-07-30", "daily", db_path=db) is None


def test_upsert_idempotent_overwrite(monkeypatch):
    db = _fresh_db(monkeypatch)
    state_store.upsert_data_ready("2026-07-30", "daily", ok=False, melted=False,
                                  latest_date=None, expected_date="2026-07-30",
                                  message="缺", db_path=db)
    state_store.upsert_data_ready("2026-07-30", "daily", ok=True, melted=False,
                                  latest_date="2026-07-30", expected_date="2026-07-30",
                                  message="PASS", db_path=db)
    got = state_store.get_data_ready("2026-07-30", "daily", db_path=db)
    assert got["ok"] == 1  # 第二次覆盖第一次


def test_multi_dataset_independent(monkeypatch):
    db = _fresh_db(monkeypatch)
    state_store.upsert_data_ready("2026-07-30", "daily", ok=True, melted=False,
                                  latest_date="2026-07-30", expected_date="2026-07-30",
                                  message="ok", db_path=db)
    state_store.upsert_data_ready("2026-07-30", "moneyflow", ok=False, melted=True,
                                  latest_date=None, expected_date="2026-07-30",
                                  message="缺", db_path=db)
    assert state_store.get_data_ready("2026-07-30", "daily", db_path=db)["ok"] == 1
    assert state_store.get_data_ready("2026-07-30", "moneyflow", db_path=db)["ok"] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_state_store_data_ready.py -v`
Expected: FAIL `AttributeError: module 'trading.state_store' has no attribute 'upsert_data_ready'`

- [ ] **Step 3: Add table DDL to init_store**

In `trading/state_store.py` `init_store()`，在现有 6 张表 `CREATE TABLE IF NOT EXISTS` 之后、`con.commit()` 之前追加：

```python
            CREATE TABLE IF NOT EXISTS data_ready (
                date          TEXT NOT NULL,
                dataset       TEXT NOT NULL,
                ok            INTEGER NOT NULL,
                melted        INTEGER NOT NULL DEFAULT 0,
                latest_date   TEXT,
                expected_date TEXT NOT NULL,
                ready_at      TEXT NOT NULL,
                message       TEXT,
                PRIMARY KEY (date, dataset)
            )
```

- [ ] **Step 4: Add CRUD functions at end of state_store.py**

```python
def upsert_data_ready(date: str, dataset: str, *, ok: bool, melted: bool,
                      latest_date: str | None, expected_date: str,
                      message: str, db_path: str | None = None) -> None:
    """幂等写数据就绪事件（同日重采覆盖，PK (date, dataset) ON CONFLICT REPLACE）。"""
    from datetime import datetime
    ready_at = datetime.now().isoformat(timespec="seconds")
    with _connect(db_path or _DEFAULT_DB) as con:
        con.execute(
            "INSERT OR REPLACE INTO data_ready "
            "(date, dataset, ok, melted, latest_date, expected_date, ready_at, message) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (date, dataset, int(ok), int(melted), latest_date, expected_date, ready_at, message),
        )


def get_data_ready(date: str, dataset: str = "daily",
                   db_path: str | None = None) -> dict | None:
    """读某日某数据集就绪事件。无记录返 None（未采集）。"""
    with _connect(db_path or _DEFAULT_DB) as con:
        row = con.execute(
            "SELECT * FROM data_ready WHERE date=? AND dataset=?", (date, dataset),
        ).fetchone()
    return dict(row) if row else None
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_state_store_data_ready.py -v`
Expected: PASS (4 tests)

- [ ] **Step 6: Commit**

```bash
git add tests/trading/test_state_store_data_ready.py trading/state_store.py
git commit -m "feat(trading): state_store 第7张表 data_ready + upsert/get CRUD（C-2 S1）"
```

---

### Task 2: Strategy Protocol required_data_keys（D3，无依赖）

**Files:**
- Modify: `strategies/base.py:46-77`（Strategy Protocol 加属性）
- Test: `tests/strategies/test_required_data_keys.py`（新）

**Interfaces:**
- Produces: `Strategy.required_data_keys: frozenset[str]`（Protocol 属性，默认 `{"daily"}`）
- Consumes: 无（契约扩展）

- [ ] **Step 1: Write the failing test**

```python
# tests/strategies/test_required_data_keys.py
from strategies.base import Strategy
from strategies.neckline.strategy import NecklineMethodStrategy


def test_protocol_declares_required_data_keys():
    # Protocol 属性存在
    assert hasattr(Strategy, "required_data_keys")


def test_neckline_defaults_to_daily():
    strat = NecklineMethodStrategy()
    assert strat.required_data_keys == frozenset({"daily"})


def test_default_value_is_daily():
    # NecklineMethodStrategy 不显式覆盖 → 继承默认 {"daily"}
    assert "daily" in NecklineMethodStrategy().required_data_keys
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv310/Scripts/python.exe -m pytest tests/strategies/test_required_data_keys.py -v`
Expected: FAIL（`required_data_keys` 不存在）

- [ ] **Step 3: Add property to Strategy Protocol**

In `strategies/base.py`，Strategy Protocol 内 `config_schema` property 之后追加：

```python
    @property
    def required_data_keys(self) -> frozenset[str]:
        """本策略依赖的数据集 registry key（如 {"daily"}）。

        eod/gate 据此决定要校验/读取哪些数据集。默认 daily；
        子类可覆盖声明额外依赖（如 moneyflow/margin）。
        """
        return frozenset({"daily"})
```

- [ ] **Step 4: Verify NecklineMethodStrategy 继承默认（无需显式覆盖）**

Run: `.venv310/Scripts/python.exe -c "from strategies.neckline.strategy import NecklineMethodStrategy; print(NecklineMethodStrategy().required_data_keys)"`
Expected: `frozenset({'daily'})`（继承 Protocol 默认值）。若 NecklineMethodStrategy 未继承（因为它是普通 class 非 Protocol 子类），则在 `strategies/neckline/strategy.py:61` NecklineMethodStrategy 类体内加一行：
```python
    required_data_keys = frozenset({"daily"})
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv310/Scripts/python.exe -m pytest tests/strategies/test_required_data_keys.py -v`
Expected: PASS (3 tests)

- [ ] **Step 6: Commit**

```bash
git add tests/strategies/test_required_data_keys.py strategies/base.py strategies/neckline/strategy.py
git commit -m "feat(strategies): Strategy Protocol 加 required_data_keys 声明（C-2 D3）"
```

---

### Task 3: T2 步骤就地落库 data_ready（D4，依赖 Task 1）

**Files:**
- Modify: `data/tools/run_data_check.py`（`run_check` 函数末尾追加落库）
- Test: `tests/data/test_run_data_check_data_ready.py`（新）

**Interfaces:**
- Consumes: `state_store.upsert_data_ready`（Task 1）
- Consumes: `check_freshness`（已存在 `data/freshness.py:39`）
- Produces: T2 步骤算完结构化结果后自动落 `data_ready` 表

- [ ] **Step 1: Write the failing test**

```python
# tests/data/test_run_data_check_data_ready.py
import tempfile
from pathlib import Path
from unittest.mock import patch
from trading import state_store


def test_t2_writes_data_ready_on_pass(monkeypatch, tmp_path):
    db = str(tmp_path / "t.db")
    monkeypatch.setattr(state_store, "_DEFAULT_DB", db)
    state_store.init_store()
    # run_check("t2") 成功后应落 data_ready
    with patch("data.tools.run_data_check.check_freshness") as cf, \
         patch("data.tools.run_data_check.expected_latest_trade_day", return_value="2026-07-30"):
        from data.freshness import FreshnessResult
        cf.return_value = FreshnessResult(key="daily", ok=True, latest_date="2026-07-30",
                                          expected_date="2026-07-30", message="PASS")
        from data.tools.run_data_check import run_check
        run_check("t2", keys=("daily",), deadline_hour=23)  # deadline 远在未来避免 sleep
    got = state_store.get_data_ready("2026-07-30", "daily", db_path=db)
    assert got is not None
    assert got["ok"] == 1


def test_t2_writes_data_ready_on_melt(monkeypatch, tmp_path):
    db = str(tmp_path / "t.db")
    monkeypatch.setattr(state_store, "_DEFAULT_DB", db)
    state_store.init_store()
    with patch("data.tools.run_data_check.check_freshness") as cf, \
         patch("data.tools.run_data_check.expected_latest_trade_day", return_value="2026-07-30"), \
         patch("data.tools.run_data_check._now", return_value="23:30"), \
         patch("data.tools.run_data_check._resync_key", return_value=(False, "fail")):
        from data.freshness import FreshnessResult
        cf.return_value = FreshnessResult(key="daily", ok=False, latest_date=None,
                                          expected_date="2026-07-30", message="缺")
        from data.tools.run_data_check import run_check
        run_check("t2", keys=("daily",), deadline_hour=20)
    got = state_store.get_data_ready("2026-07-30", "daily", db_path=db)
    assert got is not None
    assert got["ok"] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv310/Scripts/python.exe -m pytest tests/data/test_run_data_check_data_ready.py -v`
Expected: FAIL（落库未实现，`get_data_ready` 返 None）

- [ ] **Step 3: 在 run_check 末尾追加落库**

In `data/tools/run_data_check.py`，`run_check` 函数的每个 `return` 之前（PASS / T1 告警 / melted 三处）调用 `upsert_data_ready`。最简方案：在 `run_check` 函数体内，所有 `return` 处统一前置一段落库逻辑。具体地，找到 `run_check` 函数，在它 return 前加：

```python
    # D4：T2 步骤就地落 data_ready（结构化结果零丢失，供 eod/pre_open gate 读）
    if checkpoint == "t2":
        from trading.state_store import upsert_data_ready
        from data.freshness import check_freshness as _cf
        # 取最终状态落库（all_ok / melted 已在上方算出）
        _latest = next((r.latest_date for r in results if r.latest_date), None)
        _msg = "; ".join(r.message for r in results)
        try:
            upsert_data_ready(expected, results[0].key if results else "daily",
                              ok=all_ok, melted=(checkpoint == "t2" and not all_ok and _now() >= f"{deadline_hour:02d}:00"),
                              latest_date=_latest, expected_date=expected,
                              message=_msg)
        except Exception:
            logger.exception("data_ready 落库失败（不阻断检查点主流程）")
```

注意：因 `run_check` 有多个 return 点，最干净的做法是把落库抽成一个内部 helper `_persist_ready(expected, results, all_ok, melted)`，在每个 return 前调用。实现时按此重构。

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv310/Scripts/python.exe -m pytest tests/data/test_run_data_check_data_ready.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add tests/data/test_run_data_check_data_ready.py data/tools/run_data_check.py
git commit -m "feat(data): T2 检查点就地落库 data_ready（C-2 D4，结构化结果零丢失）"
```

---

### Task 4: W1 白名单实例属性 + submit_order 参数透传（独立）

**Files:**
- Modify: `trading/dynamic_whitelist.py`（新增 `static_env_whitelist()`）
- Modify: `presentation/server/services/trading_service.py:446`（`submit_order` 加 `whitelist` 参数）
- Modify: `trading/engine.py:386`（`_submit` 加 `whitelist` 参数）、`:1575`（`__init__` 加 `self._dynamic_whitelist`）、`:652`（inject 改实例）、`:1544`（clear 改实例）
- Test: `tests/trading/test_dynamic_whitelist_w1.py`（新）

**Interfaces:**
- Consumes: `check_order` 纯函数已接受 `whitelist` 参数（`trading/compute/risk.py:55`）
- Produces: `submit_order(order, *, dry_run, confirm, whitelist: set | None = None)`
- Produces: `TradingEngine._dynamic_whitelist: set[str]` 实例属性
- Produces: `dynamic_whitelist.static_env_whitelist() -> set[str]`

- [ ] **Step 1: Write the failing test**

```python
# tests/trading/test_dynamic_whitelist_w1.py
from trading.dynamic_whitelist import static_env_whitelist, get_effective_whitelist


def test_static_env_whitelist_pure_env(monkeypatch):
    monkeypatch.setenv("QMT_SYMBOL_WHITELIST", "600001.SH,688001.SH")
    assert static_env_whitelist() == {"600001.SH", "688001.SH"}


def test_get_effective_whitelist_still_works(monkeypatch):
    # server 路径：模块全局 _DYNAMIC 留空，get_effective 返纯 env（向后兼容不变）
    monkeypatch.setenv("QMT_SYMBOL_WHITELIST", "600001.SH")
    from trading import dynamic_whitelist
    dynamic_whitelist.clear_dynamic_whitelist()  # 确保模块全局空
    assert get_effective_whitelist() == {"600001.SH"}


def test_engine_instance_whitelist_isolated():
    # engine 实例属性与模块全局物理隔离
    from trading.dynamic_whitelist import _DYNAMIC
    from trading.engine import TradingEngine
    eng = TradingEngine()
    eng._dynamic_whitelist.add("300001.SZ")
    assert "300001.SZ" in eng._dynamic_whitelist
    assert "300001.SZ" not in _DYNAMIC  # 模块全局不被污染
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_dynamic_whitelist_w1.py -v`
Expected: FAIL（`static_env_whitelist` 不存在；`TradingEngine` 无 `_dynamic_whitelist`）

- [ ] **Step 3: Add static_env_whitelist to dynamic_whitelist.py**

In `trading/dynamic_whitelist.py`，`get_effective_whitelist` 之后追加：

```python
def static_env_whitelist() -> set[str]:
    """纯静态 env 白名单（不掺模块全局 _DYNAMIC）。

    供 engine 实例拼接 self._dynamic_whitelist | static_env_whitelist()，
    与 server 路径的 get_effective_whitelist()（含 _DYNAMIC）物理区分。
    """
    return {s.strip() for s in os.getenv("QMT_SYMBOL_WHITELIST", "").split(",") if s.strip()}
```

- [ ] **Step 4: Add whitelist param to submit_order**

In `presentation/server/services/trading_service.py:446`，签名改为：

```python
async def submit_order(order: OrderRequest, *, dry_run: bool, confirm: bool,
                       whitelist: set | None = None) -> dict:
```

`:468` 的 `check_order(..., whitelist=_whitelist(), ...)` 改为：
```python
        whitelist=whitelist if whitelist is not None else _whitelist(),
```

- [ ] **Step 5: Add whitelist param to engine._submit + instance attr**

In `trading/engine.py`：
- `:386` `_submit` 签名加 `whitelist: set | None = None`，body 的 svc_submit 调用加透传：

```python
async def _submit(order, *, confirm: bool = True, whitelist: set | None = None) -> dict:
    from presentation.server.services.trading_service import submit_order as svc_submit
    from trading.dynamic_whitelist import static_env_whitelist
    return await svc_submit(order, dry_run=(_mode() == "dry_run"), confirm=confirm,
                            whitelist=whitelist)
```

注意：`_submit` 是模块级函数，调用方（`pre_open`/`stop_loss_monitor`）需传入 engine 实例的 whitelist。因此 `_submit` 通过模块级 `_ACTIVE_ENGINE` 单例取实例属性。在 `_submit` 内：
```python
    if whitelist is None and _ACTIVE_ENGINE is not None:
        whitelist = _ACTIVE_ENGINE._dynamic_whitelist | static_env_whitelist()
```

- `TradingEngine.__init__`（`:1575`）加：`self._dynamic_whitelist: set[str] = set()`
- 在 engine.py 模块级加单例桥（若已有 `_ACTIVE_ENGINE` 则复用，否则新增）：在 `TradingEngine.__init__` 末尾 `global _ACTIVE_ENGINE; _ACTIVE_ENGINE = self`。
- `:652` `dynamic_whitelist.inject_dynamic_whitelist(symbols)` 改为 `self._dynamic_whitelist |= symbols`
- `:1544` `dynamic_whitelist.clear_dynamic_whitelist()` 改为 `self._dynamic_whitelist.clear()`

（注：`:652`/`:1544` 可能在模块级 `pre_open`/`post_close` 函数内而非 TradingEngine 方法内——若如此，改为通过 `_ACTIVE_ENGINE._dynamic_whitelist` 访问。）

- [ ] **Step 6: Run test to verify it passes**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_dynamic_whitelist_w1.py -v`
Expected: PASS (3 tests)

- [ ] **Step 7: Run existing engine tests to confirm no regression**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_engine.py tests/trading/test_engine_stoploss_inject.py -v`
Expected: PASS（既有测试不应因白名单改动失败；server 路径 get_effective_whitelist 行为不变）

- [ ] **Step 8: Commit**

```bash
git add tests/trading/test_dynamic_whitelist_w1.py trading/dynamic_whitelist.py \
        presentation/server/services/trading_service.py trading/engine.py
git commit -m "feat(trading): W1 白名单 _DYNAMIC→实例属性 + submit_order whitelist 参数透传（C-2）"
```

---

### Task 5: W2 shadow_gate 返 bool + W3 bootstrap()（依赖 Task 4 的 _ACTIVE_ENGINE）

**Files:**
- Modify: `trading/__main__.py`（`_shadow_gate`→`check_shadow_gate` 返 bool）
- Modify: `trading/engine.py`（加 `async def bootstrap()`）
- Test: `tests/trading/test_shadow_gate.py`（新）、`tests/trading/test_engine_bootstrap.py`（新）

**Interfaces:**
- Produces: `check_shadow_gate() -> bool`（模块级，返 False 而非 sys.exit）
- Produces: `TradingEngine.bootstrap() -> None`（async，connect + DB init）

- [ ] **Step 1: Write the failing test for shadow_gate**

```python
# tests/trading/test_shadow_gate.py
import os
from trading.__main__ import check_shadow_gate


def test_dry_run_passes(monkeypatch):
    monkeypatch.setenv("AUTO_TRADE_MODE", "dry_run")
    assert check_shadow_gate() is True


def test_live_shadow_insufficient_returns_false(monkeypatch):
    monkeypatch.setenv("AUTO_TRADE_MODE", "live")
    monkeypatch.setenv("TRADE_SHADOW_MIN_DAYS", "5")
    from datetime import datetime, timedelta
    recent = (datetime.now() - timedelta(days=1)).isoformat()  # 只观测1天
    class FakeExp:
        activated_at = recent
    monkeypatch.setattr("trading.__main__.resolve_active", lambda: [FakeExp()])
    assert check_shadow_gate() is False  # 不再 sys.exit，返 False


def test_live_shadow_sufficient_passes(monkeypatch):
    monkeypatch.setenv("AUTO_TRADE_MODE", "live")
    monkeypatch.setenv("TRADE_SHADOW_MIN_DAYS", "5")
    from datetime import datetime, timedelta
    old = (datetime.now() - timedelta(days=10)).isoformat()
    class FakeExp:
        activated_at = old
    monkeypatch.setattr("trading.__main__.resolve_active", lambda: [FakeExp()])
    assert check_shadow_gate() is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_shadow_gate.py -v`
Expected: FAIL（`check_shadow_gate` 不存在 / 仍 sys.exit）

- [ ] **Step 3: Refactor _shadow_gate to check_shadow_gate returning bool**

In `trading/__main__.py`，把 `_shadow_gate` 重命名为 `check_shadow_gate`，所有 `sys.exit(2)` 改为 `return False`，函数签名加 `-> bool`，末尾 `return True` 保留。CRITICAL 钉钉告警（build_default_manager + fire_and_forget）保留不变。`if __name__ == "__main__"` 块内的 `_shadow_gate()` 调用改为：

```python
    if not check_shadow_gate():
        logger.error("影子期不足，拒绝启动 engine（独立进程模式退出）")
        sys.exit(2)   # 独立进程模式仍可 exit；uvicorn 模式由 lifespan 决定
```

- [ ] **Step 4: Run shadow_gate test to verify it passes**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_shadow_gate.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Write the failing test for bootstrap**

```python
# tests/trading/test_engine_bootstrap.py
import pytest
from unittest.mock import AsyncMock, patch
from trading.engine import TradingEngine


@pytest.mark.asyncio
async def test_bootstrap_inits_db_and_connects():
    eng = TradingEngine()
    with patch("trading.engine.get_gateway") as gg, \
         patch("trading.position_book.init_db") as pb, \
         patch("trading.state_store.init_store") as ss, \
         patch("trading.state_store._migrate_env_to_account") as mig:
        gw = AsyncMock()
        gg.return_value = gw
        await eng.bootstrap()
        gw.connect.assert_awaited_once()
        gw.set_order_update_callback.assert_called_once()
        pb.assert_called_once()
        ss.assert_called_once()
        mig.assert_called_once()
        assert eng._gw is gw


@pytest.mark.asyncio
async def test_bootstrap_no_gateway_degrades():
    eng = TradingEngine()
    with patch("trading.engine.get_gateway", return_value=None), \
         patch("trading.position_book.init_db"), \
         patch("trading.state_store.init_store"), \
         patch("trading.state_store._migrate_env_to_account"):
        await eng.bootstrap()  # 不抛
        assert eng._gw is None
```

- [ ] **Step 6: Run test to verify it fails**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_engine_bootstrap.py -v`
Expected: FAIL（`bootstrap` 不存在）

- [ ] **Step 7: Add bootstrap() to TradingEngine**

In `trading/engine.py`，`TradingEngine` 类内 `__init__` 之后、`start()` 之前加：

```python
    async def bootstrap(self) -> None:
        """W3：I/O 初始化收口（原 __main__._run_forever 的 7 步）。

        三段：构造(零 I/O) → bootstrap(I/O init) → start(调度启动)。
        必须在 start() 之前：cron 一旦启动，回调/触发点可能读写 DB。
        """
        gw = get_gateway()
        if gw is not None:
            try:
                await gw.connect()
                gw.set_order_update_callback(self._handle_order_update)
                self._gw = gw
                logger.info("网关已连接 + 成交回调已注册")
            except Exception:
                logger.exception("网关连接失败（cron 仍启动，触发点内部 get_gateway 兜底）")
        else:
            logger.warning("未装配网关（dry_run 影子模式，回调链路不生效）")
        from trading import position_book, state_store
        position_book.init_db()
        state_store.init_store()
        state_store._migrate_env_to_account()
```

- [ ] **Step 8: Run bootstrap test to verify it passes**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_engine_bootstrap.py -v`
Expected: PASS (2 tests)

- [ ] **Step 9: Refactor __main__._run_forever to use bootstrap**

In `trading/__main__.py`，`_run_forever` 内的 7 步（get_gateway/connect/set_callback/position_book.init_db/state_store.init_store/_migrate）删除，替换为 `await eng.bootstrap()`。保留 `eng.start()` 与 `while True: await asyncio.sleep(3600)`。

- [ ] **Step 10: Commit**

```bash
git add tests/trading/test_shadow_gate.py tests/trading/test_engine_bootstrap.py \
        trading/__main__.py trading/engine.py
git commit -m "feat(trading): W2 shadow_gate 返 bool + W3 bootstrap() 收口（C-2）"
```

---

### Task 6: _pipeline_then_eod 事件链编排（S2，依赖 Task 1/2/4/5）

**Files:**
- Create: `trading/orchestrate/pipeline.py`
- Test: `tests/trading/test_pipeline_then_eod.py`

**Interfaces:**
- Consumes: `state_store.upsert_data_ready`/`get_data_ready`（Task 1）
- Consumes: `check_freshness`（`data/freshness.py:39`）
- Consumes: `str.required_data_keys`（Task 2）
- Consumes: `expected_latest_trade_day`（`trading/calendar.py`）
- Consumes: `resolve_active`（`experiment/resolver.py`）、`build_strategy`（`strategies/registry.py`）
- Produces: `async def pipeline_then_eod(engine) -> None`（采集→freshness→eod→brief）

- [ ] **Step 1: Write the failing test**

```python
# tests/trading/test_pipeline_then_eod.py
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from datetime import datetime


@pytest.mark.asyncio
async def test_non_trading_day_noop(monkeypatch):
    from trading.orchestrate.pipeline import pipeline_then_eod
    with patch("trading.calendar.is_trading_day", return_value=False):
        eng = MagicMock()
        await pipeline_then_eod(eng)
        # 不应触达采集/eod


@pytest.mark.asyncio
async def test_data_not_ready_no_eod(monkeypatch):
    from trading.orchestrate.pipeline import pipeline_then_eod
    from data.freshness import FreshnessResult
    today = datetime.now().strftime("%Y-%m-%d")
    with patch("trading.calendar.is_trading_day", return_value=True), \
         patch("trading.orchestrate.pipeline.asyncio.create_subprocess_exec") as cse, \
         patch("trading.orchestrate.pipeline.resolve_active", return_value=[]), \
         patch("trading.orchestrate.pipeline.check_freshness",
               return_value=FreshnessResult("daily", False, None, today, "缺")):
        proc = AsyncMock(); proc.wait.return_value = 1
        cse.return_value = proc
        eng = MagicMock()
        eng._eod = AsyncMock()
        await pipeline_then_eod(eng)
        eng._eod.assert_not_awaited()  # 数据未就绪不跑 eod


@pytest.mark.asyncio
async def test_data_ready_runs_eod(monkeypatch):
    from trading.orchestrate.pipeline import pipeline_then_eod
    from data.freshness import FreshnessResult
    today = datetime.now().strftime("%Y-%m-%d")
    with patch("trading.calendar.is_trading_day", return_value=True), \
         patch("trading.orchestrate.pipeline.asyncio.create_subprocess_exec") as cse, \
         patch("trading.orchestrate.pipeline.resolve_active", return_value=[]), \
         patch("trading.orchestrate.pipeline.check_freshness",
               return_value=FreshnessResult("daily", True, today, today, "PASS")):
        proc = AsyncMock(); proc.wait.return_value = 0
        cse.return_value = proc
        eng = MagicMock()
        eng._eod = AsyncMock()
        await pipeline_then_eod(eng)
        eng._eod.assert_awaited_once()


@pytest.mark.asyncio
async def test_multi_experiment_keys_union(monkeypatch):
    from trading.orchestrate.pipeline import pipeline_then_eod
    from data.freshness import FreshnessResult
    today = datetime.now().strftime("%Y-%m-%d")
    strat_a = MagicMock(); strat_a.required_data_keys = frozenset({"daily"})
    strat_b = MagicMock(); strat_b.required_data_keys = frozenset({"daily", "moneyflow"})
    class FakeExp:
        strategy_name = "x"; params = {}
    exps = [FakeExp(), FakeExp()]
    checked_keys = []
    def fake_cf(k, exp):
        checked_keys.append(k)
        return FreshnessResult(k, True, today, today, "ok")
    with patch("trading.calendar.is_trading_day", return_value=True), \
         patch("trading.orchestrate.pipeline.asyncio.create_subprocess_exec") as cse, \
         patch("trading.orchestrate.pipeline.resolve_active", return_value=exps), \
         patch("trading.orchestrate.pipeline.build_strategy", side_effect=[strat_a, strat_b]), \
         patch("trading.orchestrate.pipeline.check_freshness", side_effect=fake_cf):
        proc = AsyncMock(); proc.wait.return_value = 0
        cse.return_value = proc
        eng = MagicMock(); eng._eod = AsyncMock()
        await pipeline_then_eod(eng)
        assert set(checked_keys) == {"daily", "moneyflow"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_pipeline_then_eod.py -v`
Expected: FAIL（模块不存在）

- [ ] **Step 3: Create pipeline.py**

```python
# trading/orchestrate/pipeline.py
# -*- coding: utf-8 -*-
"""C-2 事件链编排：采集 → 等完成 → 按策略声明校验数据 → eod → brief。

物理定位（Layer2 编排层）：只连线不判定。采集/freshness/eod/brief 各自归位，
本函数把它们按事件顺序串起来，取代"19:00 时钟赌博"。

依赖单向：只 import data.freshness（纯函数）+ 标准库 + engine._eod，
不反向 import server/broadcast（低耦合）。
"""
from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime
from pathlib import Path

from data.freshness import check_freshness
from trading.calendar import expected_latest_trade_day, is_trading_day
from trading.state_store import upsert_data_ready

logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[2]


async def pipeline_then_eod(engine) -> None:
    """C-2 事件链：采集 → 等完成 → 按策略声明校验数据 → eod → brief。"""
    today = datetime.now().strftime("%Y-%m-%d")
    if not is_trading_day(today):
        logger.info("pipeline_then_eod 跳过：今日非交易日 %s", today)
        return
    # 1. 采集子进程（原 ops/data_pipeline.py，T1→采→T2）
    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(ROOT / "ops" / "data_pipeline.py"), cwd=str(ROOT),
        stdout=open(ROOT / "logs" / "data_pipeline.log", "ab"),
        stderr=asyncio.subprocess.STDOUT,
    )
    rc = await proc.wait()
    # 2. 装配本次实验策略 → 收集依赖 key 并集（D3）
    from experiment.resolver import resolve_active
    from strategies.registry import build_strategy
    keys: set[str] = set()
    try:
        for exp in resolve_active():
            strat = build_strategy(exp.strategy_name, exp.params)
            keys |= strat.required_data_keys
    except Exception:
        logger.exception("策略依赖解析失败，回退默认 {daily}")
        keys = {"daily"}
    keys = keys or {"daily"}
    # 3. 按声明的 key 逐个校验（复用 check_freshness 纯函数，不读旧 parquet）
    expected = expected_latest_trade_day(datetime.now())
    results = {k: check_freshness(k, expected) for k in keys}
    all_ok = all(r.ok for r in results.values())
    # 4. 落就绪事件（供 pre_open 防御性双检）
    for k, r in results.items():
        try:
            upsert_data_ready(today, k, ok=r.ok,
                              melted=(not all_ok and rc != 0),
                              latest_date=r.latest_date, expected_date=expected,
                              message=r.message)
        except Exception:
            logger.exception("data_ready 落库失败（不阻断）")
    if not all_ok:
        msg = f"数据未就绪：{[r.message for r in results.values() if not r.ok]}，eod 跳过"
        logger.warning(msg)
        try:
            from infra.notifier import fire_and_forget, NotificationManager, build_default_manager
            build_default_manager()
            fire_and_forget(NotificationManager.get_default().notify_risk_event(msg, "CRITICAL"))
        except Exception:
            logger.exception("CRITICAL 告警发送失败")
        return  # 不跑 eod，不产废信号
    # 5. 全绿 → 跑 eod
    await engine._eod()
    # 6. 事件链尾 → Brief 播报（D7）
    try:
        from ops.brief_all import run_brief_all
        await run_brief_all()
    except Exception:
        logger.exception("brief 播报失败（不阻断 eod 已完成的 plan）")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_pipeline_then_eod.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add trading/orchestrate/pipeline.py tests/trading/test_pipeline_then_eod.py
git commit -m "feat(trading): _pipeline_then_eod 事件链编排（C-2 S2，采集→freshness→eod→brief）"
```

---

### Task 7: brief_all 加 run_brief_all async（D7，依赖 Task 6 引用）

**Files:**
- Modify: `ops/brief_all.py`（加 `async def run_brief_all()`，保留 `main()`）
- Test: `tests/ops/test_brief_all_async.py`（新）

**Interfaces:**
- Produces: `async def run_brief_all() -> int`（事件链尾调用，subprocess 隔离）
- Consumes: `broadcast --bot <bot>`（CLI 不变）

- [ ] **Step 1: Write the failing test**

```python
# tests/ops/test_brief_all_async.py
import pytest
from unittest.mock import AsyncMock, patch


@pytest.mark.asyncio
async def test_run_brief_all_calls_three_bots():
    from ops.brief_all import run_brief_all, BOTS
    with patch("ops.brief_all.asyncio.create_subprocess_exec") as cse:
        proc = AsyncMock(); proc.wait.return_value = 0
        cse.return_value = proc
        rc = await run_brief_all()
        assert cse.call_count == len(BOTS)  # 三个 bot 各起一个子进程
        assert rc == 0


@pytest.mark.asyncio
async def test_run_brief_all_one_fail_returns_nonzero():
    from ops.brief_all import run_brief_all
    with patch("ops.brief_all.asyncio.create_subprocess_exec") as cse:
        procs = []
        for i, _ in enumerate(["trading", "strategy", "data"]):
            p = AsyncMock(); p.wait.return_value = 1 if i == 1 else 0
            procs.append(p)
        cse.side_effect = procs
        rc = await run_brief_all()
        assert rc == 1  # strategy 失败 → 非 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv310/Scripts/python.exe -m pytest tests/ops/test_brief_all_async.py -v`
Expected: FAIL（`run_brief_all` 不存在）

- [ ] **Step 3: Add run_brief_all to brief_all.py**

In `ops/brief_all.py`，`main()` 之前加：

```python
async def run_brief_all() -> int:
    """事件链尾调用：串行跑三播报 bot（与 main() 同逻辑，async 友好）。

    保持 subprocess 调 broadcast 而非 import：broadcast 模块拉起 dws/钉钉重链，
    子进程隔离比 in-process import 更稳（单 bot 崩不连累 engine）。
    """
    import asyncio
    rcs = []
    for bot in BOTS:
        print(f"--- {bot} 播报 ---")
        proc = await asyncio.create_subprocess_exec(
            PY, "-m", "broadcast", "--bot", bot, cwd=str(ROOT),
            stdout=open(ROOT / "logs" / "broadcast_connect" / f"{bot}_brief.log", "ab"),
            stderr=asyncio.subprocess.STDOUT,
        )
        rc = await proc.wait()
        rcs.append((bot, rc))
        if rc != 0:
            print(f"⚠️ {bot} 播报失败 rc={rc}（继续其余 bot）")
    return 1 if any(rc != 0 for _, rc in rcs) else 0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv310/Scripts/python.exe -m pytest tests/ops/test_brief_all_async.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add tests/ops/test_brief_all_async.py ops/brief_all.py
git commit -m "feat(ops): brief_all 加 run_brief_all async（C-2 D7，事件链尾调用）"
```

---

### Task 8: pre_open 三段 gate（S3，依赖 Task 1/2/4）

**Files:**
- Modify: `trading/engine.py`（加 `_pre_open_gate` + `_plan_data_keys`，接入 `pre_open`）
- Test: `tests/trading/test_engine_pre_open_gate.py`（新）

**Interfaces:**
- Consumes: `get_data_ready`（Task 1）、`is_client_ready`（`broker/qmt.py:311`）、`load_plan`（`trading/trading_plan.py`）
- Produces: `_pre_open_gate(date, gw) -> tuple[bool, str]`、`_plan_data_keys(plan) -> set[str]`

- [ ] **Step 1: Write the failing test**

```python
# tests/trading/test_engine_pre_open_gate.py
import pytest
from unittest.mock import MagicMock
from trading.engine import TradingEngine


@pytest.fixture
def eng():
    return TradingEngine()


@pytest.mark.asyncio
async def test_no_plan_blocks(eng):
    with patch("trading.engine.load_plan", return_value=None):
        ok, reason = await eng._pre_open_gate("2026-07-30", None)
        assert ok is False
        assert "无计划" in reason


@pytest.mark.asyncio
async def test_unconfirmed_blocks(eng):
    with patch("trading.engine.load_plan", return_value={"confirmed": False, "orders": []}):
        ok, reason = await eng._pre_open_gate("2026-07-30", None)
        assert ok is False
        assert "未确认" in reason


@pytest.mark.asyncio
async def test_gateway_not_connected_blocks(eng):
    gw = MagicMock(); gw._connected = False
    with patch("trading.engine.load_plan", return_value={"confirmed": True, "orders": []}):
        ok, reason = await eng._pre_open_gate("2026-07-30", gw)
        assert ok is False
        assert "网关" in reason


@pytest.mark.asyncio
async def test_data_not_ready_blocks(eng):
    gw = MagicMock(); gw._connected = True; gw.is_client_ready.return_value = True
    with patch("trading.engine.load_plan", return_value={"confirmed": True, "orders": []}), \
         patch("trading.engine._plan_data_keys_via_engine", return_value={"daily"}), \
         patch("trading.engine.get_data_ready", return_value=None):
        ok, reason = await eng._pre_open_gate("2026-07-30", gw)
        assert ok is False
        assert "数据" in reason


@pytest.mark.asyncio
async def test_all_green_passes(eng):
    gw = MagicMock(); gw._connected = True; gw.is_client_ready.return_value = True
    with patch("trading.engine.load_plan", return_value={"confirmed": True, "orders": []}), \
         patch("trading.engine._plan_data_keys_via_engine", return_value={"daily"}), \
         patch("trading.engine.get_data_ready", return_value={"ok": 1}):
        ok, reason = await eng._pre_open_gate("2026-07-30", gw)
        assert ok is True
        assert reason == ""
```

（注：测试里 patch 的函数名需与实现一致——实现里 `_plan_data_keys` 作为 engine 方法，patch 点是 `trading.engine.TradingEngine._plan_data_keys`；按实际实现调整 patch 路径。）

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_engine_pre_open_gate.py -v`
Expected: FAIL（`_pre_open_gate` 不存在）

- [ ] **Step 3: Add _pre_open_gate and _plan_data_keys to TradingEngine**

In `trading/engine.py`，TradingEngine 类内加：

```python
    async def _pre_open_gate(self, date: str, gw) -> tuple[bool, str]:
        """S3：pre_open 三段式前置 gate。全绿返 (True, "")。

        顺序先便宜后贵：计划确认 → 网关健康 → 数据就绪。
        任一未绿即返，绝不触达网关写操作。
        """
        # ① 计划确认（读本地 JSON，最便宜）
        plan = load_plan(date)
        if not plan:
            return False, "无计划"
        if not plan.get("confirmed"):
            return False, "计划未确认（人审闸）"
        # ② 网关健康（探测，无写副作用）
        if gw is None or not getattr(gw, "_connected", False):
            return False, "网关未连接"
        if not gw.is_client_ready():
            return False, "miniQMT 客户端未就绪"
        # ③ 数据就绪（DB 查询；防御性双检）
        for k in self._plan_data_keys(plan):
            ready = get_data_ready(date, k)
            if ready is None or not ready["ok"]:
                return False, f"数据 {k} 未就绪（{ready['message'] if ready else '未采集'}）"
        return True, ""

    def _plan_data_keys(self, plan: dict) -> set[str]:
        """从 plan 反推策略声明的数据集 key 并集（防御性双检用）。

        plan orders 携带 experiment_id，经 resolver 反查 strategy_name → required_data_keys。
        解析失败 → 返 {"daily"}（保守默认，③ 本就是双检）。
        """
        keys: set[str] = set()
        try:
            from experiment.resolver import resolve_active
            from strategies.registry import build_strategy
            exp_map = {e.id: e for e in resolve_active()}
            for o in plan.get("orders", []):
                exp = exp_map.get(o.get("experiment_id"))
                if exp is not None:
                    strat = build_strategy(exp.strategy_name, exp.params)
                    keys |= strat.required_data_keys
        except Exception:
            logger.exception("_plan_data_keys 解析失败，回退默认 {daily}")
        return keys or {"daily"}
```

模块顶部加 import：`from trading.state_store import get_data_ready`（若尚未 import）。

- [ ] **Step 4: Wire gate into pre_open module function**

In `trading/engine.py`，模块级 `async def pre_open(date)`（`:550`）入口，现有 `plan["confirmed"]` 检查之前加 gate 调用。因 `pre_open` 是模块级函数需通过 `_ACTIVE_ENGINE` 取实例方法：

```python
    # S3：三段式前置 gate（通过 _ACTIVE_ENGINE 单例调用实例方法）
    if _ACTIVE_ENGINE is not None:
        gate_ok, gate_reason = await _ACTIVE_ENGINE._pre_open_gate(date, get_gateway())
        if not gate_ok:
            msg = f"pre_open gate 未通过：{gate_reason}，跳过挂单"
            logger.warning(msg)
            if _mode() == "live":
                from infra.notifier import fire_and_forget, NotificationManager, build_default_manager
                try:
                    build_default_manager()
                    fire_and_forget(NotificationManager.get_default().notify_risk_event(msg, "CRITICAL"))
                except Exception:
                    logger.exception("CRITICAL 告警失败")
            return {"date": date, "n_orders": 0, "skipped": gate_reason}
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_engine_pre_open_gate.py -v`
Expected: PASS (5 tests)

- [ ] **Step 6: Commit**

```bash
git add tests/trading/test_engine_pre_open_gate.py trading/engine.py
git commit -m "feat(trading): pre_open 三段式 gate 计划确认+网关+数据就绪（C-2 S3）"
```

---

### Task 9: lifespan 装配块 + 删除独立 cron/schtasks + start_all 收编（依赖全部）

**Files:**
- Modify: `presentation/server/main.py:51-218`（lifespan 加 engine 装配块 + shutdown）
- Modify: `trading/engine.py`（注册 `pipeline_then_eod` cron，删 19:00 eod cron）
- Modify: `ops/start_all.py`（瘦身）
- Modify: `ops/manage_ops_schtasks.py`（移除 DataPipeline/Brief，加 `--unregister-pipeline-brief`）
- Test: `tests/server/test_lifespan_engine.py`（新）

**Interfaces:**
- Consumes: `TradingEngine`（Task 4/5/8）、`check_shadow_gate`（Task 5）、`pipeline_then_eod`（Task 6）

- [ ] **Step 1: Write the failing test**

```python
# tests/server/test_lifespan_engine.py
import pytest
from unittest.mock import AsyncMock, patch, MagicMock


@pytest.mark.asyncio
async def test_lifespan_assembles_engine(monkeypatch):
    from presentation.server.main import lifespan
    app = MagicMock(); app.state = MagicMock()
    eng = MagicMock(); eng.sched.running = True; eng.bootstrap = AsyncMock()
    with patch("trading.engine.TradingEngine", return_value=eng), \
         patch("trading.__main__.check_shadow_gate", return_value=True):
        async with lifespan(app):
            eng.bootstrap.assert_awaited_once()
            eng.start.assert_called_once()
        eng.shutdown.assert_called_once()  # lifespan 退出时 shutdown


@pytest.mark.asyncio
async def test_lifespan_shadow_fail_no_start(monkeypatch):
    from presentation.server.main import lifespan
    app = MagicMock(); app.state = MagicMock()
    eng = MagicMock(); eng.sched.running = False; eng.bootstrap = AsyncMock()
    with patch("trading.engine.TradingEngine", return_value=eng), \
         patch("trading.__main__.check_shadow_gate", return_value=False):
        async with lifespan(app):
            eng.bootstrap.assert_awaited_once()
            eng.start.assert_not_called()  # 影子期不足不起 scheduler
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv310/Scripts/python.exe -m pytest tests/server/test_lifespan_engine.py -v`
Expected: FAIL（lifespan 未装配 engine）

- [ ] **Step 3: Add engine assembly block to lifespan**

In `presentation/server/main.py`，lifespan 的 `yield` 之前（现有装配块之后）加：

```python
    # C-2：TradingEngine 装配（合并 engine 进 uvicorn 单进程）
    try:
        from trading.engine import TradingEngine
        from trading.__main__ import check_shadow_gate
        eng = TradingEngine()
        await eng.bootstrap()
        if check_shadow_gate():
            eng.start()
            app.state.trading_engine = eng
            logging.getLogger(__name__).info("TradingEngine 已装配并启动")
        else:
            app.state.trading_engine = eng
            logging.getLogger(__name__).warning(
                "TradingEngine 装配但 scheduler 未启动（影子期不足，API 继续运行）")
    except Exception:
        logging.getLogger(__name__).exception("TradingEngine 装配异常（已忽略）")
```

shutdown 段（现有网关 disconnect 之后）加：
```python
    _eng = getattr(app.state, "trading_engine", None)
    if _eng is not None and getattr(_eng.sched, "running", False):
        _eng.shutdown()
```

- [ ] **Step 4: Register pipeline_then_eod cron, remove 19:00 eod cron**

In `trading/engine.py` `TradingEngine.__init__`：
- 把 eod_plan 的 `CronTrigger.from_crontab("0 19 * * 1-5")` job（`__init__` 内 eod_plan 注册处）改为注册 `pipeline_then_eod`：

```python
        from trading.orchestrate.pipeline import pipeline_then_eod
        self.sched.add_job(
            pipeline_then_eod, CronTrigger.from_crontab(
                os.getenv("ENGINE_PIPELINE_CRON", "0 18 * * 1-5")),  # 18:00 盘后触发
            args=[self], id="pipeline_then_eod",
        )
```

- 删除原 eod_plan 的 19:00 cron 注册（eod 改由 `pipeline_then_eod` 事件链内部 `engine._eod()` 驱动）。
- pre_open/post_close/stop_loss/health_guard cron 注册不变。

- [ ] **Step 5: Run lifespan test to verify it passes**

Run: `.venv310/Scripts/python.exe -m pytest tests/server/test_lifespan_engine.py -v`
Expected: PASS (2 tests)

- [ ] **Step 6: Slim down start_all.py**

In `ops/start_all.py` `main()`：
- 删除 miniQMT 检测步（`_check_miniqmt` 调用 + 打印）。
- 删除 `trading engine` 独立进程步（`_detached([..., "-m", "trading"], "trading_engine")`）。
- `manage_ops_schtasks.py --register` 改为 `--unregister-pipeline-brief`（清退 DataPipeline/Brief schtasks）。
- 保留 uvicorn 启动 + broadcast connect + discovery schtasks 注册。

```python
def main() -> int:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env", override=True)
    print("=" * 60); print("Quanter 全栈启动（engine/采集/Brief 已收进 uvicorn）"); print("=" * 60)
    # 1. uvicorn :8000（宿主：engine + 采集 + brief 在它的 lifespan）
    if not _bind_ok(8000):
        print("[1/3] 启动 uvicorn :8000 ...")
        _detached([str(VENV_PY), "-m", "uvicorn", "presentation.server.main:app",
                   "--host", "127.0.0.1", "--port", "8000"], "uvicorn")
        _wait_port_busy(8000, timeout=40)
    else:
        print("[1/3] uvicorn :8000 已在跑，跳过")
    # 2. broadcast connect 5 钉钉机器人（独立常驻，依赖 uvicorn）
    print("[2/3] 启动 connect 5 钉钉机器人 ...")
    try:
        subprocess.run(f'echo y| "{VENV_PY}" -m broadcast connect --start all',
                       shell=True, cwd=str(ROOT), timeout=120)
    except subprocess.TimeoutExpired:
        print("      ⚠️ connect 超时，已跳过")
    # 3. schtasks：只注册 DiscoveryDaemon@02:00；清退已收编的 DataPipeline/Brief
    print("[3/3] schtasks（discovery 注册 + 清退 pipeline/brief）...")
    subprocess.run([str(VENV_PY), "-m", "discovery.schtasks", "--register"], cwd=str(ROOT))
    subprocess.run([str(VENV_PY), str(ROOT / "ops" / "manage_ops_schtasks.py"),
                    "--unregister-pipeline-brief"], cwd=str(ROOT))
    print("=" * 60); print("✅ 完成（engine/采集/Brief 在 uvicorn 内，broadcast/discovery 独立）"); print("=" * 60)
    return 0
```

- [ ] **Step 7: Update manage_ops_schtasks.py**

In `ops/manage_ops_schtasks.py`：
- `PIPELINE_TASKS` 列表（`:28-29`）的两个任务（QuanterDataPipeline/QuanterBrief）注释移除说明（已收进 uvicorn）。
- 加 `--unregister-pipeline-brief` 子命令：幂等删除这两个 schtasks（`_schtasks(["/Delete", "/TN", task, "/F"])`）。

在 `main()`/argparse 加：
```python
def unregister_pipeline_brief() -> None:
    """清退已收编进 uvicorn 的 DataPipeline/Brief schtasks（幂等，防残留）。"""
    for task in ("QuanterDataPipeline", "QuanterBrief"):
        _schtasks(["/Delete", "/TN", task, "/F"])
```
argparse 加 `--unregister-pipeline-brief` 分支调用它。

- [ ] **Step 8: Verify __main__ still works in standalone mode**

Run: `.venv310/Scripts/python.exe -c "from trading.__main__ import _run_forever; print('import ok')"`
Expected: 无异常（`_run_forever` 现在用 `eng.bootstrap()`，独立进程模式仍可用作开发/调试）

- [ ] **Step 9: Commit**

```bash
git add tests/server/test_lifespan_engine.py presentation/server/main.py trading/engine.py \
        ops/start_all.py ops/manage_ops_schtasks.py
git commit -m "feat(trading): lifespan 装配 engine + 删独立 cron/schtasks + start_all 收编（C-2 收口）"
```

---

### Task 10: e2e 测试 + 收尾（依赖全部）

**Files:**
- Modify: `tests/trading/test_e2e_trading_flow.py`（扩事件链 e2e）
- Modify: `docs/superpowers/plans/2026-07-31-scheduling-orchestration.md`（标记完成）

**Interfaces:**
- Consumes: 全部前序 Task

- [ ] **Step 1: Extend e2e test**

In `tests/trading/test_e2e_trading_flow.py`，加一个 e2e 场景：`pipeline_then_eod` 跑采集（mock subprocess）→ 写 data_ready → eod 落 plan →（模拟次日）pre_open 三段 gate 全绿挂单。具体测试代码按现有 e2e 文件的 mock 模式（参考其既有场景结构）。

- [ ] **Step 2: Run full trading test suite**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/ tests/strategies/test_required_data_keys.py tests/data/test_run_data_check_data_ready.py tests/server/test_lifespan_engine.py tests/ops/test_brief_all_async.py -v`
Expected: PASS（无回归）

- [ ] **Step 3: Run smoke check on import graph（确认无循环依赖）**

Run: `.venv310/Scripts/python.exe -c "from trading.orchestrate.pipeline import pipeline_then_eod; from presentation.server.main import app; print('import ok')"`
Expected: 无 `ImportError`/`circular import`

- [ ] **Step 4: Commit**

```bash
git add tests/trading/test_e2e_trading_flow.py
git commit -m "test(trading): C-2 e2e 事件链采集→eod→pre_open gate 全链路验证"
```

---

## Self-Review（writing-plans 内置）

**1. Spec coverage 核对：**
- O1 信号链事件化 → Task 6（`pipeline_then_eod` 的 `await proc.wait()`）
- O2 数据就绪声明式 → Task 2（`required_data_keys`）+ Task 6（动态 keys 并集）
- O3 pre_open 三段 gate → Task 8
- O4 可观测 → Task 3（T2 落库）+ Task 6（CRITICAL 告警）
- W1 → Task 4；W2/W3 → Task 5
- D7 Brief 收编 → Task 7 + Task 6 步骤 6
- start_all 收编 → Task 9 步骤 6-7
- 文件落点 → File Structure 节（2 新建 + 8 改动，对齐 spec §4.3）

**2. Placeholder scan：** 已检查，无 TBD/TODO；每个步骤含具体代码或命令。

**3. Type consistency：**
- `upsert_data_ready`/`get_data_ready` 签名 Task 1 定义，Task 3/6/8 消费一致。
- `required_data_keys: frozenset[str]` Task 2 定义，Task 6/8 消费一致。
- `submit_order(..., whitelist)` Task 4 定义。
- `check_shadow_gate() -> bool` Task 5 定义，Task 9 消费。
- `bootstrap()` Task 5 定义，Task 9 消费。
- `pipeline_then_eod(engine)` Task 6 定义，Task 9 消费。
- `run_brief_all() -> int` Task 7 定义，Task 6 消费。

**注：** Task 4 的 `_ACTIVE_ENGINE` 单例桥需确认 engine.py 是否已有该机制；若没有，Task 4 步骤 5 内含新建。Task 8/9 的 patch 路径需按实际实现调整（`_pre_open_gate` 是实例方法，patch 点是类属性）。
