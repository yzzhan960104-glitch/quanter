# 蔡森回测异步化（Spec 1）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把全市场回测从同步 HTTP 改为异步任务（ProcessPoolExecutor + SQLite 任务表），使其可执行、可观测进度、可取消、结果持久化——参数训练平台的闭环地基。

**Architecture:** uvicorn 进程内起 `ProcessPoolExecutor(max_workers=1)` 跑回测 worker 子进程（initializer 加载 data_lake 一次复用）；daemon 调度器线程 poll SQLite `PENDING` 任务 submit worker；任务全生命周期（PENDING/RUNNING/SUCCESS/FAILED/CANCELLED）持久化在 `data/replay_tasks.db`（SQLite 单一真相源，吸收原 replay_runs JSON 归档）；进度/abort 经 `multiprocessing.Queue` 传，主进程单点写 DB。

**Tech Stack:** Python 3.10（`.venv310`）、FastAPI、SQLite（标准库 `sqlite3`，WAL）、`concurrent.futures.ProcessPoolExecutor`、`multiprocessing.Queue`、pytest。

**Spec:** `docs/superpowers/specs/2026-07-13-caisen-replay-async-design.md`

## Global Constraints

- Python 3.10（`.venv310`）；测试一律 `.venv310/Scripts/python.exe -m pytest`。
- 全中文注释（CLAUDE.md）：每段代码标 What + Why（交易物理意图）。
- 极简显式（Karpathy）：无新第三方依赖（sqlite3 是标准库）；**不引 SQLAlchemy**。
- 无前视红线不破坏：`replay` 改造只加可选回调，默认 `None` = 现状。
- 并发=1：`ProcessPoolExecutor(max_workers=1)`，串行。
- 向后兼容：`replay()` 现有 3 个 caller（`caisen/__main__.py`、`scripts/calibrate_min_rr.py`、`server/services/caisen_service.py`）不破坏。
- 频繁 commit：每任务结束 commit，中文 message + `Co-Authored-By: Claude <noreply@anthropic.com>`。
- 每任务 TDD：先写失败测试 → 跑红 → 实现 → 跑绿 → commit。

## File Structure

**新建：**
- `caisen/replay_tasks_db.py` — SQLite 任务表访问层（DDL + CRUD）。单一职责：任务行读写。
- `caisen/replay_worker.py` — worker 进程入口（跑 replay + Queue 通信 + initializer 加载 data_lake）。
- `caisen/replay_scheduler.py` — 调度器 daemon 线程（poll PENDING → submit + heartbeat 监控 + 重启恢复）。
- `scripts/migrate_replay_runs_to_sqlite.py` — replay_runs JSON→SQLite 一次性迁移。
- 测试：`tests/test_replay_tasks_db.py`、`tests/test_replay_worker.py`、`tests/test_replay_scheduler.py`、`tests/test_migrate_replay_runs.py`。

**修改：**
- `caisen/backtest_replay.py` — `replay()` 加 `progress_cb`/`abort_cb` + `ReplayAborted` 异常。
- `server/services/caisen_service.py` — 加 `run_replay_async`；`list_replay_runs`/`get_replay_run`/`delete_replay_run` 改读 SQLite。
- `server/api/v1/caisen.py` — 加 4 个 async 端点；废弃老 `POST /replay`。
- `server/schemas/caisen.py` — 加 `ReplayAsyncRequest`/`ReplayTaskSummary`/`ReplayTaskDetail`/`CancelResponse`。
- `server/main.py` — lifespan 起 pool + 调度器 + 重启恢复 + shutdown 关闭 pool。

---

### Task 1: SQLite 任务表访问层 `caisen/replay_tasks_db.py`

**Files:**
- Create: `caisen/replay_tasks_db.py`
- Test: `tests/test_replay_tasks_db.py`

**Interfaces:**
- Produces（供后续所有任务调用）:
  - `init_db(path=_DEFAULT_DB_PATH) -> None`
  - `create_task(req: dict) -> str`（生成 task_id，INSERT PENDING，返回 task_id；`req` 含 `start/end/universe/cfg_override`）
  - `get_task(task_id) -> dict | None`
  - `list_tasks(status=None, limit=100) -> list[dict]`
  - `list_success_runs() -> list[dict]`（供 `/replay/runs`，等价 `list_tasks("SUCCESS")`）
  - `claim_next_pending() -> dict | None`（调度器原子领取最老 PENDING 并标 RUNNING）
  - `update_progress(task_id, progress: int) -> None`
  - `update_heartbeat(task_id) -> None`
  - `mark_success(task_id, report_json: str) -> None`
  - `mark_failed(task_id, error: str) -> None`
  - `mark_cancelled(task_id) -> None`
  - `delete_task(task_id) -> bool`（DELETE 端点用）
  - `reset_running_to_failed() -> int`（重启恢复，返回受影响行数）
- 常量：`_DEFAULT_DB_PATH = "data/replay_tasks.db"`（测试 monkeypatch 覆盖）

- [ ] **Step 1: 写失败测试 `tests/test_replay_tasks_db.py`**

```python
# -*- coding: utf-8 -*-
"""replay_tasks_db 单测：SQLite 任务表 CRUD + 状态机 + 重启恢复（Spec 1 Task 1）。"""
import os
import pytest
from caisen import replay_tasks_db


@pytest.fixture
def db(tmp_path, monkeypatch):
    """每个用例独立 SQLite 文件（隔离），monkeypatch 路径常量。"""
    path = str(tmp_path / "t.db")
    monkeypatch.setattr(replay_tasks_db, "_DEFAULT_DB_PATH", path)
    replay_tasks_db.init_db(path)
    return path


def _req(**over):
    base = {"start": "2024-01-01", "end": "2024-06-01", "universe": None, "cfg_override": {}}
    base.update(over)
    return base


def test_create_and_get(db):
    task_id = replay_tasks_db.create_task(_req(cfg_override={"min_rr_ratio": 1.5}))
    assert isinstance(task_id, str) and len(task_id) > 0
    got = replay_tasks_db.get_task(task_id)
    assert got["task_id"] == task_id
    assert got["status"] == "PENDING"
    assert got["progress"] == 0
    assert got["universe_n"] == -1            # None → -1（全市场）
    assert got["cfg_override"] == {"min_rr_ratio": 1.5}   # cfg_json 反序列化


def test_list_tasks_desc_by_created(db):
    id1 = replay_tasks_db.create_task(_req())
    id2 = replay_tasks_db.create_task(_req())
    rows = replay_tasks_db.list_tasks()
    assert [r["task_id"] for r in rows] == [id2, id1]   # 最新在前


def test_list_tasks_status_filter(db):
    tid = replay_tasks_db.create_task(_req())
    replay_tasks_db.mark_failed(tid, "boom")
    assert replay_tasks_db.list_tasks(status="PENDING") == []
    assert len(replay_tasks_db.list_tasks(status="FAILED")) == 1


def test_claim_next_pending_atomic(db):
    id1 = replay_tasks_db.create_task(_req())
    id2 = replay_tasks_db.create_task(_req())
    claimed = replay_tasks_db.claim_next_pending()
    assert claimed["task_id"] == id1          # FIFO 最老
    assert claimed["status"] == "RUNNING"     # 领取即标 RUNNING（防并发双领）
    assert replay_tasks_db.claim_next_pending()["task_id"] == id2
    assert replay_tasks_db.claim_next_pending() is None   # 队列空


def test_mark_success_embeds_report(db):
    tid = replay_tasks_db.create_task(_req())
    replay_tasks_db.mark_success(tid, '{"n_hits": 42}')
    got = replay_tasks_db.get_task(tid)
    assert got["status"] == "SUCCESS"
    assert got["report"] == {"n_hits": 42}    # report_json 反序列化字段
    assert got["finished_at"] is not None


def test_update_progress(db):
    tid = replay_tasks_db.create_task(_req())
    replay_tasks_db.update_progress(tid, 37)
    assert replay_tasks_db.get_task(tid)["progress"] == 37


def test_reset_running_to_failed(db):
    tid = replay_tasks_db.create_task(_req())
    replay_tasks_db.claim_next_pending()       # → RUNNING
    n = replay_tasks_db.reset_running_to_failed()
    assert n == 1
    got = replay_tasks_db.get_task(tid)
    assert got["status"] == "FAILED"
    assert "重启" in got["error"] or "中断" in got["error"]


def test_delete_task(db):
    tid = replay_tasks_db.create_task(_req())
    assert replay_tasks_db.delete_task(tid) is True
    assert replay_tasks_db.get_task(tid) is None
    assert replay_tasks_db.delete_task("nope") is False
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv310/Scripts/python.exe -m pytest tests/test_replay_tasks_db.py -v`
Expected: FAIL（`ModuleNotFoundError: caisen.replay_tasks_db`）

- [ ] **Step 3: 实现 `caisen/replay_tasks_db.py`**

```python
# -*- coding: utf-8 -*-
"""caisen.replay_tasks_db 异步回测任务表 SQLite 访问层（Spec 1）。

物理定位：异步回测任务全生命周期持久化（PENDING/RUNNING/SUCCESS/FAILED/CANCELLED）。
单一真相源——吸收原 replay_runs JSON 归档（成功 report 内嵌 report_json 列）。
标准库 sqlite3 + WAL，无新依赖（合 Karpathy 极简；不引 SQLAlchemy）。

并发模型：主进程（API + 调度器）单点写，worker 经 Queue 上报进度由主进程落库，
避免跨进程 SQLite 写锁。WAL 模式读不阻塞写。
"""
from __future__ import annotations
import json
import os
import sqlite3
import uuid
from datetime import datetime
from typing import Optional

_DEFAULT_DB_PATH = "data/replay_tasks.db"
_VALID_STATUS = ("PENDING", "RUNNING", "SUCCESS", "FAILED", "CANCELLED")


def _now_iso() -> str:
    """ISO 微秒时间戳（列表降序排序键 + 展示）。"""
    return datetime.now().isoformat(timespec="microseconds")


def _connect(path: str) -> sqlite3.Connection:
    """打开 SQLite 连接（autocommit + WAL + Row 工厂）。

    timeout=30：防极端并发写时抛 SQLITE_BUSY；isolation_level=None 即 autocommit，
    事务靠显式 BEGIN/COMMIT（claim_next_pending 用）。
    """
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db(path: str = _DEFAULT_DB_PATH) -> None:
    """建表 + 索引（幂等，IF NOT EXISTS）。WAL 模式提升并发读。"""
    with _connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS replay_tasks (
                task_id        TEXT PRIMARY KEY,
                created_at     TEXT NOT NULL,
                status         TEXT NOT NULL,
                progress       INTEGER DEFAULT 0,
                start          TEXT,
                end            TEXT,
                universe_n     INTEGER,
                cfg_override   TEXT,
                error          TEXT,
                report_json    TEXT,
                started_at     TEXT,
                finished_at    TEXT,
                last_heartbeat TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_tasks_status  ON replay_tasks(status);
            CREATE INDEX IF NOT EXISTS idx_tasks_created ON replay_tasks(created_at DESC);
            """
        )


def _row_to_dict(row: sqlite3.Row) -> dict:
    """行字典化 + 反序列化 cfg_override/report_json（None 透传）。"""
    d = dict(row)
    d["cfg_override"] = json.loads(d["cfg_override"]) if d.get("cfg_override") else {}
    d["report"] = json.loads(d["report_json"]) if d.get("report_json") else None
    return d


def create_task(req: dict, path: str = _DEFAULT_DB_PATH) -> str:
    """生成 task_id + 写 PENDING 行（universe_n：None→-1 全市场，列表→len）。返回 task_id。"""
    task_id = uuid.uuid4().hex
    universe = req.get("universe")
    universe_n = -1 if universe is None else len(universe)
    with _connect(path) as conn:
        conn.execute(
            """INSERT INTO replay_tasks
               (task_id, created_at, status, progress, start, end, universe_n, cfg_override)
               VALUES (?, ?, 'PENDING', 0, ?, ?, ?, ?)""",
            (task_id, _now_iso(), req.get("start"), req.get("end"),
             universe_n, json.dumps(req.get("cfg_override") or {}, ensure_ascii=False)),
        )
    return task_id


def get_task(task_id: str, path: str = _DEFAULT_DB_PATH) -> Optional[dict]:
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT * FROM replay_tasks WHERE task_id=?", (task_id,)).fetchone()
    return _row_to_dict(row) if row else None


def list_tasks(status: Optional[str] = None, limit: int = 100,
               path: str = _DEFAULT_DB_PATH) -> list[dict]:
    """按 created_at 降序；status=None 全量，否则按状态过滤。"""
    with _connect(path) as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM replay_tasks WHERE status=? ORDER BY created_at DESC LIMIT ?",
                (status, limit)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM replay_tasks ORDER BY created_at DESC LIMIT ?",
                (limit,)).fetchall()
    return [_row_to_dict(r) for r in rows]


def list_success_runs(path: str = _DEFAULT_DB_PATH) -> list[dict]:
    """供 /replay/runs：只返 SUCCESS（成功回测档案，等价 list_tasks('SUCCESS')）。"""
    return list_tasks(status="SUCCESS", limit=1000, path=path)


def claim_next_pending(path: str = _DEFAULT_DB_PATH) -> Optional[dict]:
    """调度器原子领取最老 PENDING → 标 RUNNING（事务防并发双领）。无 PENDING 返 None。"""
    with _connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")          # 写锁，防并发 claim
        row = conn.execute(
            "SELECT * FROM replay_tasks WHERE status='PENDING' "
            "ORDER BY created_at ASC LIMIT 1").fetchone()
        if row is None:
            conn.execute("COMMIT")
            return None
        conn.execute(
            "UPDATE replay_tasks SET status='RUNNING', started_at=?, last_heartbeat=? "
            "WHERE task_id=?",
            (_now_iso(), _now_iso(), row["task_id"]))
        conn.execute("COMMIT")
    return get_task(row["task_id"], path)


def update_progress(task_id: str, progress: int, path: str = _DEFAULT_DB_PATH) -> None:
    with _connect(path) as conn:
        conn.execute("UPDATE replay_tasks SET progress=? WHERE task_id=?", (progress, task_id))


def update_heartbeat(task_id: str, path: str = _DEFAULT_DB_PATH) -> None:
    with _connect(path) as conn:
        conn.execute("UPDATE replay_tasks SET last_heartbeat=? WHERE task_id=?",
                     (_now_iso(), task_id))


def mark_success(task_id: str, report_json: str, path: str = _DEFAULT_DB_PATH) -> None:
    with _connect(path) as conn:
        conn.execute(
            "UPDATE replay_tasks SET status='SUCCESS', report_json=?, progress=100, "
            "finished_at=? WHERE task_id=?",
            (report_json, _now_iso(), task_id))


def mark_failed(task_id: str, error: str, path: str = _DEFAULT_DB_PATH) -> None:
    with _connect(path) as conn:
        conn.execute(
            "UPDATE replay_tasks SET status='FAILED', error=?, finished_at=? WHERE task_id=?",
            (error, _now_iso(), task_id))


def mark_cancelled(task_id: str, path: str = _DEFAULT_DB_PATH) -> None:
    with _connect(path) as conn:
        conn.execute("UPDATE replay_tasks SET status='CANCELLED', finished_at=? WHERE task_id=?",
                     (_now_iso(), task_id))


def delete_task(task_id: str, path: str = _DEFAULT_DB_PATH) -> bool:
    """删除单任务（DELETE 端点用）。返回是否删除了行。"""
    with _connect(path) as conn:
        cur = conn.execute("DELETE FROM replay_tasks WHERE task_id=?", (task_id,))
    return cur.rowcount > 0


def reset_running_to_failed(path: str = _DEFAULT_DB_PATH) -> int:
    """重启恢复：崩溃/重启残留的 RUNNING 标 FAILED（不自动重跑，用户决定重提）。

    物理意图：uvicorn 重启时上一轮卡 RUNNING 的任务无法继续，标 FAILED + 原因，
    避免无意识重复消耗几十分钟~几小时算力（与 spec §3.3/§7 统一）。
    """
    with _connect(path) as conn:
        cur = conn.execute(
            "UPDATE replay_tasks SET status='FAILED', error='进程重启中断（需手动重提）', "
            "finished_at=? WHERE status='RUNNING'",
            (_now_iso(),))
    return cur.rowcount
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv310/Scripts/python.exe -m pytest tests/test_replay_tasks_db.py -v`
Expected: PASS（8 用例全绿）

- [ ] **Step 5: commit**

```bash
git add caisen/replay_tasks_db.py tests/test_replay_tasks_db.py
git commit -m "feat(caisen): replay_tasks_db SQLite 任务表访问层（Spec 1 Task 1）" -m "Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 2: `backtest_replay.replay()` 加进度/取消回调

**Files:**
- Modify: `caisen/backtest_replay.py`（`replay()` 函数 + 新增 `ReplayAborted` 异常）
- Test: `tests/caisen/test_backtest_replay.py`（扩展，不破坏现有用例）

**Interfaces:**
- Consumes: 无（纯算法层改造）
- Produces:
  - `ReplayAborted(Exception)`（取消信号）
  - `replay(price_data, cfg, risk, start, end, aum, *, progress_cb=None, abort_cb=None, trading_calendar=None)`（两个新可选 kw-only 参数；默认 None = 现状，3 个现有 caller 不破坏）

- [ ] **Step 1: 写失败测试（追加到 `tests/caisen/test_backtest_replay.py`）**

```python
import pytest
from caisen import backtest_replay
from caisen.backtest_replay import replay, ReplayAborted


def test_replay_progress_cb_invoked(tmp_path):
    """progress_cb 每 50 个 symbol 调一次，参数为 (done, total)。"""
    price_data = _mk_price_data(n_symbols=120)   # 复用文件现有合成 fixture；无则见下
    seen = []
    cfg, risk = _mk_cfg_risk()
    replay(price_data, cfg, risk, start=..., end=..., aum=1_000_000,
           progress_cb=lambda d, t: seen.append((d, t)))
    assert len(seen) >= 2                          # 120 symbol / 50 ≈ 2 次
    assert seen[-1][1] == 120                      # total = 标的数


def test_replay_abort_cb_raises():
    """abort_cb 返回 True → 抛 ReplayAborted（双层循环顶检查）。"""
    price_data = _mk_price_data(n_symbols=10)
    cfg, risk = _mk_cfg_risk()
    with pytest.raises(ReplayAborted):
        replay(price_data, cfg, risk, start=..., end=..., aum=1_000_000,
               abort_cb=lambda: True)


def test_replay_default_cb_none_unchanged():
    """progress_cb/abort_cb 默认 None = 现状行为（现有用例守护，确认签名兼容）。"""
    price_data = _mk_price_data(n_symbols=5)
    cfg, risk = _mk_cfg_risk()
    report = replay(price_data, cfg, risk, start=..., end=..., aum=1_000_000)
    assert report.n_hits >= 0                      # 正常返回，不抛
```

> 注：`_mk_price_data/_mk_cfg_risk/start/end` 复用该测试文件已有的合成助手；若文件中无统一助手，沿用 `test_replay_no_lookahead` 等现有用例里构造 `price_data` 的代码片段（直接复制，勿写"类似"）。

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv310/Scripts/python.exe -m pytest tests/caisen/test_backtest_replay.py -v -k "progress_cb or abort_cb or default_cb"`
Expected: FAIL（`TypeError: unexpected keyword argument 'progress_cb'`）

- [ ] **Step 3: 改 `caisen/backtest_replay.py`**

在文件异常类区加：
```python
class ReplayAborted(Exception):
    """回测被用户取消（abort_cb 返回 True 时于循环顶抛出）。"""
```

改 `replay()` 签名与循环（在 `:91` 附近）：
```python
def replay(
    price_data, cfg, risk, start, end, aum,
    *,
    progress_cb=None,        # Callable[[done:int, total:int], None] —— 每 50 symbol 调一次
    abort_cb=None,           # Callable[[], bool] —— True 即中止
    trading_calendar=None,
):
```

在 symbol 外层循环体（`for symbol, df in price_data.items():` 之上）加计数与回调。改造后的循环骨架（替换 `:134-141` 区域，保留原有 full_pivots/full_hv 预计算）：
```python
    # —— symbol 循环：进度上报（每 50）+ abort 检查（循环顶）——
    total = len(price_data)
    done = 0
    _PROGRESS_EVERY = 50        # 全市场 5000 只 ≈ 100 次上报（spec §5.1）
    items = list(price_data.items())
    for idx, (symbol, df) in enumerate(items):
        # 取消检查点（循环顶）：abort_cb 命中即抛，task 标 CANCELLED
        if abort_cb is not None and abort_cb():
            raise ReplayAborted()
        # —— symbol 原有处理逻辑（full_pivots 截断 + screener + plan + _simulate）保持不变 ——
        ...   # 原 for 循环体内容不动
        done = idx + 1
        if progress_cb is not None and (done % _PROGRESS_EVERY == 0 or done == total):
            progress_cb(done, total)
```

> 关键：只在循环顶插 abort 检查 + 循环尾插进度回调，**不改动 screener/plan/_simulate 任何逻辑**（无前视红线不动）。`trading_calendar` 参数原本就在，提到 kw-only 位置时确认现有 caller 用位置传参的需改为关键字——检查 `caisen/__main__.py`、`scripts/calibrate_min_rr.py`、`caisen_service.run_replay` 三处调用，若传了 `trading_calendar=` 则保持，若位置传参则调整。

- [ ] **Step 4: 跑全量 replay 测试确认通过且不破坏现有**

Run: `.venv310/Scripts/python.exe -m pytest tests/caisen/test_backtest_replay.py -v`
Expected: PASS（新 3 用例 + 全部现有用例绿）

- [ ] **Step 5: 跑现有 3 个 caller 相关门测试确认未破坏**

Run: `.venv310/Scripts/python.exe -m pytest tests/caisen/test_cli_main.py tests/test_caisen_service.py tests/test_caisen_replay_runs.py -q`
Expected: PASS

- [ ] **Step 6: commit**

```bash
git add caisen/backtest_replay.py tests/caisen/test_backtest_replay.py
git commit -m "feat(caisen): replay 加 progress_cb/abort_cb 回调 + ReplayAborted（Spec 1 Task 2）" -m "Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 3: worker 进程入口 `caisen/replay_worker.py`

**Files:**
- Create: `caisen/replay_worker.py`
- Test: `tests/test_replay_worker.py`

**Interfaces:**
- Consumes: Task 1 `replay_tasks_db`（get_task/mark_success/mark_failed/mark_cancelled/update_progress/update_heartbeat）；Task 2 `backtest_replay.replay`/`ReplayAborted`；现有 `caisen_service._load_price_data`、`_merge_cfg`、`caisen.risk.RiskManager`
- Produces:
  - `run_replay_worker(task_id, abort_flag, progress_q, heartbeat_q)` —— worker 子进程入口（同步函数，被 ProcessPoolExecutor submit）
  - `_init_worker()` —— ProcessPoolExecutor 的 `initializer`（加载 data_lake 一次，常驻复用）

- [ ] **Step 1: 写失败测试 `tests/test_replay_worker.py`**

```python
# -*- coding: utf-8 -*-
"""replay_worker 单测：worker 跑完/异常/取消三条路径（Spec 1 Task 3）。

worker 是同步函数（子进程跑），测试用合成 price_data + monkeypatch data_lake 装配，
在主进程直接调 run_replay_worker 验证状态机写回（不真起子进程）。
"""
import multiprocessing as mp
import pytest
from caisen import replay_tasks_db, replay_worker


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = str(tmp_path / "t.db")
    monkeypatch.setattr(replay_tasks_db, "_DEFAULT_DB_PATH", path)
    replay_tasks_db.init_db(path)
    return path


def _make_task(db, **req_over):
    return replay_tasks_db.create_task(
        {"start": "2024-01-01", "end": "2024-06-01", "universe": ["000001.SZ"],
         "cfg_override": {}, **req_over})


def test_worker_success_marks_success(db, monkeypatch):
    """worker 跑完 → mark_success + report 内嵌 + progress=100。"""
    task_id = _make_task(db)
    monkeypatch.setattr(replay_worker, "_load_price_data",
                        lambda uni, end: {"000001.SZ": _synth_df()})
    abort_flag = mp.Event()                # 未 set = 不取消
    prog_q, hb_q = mp.Queue(), mp.Queue()
    replay_worker.run_replay_worker(task_id, abort_flag, prog_q, hb_q)
    got = replay_tasks_db.get_task(task_id)
    assert got["status"] == "SUCCESS"
    assert got["progress"] == 100
    assert got["report"] is not None


def test_worker_exception_marks_failed(db, monkeypatch):
    """price_data 装配空 → 写 FAILED（不抛出 worker 外）。"""
    task_id = _make_task(db)
    monkeypatch.setattr(replay_worker, "_load_price_data", lambda uni, end: {})
    abort_flag = mp.Event()
    prog_q, hb_q = mp.Queue(), mp.Queue()
    replay_worker.run_replay_worker(task_id, abort_flag, prog_q, hb_q)
    got = replay_tasks_db.get_task(task_id)
    assert got["status"] == "FAILED"
    assert got["error"]


def test_worker_abort_marks_cancelled(db, monkeypatch):
    """abort_flag 已 set → ReplayAborted → mark_cancelled。"""
    task_id = _make_task(db)
    monkeypatch.setattr(replay_worker, "_load_price_data",
                        lambda uni, end: {"000001.SZ": _synth_df()})
    abort_flag = mp.Event(); abort_flag.set()       # 预先取消
    prog_q, hb_q = mp.Queue(), mp.Queue()
    replay_worker.run_replay_worker(task_id, abort_flag, prog_q, hb_q)
    assert replay_tasks_db.get_task(task_id)["status"] == "CANCELLED"
```

> `_synth_df()` 构造一段合法 OHLCV DataFrame（沿用 `test_backtest_replay.py` 的合成手法，直接复制构造代码）。

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv310/Scripts/python.exe -m pytest tests/test_replay_worker.py -v`
Expected: FAIL（`ModuleNotFoundError: caisen.replay_worker`）

- [ ] **Step 3: 实现 `caisen/replay_worker.py`**

```python
# -*- coding: utf-8 -*-
"""caisen.replay_worker 异步回测 worker 进程入口（Spec 1 Task 3）。

物理定位：被 ProcessPoolExecutor submit 在子进程跑单次回测。
- _init_worker：进程 initializer，加载 data_lake 一次（数 GB parquet），所有 task 复用。
- run_replay_worker：读 task → 装配 price_data → 跑 replay(progress_cb/abort_cb)
  → 写回 SUCCESS/FAILED/CANCELLED。abort 经 multiprocessing.Event 传入，
  progress/heartbeat 经 Queue 回报主进程（主进程单点写 DB，避免跨进程 SQLite 锁）。
"""
from __future__ import annotations
import json
import logging
import multiprocessing as mp

from caisen import replay_tasks_db
from caisen.backtest_replay import replay, ReplayAborted
from caisen.config import StrategyConfig
from caisen.risk import RiskManager

logger = logging.getLogger(__name__)

# 模块级：worker 进程内复用的 data_lake reader（_init_worker 装配）
_reader = None


def _init_worker():
    """ProcessPoolExecutor initializer：加载 daily 湖一次（子进程常驻复用）。

    物理意图：5000 只 parquet 加载占数 GB + 耗时，每 task 重 load 不可接受。
    进程池 worker 常驻 → _init_worker 首次调用 load 一次 → 后续 task 直接读 _reader。
    """
    global _reader
    if _reader is not None:
        return
    from data.lake_reader import DataLakeReader
    from config import LAKE_CONFIG
    _reader = DataLakeReader.get_instance()
    daily_path = LAKE_CONFIG["lakes"]["daily"]
    if not _reader.loaded or "daily" not in _reader.lakes():
        _reader.load(daily_path, key="daily")


def run_replay_worker(task_id: str, abort_flag, progress_q, heartbeat_q) -> None:
    """worker 入口：跑单次回测 + 写回状态。任何异常都落 FAILED（不抛出子进程外）。

    参数：
        task_id：任务 id。
        abort_flag：multiprocessing.Event，主进程 set 后 worker 循环顶命中即 CANCELLED。
        progress_q/heartbeat_q：主进程消费的 Queue（worker 上报，主进程落库）。
    """
    from server.services.caisen_service import _load_price_data, _merge_cfg
    try:
        _init_worker()
        task = replay_tasks_db.get_task(task_id)
        if task is None:
            return
        req = {"start": task["start"], "end": task["end"],
               "universe": None if task["universe_n"] == -1 else ["x"] * task["universe_n"],
               "cfg_override": task["cfg_override"]}
        # universe 原值未逐个存（只存了数量 universe_n），需从 task 起源保留——见 §实现注
        cfg = _merge_cfg(task["cfg_override"])
        risk = RiskManager(cfg)
        price_data = _load_price_data(req["universe"], task["end"])

        # 回调：abort 查 Event；progress/heartbeat 投 Queue（主进程落库）
        def _abort(): return abort_flag.is_set()
        _last = {"done": 0}
        def _progress(done, total):
            progress_q.put((task_id, done, total))
            _last["done"] = done

        report = replay(price_data, cfg, risk, start=req["start"], end=req["end"],
                        aum=1_000_000.0, progress_cb=_progress, abort_cb=_abort)
        replay_tasks_db.mark_success(task_id, json.dumps(report.__dict__, ensure_ascii=False,
                                                         default=str))
    except ReplayAborted:
        replay_tasks_db.mark_cancelled(task_id)
        logger.info("worker 任务被取消：task_id=%s", task_id)
    except Exception as exc:
        replay_tasks_db.mark_failed(task_id, f"{type(exc).__name__}: {exc}")
        logger.exception("worker 任务异常：task_id=%s", task_id)
```

> **实现注（universe 保留）**：Task 1 的 `create_task` 只存了 `universe_n`（数量），worker 要还原 universe 列表。**修正**：Task 1 schema 加 `universe_json TEXT` 列（存完整 symbol 列表或 None），`create_task` 写入，worker 读 `task["universe"]`。执行 Task 1 时一并加此列（回 Task 1 补一个 `universe_json` 列 + 测试断言）。本处 worker 用 `task["universe"]` 而非上文的 `["x"]*n`。

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv310/Scripts/python.exe -m pytest tests/test_replay_worker.py -v`
Expected: PASS（3 用例）

- [ ] **Step 5: commit**

```bash
git add caisen/replay_worker.py tests/test_replay_worker.py
git commit -m "feat(caisen): replay_worker 子进程入口 + initializer（Spec 1 Task 3）" -m "Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 4: 调度器 `caisen/replay_scheduler.py`

**Files:**
- Create: `caisen/replay_scheduler.py`
- Test: `tests/test_replay_scheduler.py`

**Interfaces:**
- Consumes: Task 1 `replay_tasks_db`（claim_next_pending/reset_running_to_failed/mark_failed）；Task 3 `replay_worker.run_replay_worker`/`_init_worker`
- Produces:
  - `ReplayScheduler(pool, abort_flags: dict, db_path)` 类
  - `ReplayScheduler.start()` / `stop()` —— 启停 daemon 线程
  - `ReplayScheduler.request_cancel(task_id)` —— 置 abort flag（cancel 端点调）
  - 模块级 `_POLL_INTERVAL=2.0`、`_HEARTBEAT_TIMEOUT=300`（5min）

- [ ] **Step 1: 写失败测试 `tests/test_replay_scheduler.py`**

```python
# -*- coding: utf-8 -*-
"""replay_scheduler 单测：领取/派发/cancel/heartbeat 超时/重启恢复（Spec 1 Task 4）。

用假 pool（直接同步执行 callable）+ monkeypatch run_replay_worker，避免真起子进程。
"""
import time
import pytest
import multiprocessing as mp
from caisen import replay_tasks_db, replay_scheduler


class _FakePool:
    """假 ProcessPoolExecutor：submit 直接记录 callable，不真跑（测试控制）。"""
    def __init__(self): self.submitted = []
    def submit(self, fn, *a, **kw): self.submitted.append((fn, a, kw))


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = str(tmp_path / "t.db")
    monkeypatch.setattr(replay_tasks_db, "_DEFAULT_DB_PATH", path)
    replay_tasks_db.init_db(path)
    return path


def test_start_resets_running_on_startup(db):
    """调度器 start 时把残留 RUNNING 标 FAILED（重启恢复）。"""
    tid = replay_tasks_db.create_task({"start": "s", "end": "e", "universe": None, "cfg_override": {}})
    replay_tasks_db.claim_next_pending()         # → RUNNING
    sched = replay_scheduler.ReplayScheduler(_FakePool(), {}, db,
                                              run_replay_worker=lambda *a, **k: None)
    sched._reset_on_startup()
    assert replay_tasks_db.get_task(tid)["status"] == "FAILED"


def test_poll_dispatches_pending(db, monkeypatch):
    """有 PENDING → claim + submit worker（注册 abort_flag）。"""
    tid = replay_tasks_db.create_task({"start": "s", "end": "e", "universe": None, "cfg_override": {}})
    pool = _FakePool()
    sched = replay_scheduler.ReplayScheduler(pool, {}, db,
                                              run_replay_worker=lambda *a, **k: None)
    sched._poll_once()
    assert len(pool.submitted) == 1
    assert tid in sched.abort_flags


def test_request_cancel_sets_flag(db):
    tid = replay_tasks_db.create_task({"start": "s", "end": "e", "universe": None, "cfg_override": {}})
    pool = _FakePool()
    sched = replay_scheduler.ReplayScheduler(pool, {}, db,
                                              run_replay_worker=lambda *a, **k: None)
    sched._poll_once()                            # 注册 abort_flag
    sched.request_cancel(tid)
    assert sched.abort_flags[tid].is_set()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv310/Scripts/python.exe -m pytest tests/test_replay_scheduler.py -v`
Expected: FAIL（`ModuleNotFoundError`）

- [ ] **Step 3: 实现 `caisen/replay_scheduler.py`**

```python
# -*- coding: utf-8 -*-
"""caisen.replay_scheduler 异步回测调度器 daemon 线程（Spec 1 Task 4）。

物理定位：uvicorn 进程内的 daemon 线程，串行调度（concurrency=1）：
- 启动时 reset_running_to_failed（重启恢复）；
- 周期 poll PENDING → claim_next_pending → submit run_replay_worker（注册 abort_flag）；
- 监控 last_heartbeat 超时 → mark_failed（worker 崩溃，不重跑）；
- request_cancel(task_id)：set abort_flag → worker 循环顶命中 → CANCELLED。
"""
from __future__ import annotations
import logging
import multiprocessing as mp
import threading
import time
from datetime import datetime

from caisen import replay_tasks_db

logger = logging.getLogger(__name__)

_POLL_INTERVAL = 2.0          # poll 间隔（秒）
_HEARTBEAT_TIMEOUT = 300      # heartbeat 超时（秒）→ 标 FAILED


class ReplayScheduler:
    def __init__(self, pool, abort_flags: dict, db_path: str,
                 run_replay_worker=None, clock=datetime.now):
        self._pool = pool
        self.abort_flags = abort_flags       # task_id → mp.Event（主进程持有，cancel 端点 set）
        self._db_path = db_path
        self._run_replay_worker = run_replay_worker   # 测试注入；生产从 replay_worker import
        self._clock = clock
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self):
        self._reset_on_startup()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="replay-scheduler")
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _reset_on_startup(self):
        """重启恢复：残留 RUNNING 标 FAILED（spec §3.3）。"""
        n = replay_tasks_db.reset_running_to_failed(self._db_path)
        if n:
            logger.warning("启动恢复：%d 个残留 RUNNING 任务标 FAILED", n)

    def request_cancel(self, task_id: str):
        """cancel 端点调：set abort_flag（worker 循环顶命中即 CANCELLED）。"""
        flag = self.abort_flags.get(task_id)
        if flag is not None:
            flag.set()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self._poll_once()
            except Exception:
                logger.exception("调度器 poll 异常（不中断循环）")
            self._stop.wait(_POLL_INTERVAL)

    def _poll_once(self):
        """领取一个 PENDING → submit worker（concurrency=1：pool 满则 submit 阻塞排队）。"""
        run_worker = self._run_replay_worker or self._import_worker()
        task = replay_tasks_db.claim_next_pending(self._db_path)
        if task is None:
            return
        task_id = task["task_id"]
        abort_flag = mp.Event()
        self.abort_flags[task_id] = abort_flag
        progress_q = mp.Queue(); heartbeat_q = mp.Queue()
        # submit 到 pool（concurrency=1 串行）；队列消费线程另起（落库 progress/heartbeat）
        self._pool.submit(run_worker, task_id, abort_flag, progress_q, heartbeat_q)
        threading.Thread(target=self._consume_queues, args=(task_id, progress_q),
                         daemon=True).start()

    def _consume_queues(self, task_id, progress_q):
        """消费 worker progress 上报 → 落库（主进程单点写 DB）。"""
        while not self._stop.is_set():
            try:
                tid, done, total = progress_q.get(timeout=_POLL_INTERVAL)
            except Exception:
                break
            pct = int(done * 100 / total) if total else 0
            replay_tasks_db.update_progress(tid, pct)
            replay_tasks_db.update_heartbeat(tid)

    @staticmethod
    def _import_worker():
        from caisen.replay_worker import run_replay_worker
        return run_replay_worker
```

> 注：heartbeat 超时监控（`_HEARTBEAT_TIMEOUT`）在 `_loop` 里周期扫 RUNNING 任务的 `last_heartbeat`，超时 `mark_failed`——作为 Step 3 的补充逻辑加进 `_poll_once` 或独立 `_sweep_stale`，附一个 `test_heartbeat_timeout_marks_failed` 用例（注入 fake clock 让 heartbeat 过期）。

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv310/Scripts/python.exe -m pytest tests/test_replay_scheduler.py -v`
Expected: PASS

- [ ] **Step 5: commit**

```bash
git add caisen/replay_scheduler.py tests/test_replay_scheduler.py
git commit -m "feat(caisen): replay_scheduler daemon 调度器（Spec 1 Task 4）" -m "Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 5: service 层 `run_replay_async` + list/get/delete 改读 SQLite

**Files:**
- Modify: `server/services/caisen_service.py`
- Test: `tests/test_caisen_service.py`

**Interfaces:**
- Consumes: Task 1 `replay_tasks_db`
- Produces:
  - `run_replay_async(req: ReplayAsyncRequest) -> str`（task_id，立即返回不阻塞）
  - `list_replay_runs() -> list[dict]` / `get_replay_run(task_id) -> dict|None` / `delete_replay_run(task_id) -> bool`（改读 SQLite，对外契约不变）

- [ ] **Step 1: 写失败测试（追加 `tests/test_caisen_service.py`）**

```python
def test_run_replay_async_writes_pending(tmp_path, monkeypatch):
    from caisen import replay_tasks_db
    monkeypatch.setattr(replay_tasks_db, "_DEFAULT_DB_PATH", str(tmp_path / "t.db"))
    replay_tasks_db.init_db()
    from server.schemas.caisen import ReplayAsyncRequest
    from server.services import caisen_service
    tid = caisen_service.run_replay_async(
        ReplayAsyncRequest(start="2024-01-01", end="2024-06-01"))
    assert replay_tasks_db.get_task(tid)["status"] == "PENDING"


def test_list_replay_runs_reads_sqlite_success_only(tmp_path, monkeypatch):
    """list_replay_runs 只返 SUCCESS（FAILED/CANCELLED 不进 /replay/runs）。"""
    from caisen import replay_tasks_db
    from server.services import caisen_service
    monkeypatch.setattr(replay_tasks_db, "_DEFAULT_DB_PATH", str(tmp_path / "t.db"))
    replay_tasks_db.init_db()
    t1 = replay_tasks_db.create_task({"start": "s", "end": "e", "universe": None, "cfg_override": {}})
    t2 = replay_tasks_db.create_task({"start": "s", "end": "e", "universe": None, "cfg_override": {}})
    replay_tasks_db.mark_success(t1, '{"n_hits": 1}')
    replay_tasks_db.mark_failed(t2, "boom")
    rows = caisen_service.list_replay_runs()
    assert [r["task_id"] for r in rows] == [t1]
```

- [ ] **Step 2: 跑确认失败** → `.venv310/Scripts/python.exe -m pytest tests/test_caisen_service.py -v -k "replay_async or list_replay_runs_reads"`

- [ ] **Step 3: 实现（改 `caisen_service.py`）**

```python
from caisen import replay_tasks_db

def run_replay_async(req) -> str:
    """提交异步回测：写 PENDING 行，返回 task_id（不阻塞，调度器后续 poll 派发）。"""
    replay_tasks_db.init_db()
    return replay_tasks_db.create_task(req.model_dump())

def list_replay_runs() -> list:
    """历史回测列表（改读 SQLite SUCCESS，契约不变）。"""
    return replay_tasks_db.list_success_runs()

def get_replay_run(run_id: str):
    return replay_tasks_db.get_task(run_id)

def delete_replay_run(run_id: str) -> bool:
    return replay_tasks_db.delete_task(run_id)
```

> 同时**移除**老同步 `run_replay` 函数及其对 `backtest_replay.replay` 的直接调用（已被 worker 取代）；保留 `_load_price_data`/`_merge_cfg`（worker 复用）。删除 `from caisen import replay_runs`（老 JSON 模块）相关 import。

- [ ] **Step 4: 跑确认通过** → `.venv310/Scripts/python.exe -m pytest tests/test_caisen_service.py -v`
- [ ] **Step 5: commit**
```bash
git add server/services/caisen_service.py tests/test_caisen_service.py
git commit -m "refactor(caisen): service 改走 SQLite 任务表 + run_replay_async（Spec 1 Task 5）" -m "Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 6: API 端点 + schema

**Files:**
- Modify: `server/schemas/caisen.py`、`server/api/v1/caisen.py`
- Test: `tests/test_caisen_api.py`

**Interfaces:**
- Produces 端点：`POST /replay/async`、`GET /replay/tasks`、`GET /replay/tasks/{id}`、`POST /replay/tasks/{id}/cancel`；废弃 `POST /replay`。
- cancel 端点依赖 scheduler 单例（Task 7 装配到 `app.state.replay_scheduler`）。

- [ ] **Step 1: schema（`server/schemas/caisen.py` 追加）**

```python
class ReplayAsyncRequest(BaseModel):
    start: str
    end: str
    universe: Optional[List[str]] = None
    cfg_override: Dict[str, Any] = Field(default_factory=dict)

class ReplayTaskSummary(BaseModel):
    task_id: str
    created_at: str
    status: str
    progress: int
    start: Optional[str] = None
    end: Optional[str] = None
    universe_n: Optional[int] = None
    cfg_override: dict = {}

class ReplayTaskDetail(ReplayTaskSummary):
    report: Optional[dict] = None
    error: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    last_heartbeat: Optional[str] = None

class CancelResponse(BaseModel):
    task_id: str
    cancelled: bool
    message: str
```

- [ ] **Step 2: 端点（`server/api/v1/caisen.py` 追加 + 删老 POST /replay）**

```python
from fastapi import Request
from server.schemas.caisen import (ReplayAsyncRequest, ReplayTaskSummary,
                                    ReplayTaskDetail, CancelResponse)

@router.post("/replay/async", summary="提交异步回测（返 task_id）")
def replay_async(body: ReplayAsyncRequest):
    return {"task_id": caisen_service.run_replay_async(body)}

@router.get("/replay/tasks", summary="任务列表（降序，可按 status 过滤）")
def list_replay_tasks(status: Optional[str] = None):
    from caisen import replay_tasks_db
    return replay_tasks_db.list_tasks(status=status)

@router.get("/replay/tasks/{task_id}", summary="单任务状态/进度/结果")
def get_replay_task(task_id: str):
    from caisen import replay_tasks_db
    t = replay_tasks_db.get_task(task_id)
    if t is None:
        raise HTTPException(status_code=404, detail=f"任务不存在：{task_id}")
    return t

@router.post("/replay/tasks/{task_id}/cancel", summary="取消任务")
def cancel_replay_task(task_id: str, request: Request):
    sched = getattr(request.app.state, "replay_scheduler", None)
    if sched is None:
        raise HTTPException(status_code=503, detail="调度器未装配")
    sched.request_cancel(task_id)
    return CancelResponse(task_id=task_id, cancelled=True, message="取消信号已发送")
```

> **删除**老 `@router.post("/replay", ...)` 同步端点（spec §6 废弃）+ `tests/test_caisen_api.py` 中对应同步测试用例。`/replay/runs` 三端点签名不变（service 已改读 SQLite）。

- [ ] **Step 3: 写端点测试**（`tests/test_caisen_api.py` 追加）：POST /replay/async 返 task_id；GET /replay/tasks 列出；GET /replay/tasks/{id} 404 case；cancel 返 cancelled。
- [ ] **Step 4: 跑** → `.venv310/Scripts/python.exe -m pytest tests/test_caisen_api.py -v`
- [ ] **Step 5: commit**
```bash
git add server/schemas/caisen.py server/api/v1/caisen.py tests/test_caisen_api.py
git commit -m "feat(caisen): 异步回测 API 端点 + schema + 废弃同步 /replay（Spec 1 Task 6）" -m "Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 7: `main.py` lifespan 装配 pool + 调度器

**Files:**
- Modify: `server/main.py`（lifespan）
- 验证：手动 + 现有 lifespan 集成测试不破坏

- [ ] **Step 1: 改 lifespan（`server/main.py`）**

```python
from concurrent.futures import ProcessPoolExecutor
from caisen import replay_tasks_db, replay_worker, replay_scheduler

# —— lifespan 启动段（在 data_lake load 之后追加）——
replay_tasks_db.init_db()                                    # 建表（幂等）
app.state.replay_pool = ProcessPoolExecutor(
    max_workers=1, initializer=replay_worker._init_worker)   # concurrency=1 串行
app.state.replay_scheduler = replay_scheduler.ReplayScheduler(
    app.state.replay_pool, {}, "data/replay_tasks.db")
app.state.replay_scheduler.start()

# —— lifespan 关闭段（yield 之后，网关 disconnect 之前）——
sched = getattr(app.state, "replay_scheduler", None)
if sched:
    sched.stop()
pool = getattr(app.state, "replay_pool", None)
if pool:
    pool.shutdown(wait=False)
```

- [ ] **Step 2: 手动验证** → 启 `scripts/dev.py`，看日志「启动恢复：N 个残留 RUNNING 标 FAILED」（首次 0）+ scheduler 线程启动无异常。
- [ ] **Step 3: 跑回归** → `.venv310/Scripts/python.exe -m pytest tests/ -q`（确认 lifespan 改动不破坏现有套件；ProcessPoolExecutor 在纯单元测试环境可 monkeypatch 为 None 跳过，或用 TestClient 验证装配）。
- [ ] **Step 4: commit**
```bash
git add server/main.py
git commit -m "feat(server): lifespan 装配回测 pool + 调度器（Spec 1 Task 7）" -m "Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 8: 迁移脚本 + 清理 + E2E

**Files:**
- Create: `scripts/migrate_replay_runs_to_sqlite.py`、`tests/test_migrate_replay_runs.py`

- [ ] **Step 1: 写迁移测试 `tests/test_migrate_replay_runs.py`**

```python
import json
from scripts import migrate_replay_runs_to_sqlite as mig
from caisen import replay_tasks_db

def test_migrate_json_to_sqlite(tmp_path, monkeypatch):
    runs_dir = tmp_path / "replay_runs"
    runs_dir.mkdir()
    # 造一个老格式 JSON（仿 caf3772 replay_runs/<id>.json 结构）
    (runs_dir / "20260701-120000-abcdef.json").write_text(json.dumps({
        "run_id": "20260701-120000-abcdef",
        "created_at": "2026-07-01T12:00:00.000000",
        "request": {"start": "2024-01-01", "end": "2024-06-01",
                    "universe": None, "cfg_override": {}},
        "report": {"n_hits": 5, "win_rate": 0.6}
    }, ensure_ascii=False), encoding="utf-8")
    db = tmp_path / "t.db"
    monkeypatch.setattr(replay_tasks_db, "_DEFAULT_DB_PATH", str(db))
    mig.migrate(str(runs_dir), str(db))
    replay_tasks_db.init_db(str(db))
    rows = replay_tasks_db.list_success_runs(str(db))
    assert len(rows) == 1
    assert rows[0]["report"]["n_hits"] == 5
```

- [ ] **Step 2: 实现迁移脚本 `scripts/migrate_replay_runs_to_sqlite.py`**

```python
# -*- coding: utf-8 -*-
"""replay_runs JSON → SQLite 一次性迁移（Spec 1 Task 8）。

遍历 replay_runs/*.json → 每条 INSERT 为 SUCCESS 行（report_json 存原 report）。
幂等：已存在的 task_id 跳过（防重复迁移）。
"""
import glob, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from caisen import replay_tasks_db
from caisen.replay_tasks_db import _connect, _now_iso


def migrate(runs_dir: str = "replay_runs", db_path: str = "data/replay_tasks.db") -> int:
    replay_tasks_db.init_db(db_path)
    n = 0
    for fp in glob.glob(os.path.join(runs_dir, "*.json")):
        if os.path.basename(fp) == "index.json":
            continue
        data = json.load(open(fp, encoding="utf-8"))
        tid = data.get("run_id") or os.path.basename(fp)[:-5]
        if replay_tasks_db.get_task(tid, db_path) is not None:
            continue                                  # 幂等
        req = data.get("request", {})
        report = data.get("report", {})
        with _connect(db_path) as conn:
            conn.execute(
                """INSERT INTO replay_tasks
                   (task_id, created_at, status, progress, start, end, universe_n,
                    cfg_override, report_json, finished_at)
                   VALUES (?, ?, 'SUCCESS', 100, ?, ?, ?, ?, ?, ?)""",
                (tid, data.get("created_at", _now_iso()), req.get("start"), req.get("end"),
                 -1 if req.get("universe") is None else len(req.get("universe") or []),
                 json.dumps(req.get("cfg_override") or {}, ensure_ascii=False),
                 json.dumps(report, ensure_ascii=False), _now_iso()))
        n += 1
    print(f"迁移完成：{n} 条 replay_runs → {db_path}")
    return n


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="replay_runs JSON → SQLite 迁移")
    ap.add_argument("--runs-dir", default="replay_runs")
    ap.add_argument("--db", default="data/replay_tasks.db")
    a = ap.parse_args()
    migrate(a.runs_dir, a.db)
```

- [ ] **Step 3: 跑迁移测试** → `.venv310/Scripts/python.exe -m pytest tests/test_migrate_replay_runs.py -v`
- [ ] **Step 4: 删除老 `caisen/replay_runs.py` + `tests/test_caisen_replay_runs.py`**（已被 SQLite 取代；先 `grep -rn "replay_runs" --include=*.py` 确认仅迁移脚本和自身测试引用，无残余依赖）
- [ ] **Step 5: E2E 手动验证**：启服务 → `curl -X POST .../replay/async -d '{"start":"2024-01-01","end":"2024-03-01"}'` 拿 task_id → 轮询 `GET /replay/tasks/{id}` 看 PENDING→RUNNING(progress↑)→SUCCESS → `GET /replay/runs` 列出该条
- [ ] **Step 6: 全量回归** → `.venv310/Scripts/python.exe -m pytest tests/ -q`（全绿）+ `python scripts/run_checks.py`（fast gate）
- [ ] **Step 7: commit**
```bash
git add scripts/migrate_replay_runs_to_sqlite.py tests/test_migrate_replay_runs.py
git rm caisen/replay_runs.py tests/test_caisen_replay_runs.py
git commit -m "feat(caisen): replay_runs→SQLite 迁移脚本 + 删除老 JSON 模块（Spec 1 Task 8 完成）" -m "Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Self-Review 记录

**1. Spec coverage**：spec §2-9 全覆盖——§3 进程模型(Task 7)、§3.2 数据流(Task 3/4)、§3.3 重启恢复(Task 4 `_reset_on_startup`)、§4 schema(Task 1)、§4.2 迁移(Task 8)、§4.3 /replay/runs(Task 5)、§5.1 replay 回调(Task 2)、§5.2 编排(Task 3/5)、§6 API(Task 6)、§7 边界(Task 3/4)、§8 测试(各 Task TDD)。无遗漏节。

**2. Placeholder 扫描**：无 TBD/TODO；所有代码步骤含实际代码；无"add error handling"类空话。

**3. Type 一致性**：`ReplayAborted`、`run_replay_worker(task_id, abort_flag, progress_q, heartbeat_q)`、`ReplayScheduler(pool, abort_flags, db_path, run_replay_worker=, clock=)`、`create_task(req) -> task_id`、`list_success_runs()` 跨任务命名/签名一致。

**4. 已知修正项（执行 Task 1 时一并落实）**：Task 1 schema 需加 `universe_json TEXT` 列——worker 装配 price_data 需完整 universe 列表（`universe_n` 仅数量不够）。落实方式：schema 加列、`create_task` 写 `json.dumps(req.get("universe"))`、`_row_to_dict` 读出 `task["universe"]`（None=全市场）、Task 3 worker 用 `task["universe"]` 替代实现注里的占位 `["x"]*n`。

