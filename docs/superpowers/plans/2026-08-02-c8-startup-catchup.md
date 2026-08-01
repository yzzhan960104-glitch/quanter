# C-8 全 job 启动补跑 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** lifespan 启动时把 pipeline 链（采集→校验→data_ready→eod→brief）与 pre_open 补跑到「当前可用的一致态」（C-8 spec：台账 + 日期参数化 + 后台补跑编排）。

**Architecture:** 新增轻量 job 台账 `trading/job_ledger.py`（sqlite，running/done/skipped/failed，cron 与补跑共用防双跑）；`pipeline_then_eod(for_date, run_eod)` + `TradingEngine._eod(data_day, plan_date)` 日期参数化（默认 None 行为零变化）；新增 `trading/catchup.py` 由 lifespan `asyncio.create_task` 后台编排：pipeline(D) 未 done → 补跑链（窗口过只补数据），brief 用 `.last_*_brief` 独立兜底，pre_open 窗口 [09:22, 10:00) 内补挂；补跑失败 → 台账 failed + CRITICAL，不停调度（留今晚 18:00 cron 收敛）。

**Tech Stack:** Python 3.10、sqlite3、asyncio、APScheduler（engine.sched）、FastAPI/uvicorn lifespan、pytest。

## Global Constraints

- **全中文注释**（CLAUDE.md，What + Why，新增/修改代码必须带物理意图注释）。
- **默认路径行为零变化**：`for_date/data_day/plan_date=None` 时，`pipeline_then_eod` / `engine._eod` 与改造前逐字节等价（既有测试全绿）。
- **cron 路径 L1 停调度语义不变**（C-4）：`_critical_guard` / `_CriticalHalt` / `_halted` 不改。
- **补跑失败不停调度**：补跑异常 → 台账 failed + CRITICAL 告警，不 raise 出 lifespan、不 `sched.shutdown`。
- **台账 DB**：`logs/trading_job_run.db`，env `TRADING_JOB_LEDGER_DB` 覆盖；写入失败一律软降级（绝不阻断交易关键路径）。
- **pre_open 补跑窗口**：`[09:22, ENGINE_PRE_OPEN_CATCHUP_UNTIL)`，env 缺省 `"10:00"`。
- **政策 A（只补最近一致态）**：不逐日补历史；plan 日期 ≤ 今天且窗口已过 → `run_eod=False` 不产废计划。
- **全量回归基线**：C-7 后 **1180 passed / 0 failed**（`-m "not slow and not e2e_long"` 默认 addopts），本期零退化。
- **测试入口**：`F:/quanter/.venv310/Scripts/python.exe -m pytest ...`（简写 pytest）。
- **commit 规范**：`feat(c8-vN): ...` / `test(c8-vN): ...`，结尾 `Co-Authored-By: Claude <noreply@anthropic.com>`。

---

## File Structure

| 文件 | 责任 | 本期改动 |
|---|---|---|
| `trading/job_ledger.py` | C-8 job 运行台账（新）：sqlite CRUD + 状态机 + 启动重置 | **V1 新建** |
| `tests/conftest.py` | 全局测试隔离 | **V1**：autouse fixture 把 `TRADING_JOB_LEDGER_DB` 指到 tmp_path |
| `trading/orchestrate/pipeline.py` | C-2 事件链 | **V2**：`for_date`/`run_eod` 参数 + pipeline 台账写入（running/done/failed） |
| `trading/engine.py` | 交易引擎 | **V2**：`_eod(data_day, plan_date)`；**V3**：`pre_open` 拆薄包裹 `_pre_open_impl` + 台账 |
| `trading/catchup.py` | C-8 启动补跑编排（新） | **V4 新建** |
| `presentation/server/main.py` | FastAPI lifespan | **V5**：engine start 后 create_task 补跑 + shutdown cancel |
| `tests/trading/test_job_ledger.py` | 台账单测（新） | V1 |
| `tests/trading/test_c8_date_param.py` | 日期参数化 + pipeline 台账单测（新） | V2 |
| `tests/trading/test_pre_open_ledger.py` | pre_open 台账单测（新） | V3 |
| `tests/trading/test_catchup.py` | 补跑编排单测（新） | V4 |
| `tests/presentation/test_lifespan_consolidation.py` | lifespan 接线单测（既有） | V5 增补 |

---

## Task 1 (V1)：job 运行台账 trading/job_ledger.py

**Files:**
- Create: `trading/job_ledger.py`
- Modify: `tests/conftest.py`（加 autouse 隔离 fixture）
- Test: `tests/trading/test_job_ledger.py`（新建）

**Interfaces:**
- Consumes: 仅标准库（sqlite3/os/pathlib/datetime）。
- Produces（后续 V2-V5 依赖，签名固定）:
  - `init_db(path=None) -> None`
  - `begin_run(job_name: str, business_date: str, started_at: str, path=None) -> None`
  - `finish_run(job_name: str, business_date: str, status: str, message="", path=None) -> None`
  - `latest_status(job_name: str, business_date: str, path=None) -> str | None`
  - `reset_stale_running(path=None) -> int`
  - `_DEFAULT_DB_PATH = "logs/trading_job_run.db"`；env `TRADING_JOB_LEDGER_DB` 优先于默认。

- [ ] **Step 1：写失败测试（新建 test_job_ledger.py）**

创建 `tests/trading/test_job_ledger.py`：

```python
# -*- coding: utf-8 -*-
"""C-8 V1：job 运行台账单测（CRUD + 覆盖式重跑 + 启动重置 + tmp 隔离）。

物理意图（spec §3.1）：(job_name, business_date) 唯一键下的状态机
running/done/skipped/failed；begin_run 可覆盖旧状态（重跑/崩溃恢复）；
reset_stale_running 把遗留 running 置 failed，防幂等守卫永久阻塞。
"""
from trading import job_ledger


def test_begin_finish_status_roundtrip(tmp_path):
    db = str(tmp_path / "job_run.db")
    job_ledger.init_db(db)
    job_ledger.begin_run("pipeline", "2026-07-31", "2026-07-31T18:00:00", path=db)
    assert job_ledger.latest_status("pipeline", "2026-07-31", path=db) == "running"
    job_ledger.finish_run("pipeline", "2026-07-31", "done", path=db)
    assert job_ledger.latest_status("pipeline", "2026-07-31", path=db) == "done"


def test_begin_run_replaces_previous_status(tmp_path):
    db = str(tmp_path / "job_run.db")
    job_ledger.begin_run("pre_open", "2026-08-03", "t1", path=db)
    job_ledger.finish_run("pre_open", "2026-08-03", "done", path=db)
    job_ledger.begin_run("pre_open", "2026-08-03", "t2", path=db)  # 重跑覆盖为 running
    assert job_ledger.latest_status("pre_open", "2026-08-03", path=db) == "running"


def test_latest_status_none_when_missing(tmp_path):
    db = str(tmp_path / "job_run.db")
    assert job_ledger.latest_status("pipeline", "2026-07-31", path=db) is None


def test_reset_stale_running(tmp_path):
    db = str(tmp_path / "job_run.db")
    job_ledger.begin_run("pipeline", "2026-07-31", "t1", path=db)     # 遗留 running
    job_ledger.begin_run("pre_open", "2026-08-03", "t1", path=db)
    job_ledger.finish_run("pre_open", "2026-08-03", "done", path=db)  # done 不受影响
    n = job_ledger.reset_stale_running(path=db)
    assert n == 1
    assert job_ledger.latest_status("pipeline", "2026-07-31", path=db) == "failed"
    assert job_ledger.latest_status("pre_open", "2026-08-03", path=db) == "done"
```

- [ ] **Step 2：跑测试验证失败**

Run: `pytest tests/trading/test_job_ledger.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'trading.job_ledger'`）。

- [ ] **Step 3：实现 trading/job_ledger.py**

创建 `trading/job_ledger.py`：

```python
# -*- coding: utf-8 -*-
"""C-8 job 运行台账：pipeline/pre_open 的跨重启运行状态（漏跑判定 + 幂等守卫）。

物理意图（spec §3.1）：
    生产机不 7x24，启动补跑需要「某业务日某 job 是否已跑」的持久真相源——plan 文件/
    data_ready/.last 文件都是产物反推，语义模糊（无 plan 可能是无信号而非没跑）。
    本台账以 (job_name, business_date) 为键记录状态机 running/done/skipped/failed，
    cron 与启动补跑共用（先查后写），保证「谁先完成谁生效」的最终一致性。

状态语义（spec §3.1）：
    running  = 执行中（进程崩溃残留由 reset_stale_running 启动重置）；
    done     = 流程正常完成（pipeline 含 run_eod=False 的裁剪态；pre_open 含无单可挂）；
    skipped  = pre_open gate 未过（无计划/未确认/网关/数据未就绪）——不算完成，补跑可重试；
    failed   = 采集失败 / data 未就绪 / 未预期异常。

设计约束（Karpathy 极简）：
    - 独立 sqlite（logs/trading_job_run.db），不混入 trading_state.db——台账是操作元数据，
      写失败绝不影响交易关键路径（调用方全部 try/except 包裹）；
    - 每次操作前 CREATE TABLE IF NOT EXISTS（幂等、零装配负担）；
    - path=None fallback 读 env TRADING_JOB_LEDGER_DB > 模块级 _DEFAULT_DB_PATH
      （同 backtest/tasks_db.py 测试隔离范式）。
"""
from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_DEFAULT_DB_PATH = "logs/trading_job_run.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS job_run (
  job_name      TEXT NOT NULL,
  business_date TEXT NOT NULL,
  status        TEXT NOT NULL,
  started_at    TEXT NOT NULL,
  finished_at   TEXT,
  message       TEXT,
  PRIMARY KEY (job_name, business_date)
);
"""


def _db_path(path: Optional[str] = None) -> str:
    """解析 DB 路径：显式 path > env TRADING_JOB_LEDGER_DB > 默认。"""
    if path is not None:
        return path
    env = os.getenv("TRADING_JOB_LEDGER_DB")
    return env if env else _DEFAULT_DB_PATH


def _connect(path: Optional[str] = None) -> sqlite3.Connection:
    """打开连接并保证表存在（幂等，零装配负担）。"""
    db = _db_path(path)
    Path(db).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.execute(_SCHEMA)
    return conn


def init_db(path: Optional[str] = None) -> None:
    """建表（幂等）。显式入口，供启动补跑编排先清场。"""
    conn = _connect(path)
    conn.commit()
    conn.close()


def begin_run(job_name: str, business_date: str, started_at: str,
              path: Optional[str] = None) -> None:
    """标记 job 开始（INSERT OR REPLACE → running，重跑可覆盖旧终态）。"""
    conn = _connect(path)
    conn.execute(
        "INSERT OR REPLACE INTO job_run "
        "(job_name, business_date, status, started_at, finished_at, message) "
        "VALUES (?, ?, 'running', ?, NULL, '')",
        (job_name, business_date, started_at),
    )
    conn.commit()
    conn.close()


def finish_run(job_name: str, business_date: str, status: str,
               message: str = "", path: Optional[str] = None) -> None:
    """落终态（done/skipped/failed）。"""
    conn = _connect(path)
    conn.execute(
        "UPDATE job_run SET status=?, finished_at=?, message=? "
        "WHERE job_name=? AND business_date=?",
        (status, datetime.now().isoformat(), message, job_name, business_date),
    )
    conn.commit()
    conn.close()


def latest_status(job_name: str, business_date: str,
                  path: Optional[str] = None) -> Optional[str]:
    """查某 (job, date) 最新状态；无记录返 None。"""
    conn = _connect(path)
    row = conn.execute(
        "SELECT status FROM job_run WHERE job_name=? AND business_date=?",
        (job_name, business_date),
    ).fetchone()
    conn.close()
    return row[0] if row else None


def reset_stale_running(path: Optional[str] = None) -> int:
    """把遗留 running 全部置 failed('interrupted')，返回重置行数。

    物理意图：进程崩溃/重启会留下 running 残留；若不重置，cron/补跑的
    「running 跳过」守卫会永久阻塞该日 job（与 training_loops_db.reset_interrupted 同范式）。
    """
    conn = _connect(path)
    cur = conn.execute(
        "UPDATE job_run SET status='failed', finished_at=?, message='interrupted' "
        "WHERE status='running'",
        (datetime.now().isoformat(),),
    )
    conn.commit()
    n = cur.rowcount
    conn.close()
    return n
```

- [ ] **Step 4：tests/conftest.py 加 autouse 隔离 fixture**

在 `tests/conftest.py` 末尾追加（物理意图：所有既有/新增测试共享 tmp 台账 DB，防测试写真实 `logs/trading_job_run.db` 污染生产补跑判定）：

```python
# ============ C-8 V1：隔离 job 台账 DB（防测试写真实 logs/trading_job_run.db）============
# Why autouse：pipeline_then_eod / pre_open 改造后会写台账；若不隔离，任何调用这些
# 函数的既有测试都会把「测试日」写成 done，污染真实启动补跑判定（漏跑被误判为已跑）。
# tmp_path 每测试唯一，天然互不干扰。
@pytest.fixture(autouse=True)
def _isolate_job_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_JOB_LEDGER_DB", str(tmp_path / "job_run.db"))
```

- [ ] **Step 5：跑测试验证通过**

Run: `pytest tests/trading/test_job_ledger.py tests/trading/test_pipeline_then_eod.py tests/trading/test_engine.py -q`
Expected: 全部 PASS（台账 4 用例 + 既有 pipeline/engine 用例零退化）。

- [ ] **Step 6：commit**

```bash
git add trading/job_ledger.py tests/trading/test_job_ledger.py tests/conftest.py
git commit -m "feat(c8-v1): job 运行台账（sqlite 状态机 running/done/skipped/failed + 启动重置）

- trading/job_ledger.py：begin_run/finish_run/latest_status/reset_stale_running
- env TRADING_JOB_LEDGER_DB 覆盖 logs/trading_job_run.db（测试隔离）
- conftest autouse 隔离台账 DB（防既有测试污染生产补跑判定）
- 4 用例：roundtrip/重跑覆盖/无记录/reset

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 2 (V2)：日期参数化 + pipeline 台账写入

**Files:**
- Modify: `trading/orchestrate/pipeline.py`
- Modify: `trading/engine.py`（仅 `_eod` 签名与日期取值两处 + docstring）
- Test: `tests/trading/test_c8_date_param.py`（新建）

**Interfaces:**
- Consumes: `job_ledger.begin_run/finish_run/latest_status`（V1 产出）。
- Produces（后续 V3-V5 依赖，签名固定）:
  - `pipeline_then_eod(engine, *, for_date: str | None = None, run_eod: bool = True) -> None`
  - `TradingEngine._eod(*, data_day: str | None = None, plan_date: str | None = None) -> None`
  - pipeline 台账：入口 `begin_run("pipeline", today, ...)`；完成 `finish_run("pipeline", today, "done")`；采集失败/data 未就绪/异常 → `"failed"`。

- [ ] **Step 1：写失败测试（新建 test_c8_date_param.py）**

创建 `tests/trading/test_c8_date_param.py`：

```python
# -*- coding: utf-8 -*-
"""C-8 V2：日期参数化 + pipeline 台账单测。

覆盖（spec §3.2/§3.4）：
  - pipeline_then_eod(for_date=D) → data_ready 落 D + engine._eod(data_day=D, plan_date=next(D))
  - run_eod=False → 不调 _eod，链尾 brief 仍跑
  - 默认路径（for_date=None）→ engine._eod() 无参（行为零变化）
  - 台账：成功 → done；data 未就绪 → failed；采集失败 → failed 后抛 _CriticalHalt
  - engine._eod(data_day/plan_date) → gate 用 data_day、eod_plan 落盘 key 用 plan_date
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from data.freshness import FreshnessResult
from trading import job_ledger


@pytest.mark.asyncio
async def test_for_date_passes_explicit_dates_to_eod():
    """for_date=D：data_ready 落 D，engine._eod 收 (data_day=D, plan_date=next(D))，链尾 brief 仍跑。"""
    from trading.orchestrate import pipeline as pl
    eng = MagicMock()
    eng._eod = AsyncMock()
    with patch.object(pl, "is_trading_day", return_value=True), \
         patch.object(pl, "asyncio.create_subprocess_exec") as cse, \
         patch.object(pl, "resolve_active", return_value=[]), \
         patch.object(pl, "next_trading_day", return_value="2026-08-03"), \
         patch.object(pl, "check_freshness",
                      return_value=FreshnessResult("daily", True, "2026-07-31",
                                                   "2026-07-31", "PASS")), \
         patch.object(pl, "upsert_data_ready") as udr, \
         patch("ops.brief_all.run_brief_all", new=AsyncMock()):
        proc = AsyncMock(); proc.wait.return_value = 0
        cse.return_value = proc
        await pl.pipeline_then_eod(eng, for_date="2026-07-31")
    assert udr.call_args.args[0] == "2026-07-31"   # data_ready 落 for_date（非今天）
    assert eng._eod.await_args.kwargs == {"data_day": "2026-07-31", "plan_date": "2026-08-03"}
    assert job_ledger.latest_status("pipeline", "2026-07-31") == "done"


@pytest.mark.asyncio
async def test_default_path_calls_eod_without_args():
    """for_date=None（cron 正常路径）→ engine._eod() 无参，行为零变化。"""
    from trading.orchestrate import pipeline as pl
    eng = MagicMock()
    eng._eod = AsyncMock()
    with patch.object(pl, "is_trading_day", return_value=True), \
         patch.object(pl, "asyncio.create_subprocess_exec") as cse, \
         patch.object(pl, "resolve_active", return_value=[]), \
         patch.object(pl, "check_freshness",
                      return_value=FreshnessResult("daily", True, "2026-08-03",
                                                   "2026-08-03", "PASS")), \
         patch("ops.brief_all.run_brief_all", new=AsyncMock()):
        proc = AsyncMock(); proc.wait.return_value = 0
        cse.return_value = proc
        await pl.pipeline_then_eod(eng)
    assert eng._eod.await_args.args == ()
    assert eng._eod.await_args.kwargs == {}


@pytest.mark.asyncio
async def test_run_eod_false_skips_eod_but_runs_brief():
    """run_eod=False（窗口已过只补数据）→ 不调 _eod，链尾 brief 仍跑，台账 done。"""
    from trading.orchestrate import pipeline as pl
    eng = MagicMock()
    eng._eod = AsyncMock()
    with patch.object(pl, "is_trading_day", return_value=True), \
         patch.object(pl, "asyncio.create_subprocess_exec") as cse, \
         patch.object(pl, "resolve_active", return_value=[]), \
         patch.object(pl, "check_freshness",
                      return_value=FreshnessResult("daily", True, "2026-07-31",
                                                   "2026-07-31", "PASS")), \
         patch("ops.brief_all.run_brief_all", new=AsyncMock()) as rba:
        proc = AsyncMock(); proc.wait.return_value = 0
        cse.return_value = proc
        await pl.pipeline_then_eod(eng, for_date="2026-07-31", run_eod=False)
    eng._eod.assert_not_awaited()
    rba.assert_awaited_once()
    assert job_ledger.latest_status("pipeline", "2026-07-31") == "done"


@pytest.mark.asyncio
async def test_data_unready_records_failed():
    """data 未就绪（采集成功但 freshness 不过）→ 台账 failed，eod 不跑。"""
    from trading.orchestrate import pipeline as pl
    eng = MagicMock()
    eng._eod = AsyncMock()
    with patch.object(pl, "is_trading_day", return_value=True), \
         patch.object(pl, "asyncio.create_subprocess_exec") as cse, \
         patch.object(pl, "resolve_active", return_value=[]), \
         patch.object(pl, "check_freshness",
                      return_value=FreshnessResult("daily", False, None,
                                                   "2026-07-31", "缺")), \
         patch("ops.brief_all.run_brief_all", new=AsyncMock()):
        proc = AsyncMock(); proc.wait.return_value = 0
        cse.return_value = proc
        await pl.pipeline_then_eod(eng, for_date="2026-07-31")
    eng._eod.assert_not_awaited()
    assert job_ledger.latest_status("pipeline", "2026-07-31") == "failed"


@pytest.mark.asyncio
async def test_collect_failure_records_failed_and_raises(monkeypatch):
    """采集子进程 rc!=0 → 台账 failed 后抛 _CriticalHalt（cron 路径 L1 停调度语义不变）。"""
    from trading.engine import _CriticalHalt
    from trading.orchestrate import pipeline as pl

    class _FakeProc:
        async def wait(self):
            return 1
    async def _fake_exec(*a, **kw):
        return _FakeProc()
    monkeypatch.setattr(pl.asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(pl, "is_trading_day", lambda d: True)
    with pytest.raises(_CriticalHalt, match="采集子进程失败"):
        await pl.pipeline_then_eod(object(), for_date="2026-07-31")
    assert job_ledger.latest_status("pipeline", "2026-07-31") == "failed"


@pytest.mark.asyncio
async def test_eod_data_day_gate_uses_data_day(monkeypatch):
    """_eod(data_day=D)：交易日 gate 用 D（周末补跑周五链可过 gate）。"""
    from trading.engine import TradingEngine
    eng = TradingEngine.__new__(TradingEngine)
    seen = []
    monkeypatch.setattr("trading.engine.calendar.is_trading_day",
                        lambda d: seen.append(d) or True)
    monkeypatch.setattr("experiment.resolver.resolve_active", lambda: [])
    await eng._eod(data_day="2026-07-31", plan_date="2026-08-03")
    assert seen == ["2026-07-31"]


@pytest.mark.asyncio
async def test_eod_plan_date_flows_to_eod_plan(monkeypatch):
    """_eod(plan_date=P)：eod_plan 落盘 key 用 P（补跑产下一交易日计划）。"""
    from trading.engine import TradingEngine
    eng = TradingEngine.__new__(TradingEngine)
    monkeypatch.setattr("trading.engine.calendar.is_trading_day", lambda d: True)
    monkeypatch.setattr("experiment.resolver.resolve_active",
                        lambda: [MagicMock(strategy_name="s", params={}, weight=1.0,
                                           experiment_id="e1")])
    monkeypatch.setattr("strategies.registry.build_strategy",
                        lambda *a, **kw: MagicMock())
    monkeypatch.setattr("pandas.read_parquet", lambda *a, **kw: MagicMock())
    monkeypatch.setattr(eng, "_load_universe", lambda lake: [])
    monkeypatch.setattr(eng, "_load_integrity_ctx", lambda d: (set(), set()))
    monkeypatch.setattr("data.integrity.filter_universe_by_continuity",
                        lambda *a, **kw: [])
    monkeypatch.setattr(eng, "_resolve_id_window", lambda s: 10)
    monkeypatch.setattr(eng, "_resolve_cooldown_days", lambda exps: 0)
    monkeypatch.setattr(eng, "_broadcast_positions_pnl", AsyncMock())
    eod_plan = AsyncMock()
    monkeypatch.setattr("trading.engine.eod_plan", eod_plan)
    await eng._eod(data_day="2026-07-31", plan_date="2026-08-03")
    assert eod_plan.await_args.args[0] == "2026-08-03"
    assert eod_plan.await_args.args[1] == []      # 无信号（universe 空）
```

- [ ] **Step 2：跑测试验证失败**

Run: `pytest tests/trading/test_c8_date_param.py -v`
Expected: FAIL（`pipeline_then_eod() got an unexpected keyword argument 'for_date'`；`_eod() got an unexpected keyword argument 'data_day'`；`job_ledger` 断言失败——台账未写入）。

- [ ] **Step 3：实现 pipeline.py 日期参数化 + 台账**

**3a. 改 import（trading/orchestrate/pipeline.py）**：

```python
from trading.calendar import expected_latest_trade_day, is_trading_day, next_trading_day
from trading import clock, job_ledger
```

**3b. 模块 docstring 尾部追加**：

```python
C-8 补跑参数化（spec §3.2）：
    for_date = 事件链数据日（补跑传最近已收盘交易日 T）；run_eod = 是否产计划
    （窗口已过传 False 只补数据+brief，政策 A 不产过期计划）。默认 None/True 与 C-2 行为等价。
台账（spec §3.4）：pipeline 状态 running→done/failed 由本函数统一落，
    cron 与启动补跑共用（先查后写防双跑）。
```

**3c. 加台账 helper（模块级，`pipeline_then_eod` 前）**：

```python
def _ledger_finish(today: str, status: str, message: str = "") -> None:
    """pipeline 台账落终态（失败软降级，绝不阻断事件链主流程）。"""
    try:
        job_ledger.finish_run("pipeline", today, status, message)
    except Exception:
        logger.exception("job_ledger finish_run 失败（不阻断主流程）")
```

**3d. 重写 `pipeline_then_eod`（整函数替换）**：

```python
async def pipeline_then_eod(engine, *, for_date: str | None = None,
                            run_eod: bool = True) -> None:
    """C-2 事件链：采集 → 等完成 → 按策略声明校验数据 → eod → brief。

    Args:
        engine: 持有 ``async _eod()`` 的交易引擎（编排层只调这一个方法，
            不读引擎内部状态——低耦合）。
        for_date: 补跑用——事件链数据日（YYYY-MM-DD，缺省=clock.today()）。
            C-8 spec §3.2：T+1 早上补跑 T 日链时，data_ready 必须落 T、
            eod 必须产 next_trading_day(T) 计划，否则日期错位（C-6 同源风险）。
        run_eod: 补跑窗口已过时传 False——只补 采集→校验→data_ready→brief，
            不为已过期交易日产废计划（政策 A，spec §2）。
    """
    today = for_date or clock.today()
    if not is_trading_day(today):
        logger.info("pipeline_then_eod 跳过：非交易日 %s", today)
        return
    _st = job_ledger.latest_status("pipeline", today)
    if _st in ("running", "done"):
        logger.info("pipeline_then_eod 跳过：%s 已 %s（台账守卫，cron/补跑不双跑）",
                    today, _st)
        return
    try:
        job_ledger.begin_run("pipeline", today, clock.now().isoformat())
    except Exception:
        logger.exception("job_ledger begin_run 失败（不阻断主流程）")
    try:
        # 1. 采集子进程（原 ops/data_pipeline.py，T1→采→T2）
        log_path = ROOT / "logs" / "data_pipeline.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        # M2：本编排现跑在长生命周期 uvicorn 进程内（不再是短命 schtasks 进程），裸
        # ``open(...,"ab")`` 作为 subprocess stdout 会让文件句柄泄漏累积（每天 +1）。显式
        # 捕获到局部变量，await proc.wait() 后 close()——确保句柄确定性地归还 OS。
        log_fh = open(log_path, "ab")
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, str(ROOT / "ops" / "data_pipeline.py"), cwd=str(ROOT),
                stdout=log_fh,
                stderr=asyncio.subprocess.STDOUT,
            )
            rc = await proc.wait()
        finally:
            log_fh.close()
        # C-4 U3c：采集子进程失败（rc!=0）= T 日增量未落湖 → 用 T-1 数据算 T+1 计划 = 时序 bug
        # （[[eod-date-offbyone-fix]] 同源风险）。升 L1：raise _CriticalHalt → engine _halt 停调度，
        # 绝不用陈旧数据产废信号（spec §3 pipeline 采集失败=L1）。
        # C-8 V2：抛前落台账 failed（补跑路径由 catchup 捕获转 failed+CRITICAL，不停调度）。
        if rc != 0:
            _ledger_finish(today, "failed", f"采集子进程失败 rc={rc}")
            from trading.engine import _CriticalHalt
            raise _CriticalHalt(f"采集子进程失败 rc={rc}（T 日增量未落湖，拒产 T+1 计划）")
        # 2. 装配本次在线实验策略 → 收集依赖 key 并集（D3）
        keys: set[str] = set()
        try:
            for exp in resolve_active():
                strat = build_strategy(exp.strategy_name, exp.params)
                keys |= set(strat.required_data_keys)
        except Exception:
            logger.exception("策略依赖解析失败，回退默认 {daily}")
            keys = {"daily"}
        keys = keys or {"daily"}
        # 3. 按声明的 key 逐个校验（复用 check_freshness 纯函数，不读旧 parquet mtime）
        # C-6 V3：单一时间源（时点传 expected_latest_trade_day，clock.now() 返 datetime 等价）。
        expected = expected_latest_trade_day(clock.now())
        results = {k: check_freshness(k, expected) for k in keys}
        all_ok = all(r.ok for r in results.values())
        # 4. 落就绪事件（供 pre_open 防御性双检）——C-8 V2：日期用 today（for_date），
        #    补跑时落 T 而非今天，否则 pre_open gate 查 expected_latest_trade_day=T 永远 None。
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
            _ledger_finish(today, "failed", msg)
            try:
                from infra.notifier import (build_default_manager, fire_and_forget,
                                            NotificationManager)
                build_default_manager()
                fire_and_forget(
                    NotificationManager.get_default().notify_risk_event(msg, "CRITICAL")
                )
            except Exception:
                logger.exception("CRITICAL 告警发送失败")
            return  # 不跑 eod，不产废信号
        # 5. 全绿 → 跑 eod（C-8 V2：补跑传显式 data_day/plan_date；默认路径零变化）
        if run_eod:
            if for_date is not None:
                await engine._eod(data_day=today, plan_date=next_trading_day(today))
            else:
                await engine._eod()
        # 6. 事件链尾 → Brief 播报（D7）。失败不阻断已完成的 eod plan。
        try:
            from ops.brief_all import run_brief_all
            await run_brief_all()
        except Exception:
            logger.exception("brief 播报失败（不阻断 eod 已完成的 plan）")
        _ledger_finish(today, "done")
    except Exception:
        _ledger_finish(today, "failed", "未预期异常")
        raise
```

**3e. engine.py `_eod` 参数化**：

`trading/engine.py` 中 `async def _eod(self) -> None:` 改为：

```python
    async def _eod(self, *, data_day: str | None = None,
                   plan_date: str | None = None) -> None:
```

docstring 的 Args 段追加：

```python
        Args:
            data_day:  补跑用——数据日 T（df_upto 截止 / 完整性 ctx / cooldown / scan_live
                       截止），缺省 clock.today()（C-6 入口缓存语义不变）。
            plan_date: 补跑用——计划生效日（eod_plan 落盘 key），缺省 clock.trading_day()
                       = next_trading_day(clock.today())。C-8 spec §3.2：T+1 早上补跑 T 日链
                       必须传 plan_date=next(T)，否则产出 T+2 废计划。
```

函数体开头两处改为（其余代码原样不动，继续用 `_today`/`_td` 局部变量）：

```python
        # C-6 V2：单一时间源 + 入口缓存（防同轮跨午夜漂移）。
        # C-8 V2：data_day/plan_date 显式注入（启动补跑传最近已收盘交易日）；
        # 缺省 None 时与 C-6 完全等价（_today=clock.today，_td=clock.trading_day）。
        _today = data_day or clock.today()
        if not calendar.is_trading_day(_today):
            logger.info("eod_plan 跳过：非交易日 %s", _today)
            return
        _td = plan_date or clock.trading_day()
```

- [ ] **Step 4：跑测试验证通过**

Run: `pytest tests/trading/test_c8_date_param.py tests/trading/test_pipeline_then_eod.py tests/trading/test_engine.py tests/trading/test_engine_eod_injection.py tests/trading/test_pre_open_l1_halt.py -q`
Expected: 全 PASS（新 7 用例 + 既有相关用例零退化）。

- [ ] **Step 5：commit**

```bash
git add trading/orchestrate/pipeline.py trading/engine.py tests/trading/test_c8_date_param.py
git commit -m "feat(c8-v2): 事件链日期参数化 + pipeline 台账（for_date/run_eod + data_day/plan_date）

- pipeline_then_eod(engine, for_date=None, run_eod=True)：data_ready 落 for_date，
  eod 传 next_trading_day(for_date)；默认 None 行为零变化
- TradingEngine._eod(data_day=None, plan_date=None)：gate/截止用 data_day，落盘用 plan_date
- pipeline 台账：入口 running，完成 done，采集失败/data 未就绪/异常 failed
- 7 用例：显式日期透传/默认无参/run_eod=False/未就绪 failed/采集失败 failed+抛/gate/落盘 key

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 3 (V3)：pre_open 台账包裹

**Files:**
- Modify: `trading/engine.py`（`pre_open` 拆薄包裹 + `_pre_open_impl` + 顶部 import job_ledger）
- Test: `tests/trading/test_pre_open_ledger.py`（新建）

**Interfaces:**
- Consumes: `job_ledger.begin_run/finish_run`（V1）；`_ACTIVE_ENGINE` / `get_gateway` / `trading_plan.load_plan`（既有）。
- Produces（V4 依赖）:
  - `pre_open(date) -> dict`（签名不变，新增台账语义：gate 未过/无计划/未确认 → `skipped`；成功 → `done`；异常 → `failed` 后上抛）
  - `_pre_open_impl(date) -> dict`（原 pre_open 函数体改名，逻辑零改动）

- [ ] **Step 1：写失败测试（新建 test_pre_open_ledger.py）**

创建 `tests/trading/test_pre_open_ledger.py`：

```python
# -*- coding: utf-8 -*-
"""C-8 V3：pre_open 台账包裹单测（running→done/skipped/failed）。

物理意图（spec §3.4）：cron（engine._pre_open）与启动补跑（trading.catchup）共用
模块级 pre_open；台账在此统一落——skipped 不算完成，补跑窗口内可重试。
"""
import pytest
from unittest.mock import AsyncMock, patch

from trading import job_ledger


@pytest.mark.asyncio
async def test_pre_open_gate_skip_records_skipped():
    """gate 未过（无计划等）→ 台账 skipped（不算完成，补跑可重试）。"""
    from trading.engine import pre_open
    fake_engine = AsyncMock()
    fake_engine._pre_open_gate = AsyncMock(return_value=(False, "无计划"))
    with patch("trading.engine._ACTIVE_ENGINE", fake_engine), \
         patch("trading.engine.get_gateway", return_value=None):
        result = await pre_open("2026-08-03")
    assert result["skipped"] == "无计划"
    assert job_ledger.latest_status("pre_open", "2026-08-03") == "skipped"


@pytest.mark.asyncio
async def test_pre_open_no_plan_records_skipped():
    """计划不存在 → 台账 skipped。"""
    from trading.engine import pre_open
    fake_engine = AsyncMock()
    fake_engine._pre_open_gate = AsyncMock(return_value=(True, ""))
    with patch("trading.engine._ACTIVE_ENGINE", fake_engine), \
         patch("trading.engine.get_gateway", return_value=None), \
         patch("trading.engine.load_plan", return_value=None):
        result = await pre_open("2026-08-03")
    assert result["reason"] == "无计划"
    assert job_ledger.latest_status("pre_open", "2026-08-03") == "skipped"


@pytest.mark.asyncio
async def test_pre_open_success_records_done():
    """主流程正常完成（空订单列表）→ 台账 done。"""
    from trading.engine import pre_open
    fake_engine = AsyncMock()
    fake_engine._pre_open_gate = AsyncMock(return_value=(True, ""))
    with patch("trading.engine._ACTIVE_ENGINE", fake_engine), \
         patch("trading.engine.get_gateway", return_value=None), \
         patch("trading.engine.load_plan",
               return_value={"confirmed": True, "orders": []}):
        result = await pre_open("2026-08-03")
    assert result["submitted"] == 0
    assert job_ledger.latest_status("pre_open", "2026-08-03") == "done"


@pytest.mark.asyncio
async def test_pre_open_exception_records_failed_and_raises():
    """未预期异常 → 台账 failed 后上抛（cron 路径由 _critical_guard 按 C-4 L1 停调度）。"""
    from trading.engine import pre_open
    fake_engine = AsyncMock()
    fake_engine._pre_open_gate = AsyncMock(side_effect=RuntimeError("DB 故障"))
    with patch("trading.engine._ACTIVE_ENGINE", fake_engine), \
         patch("trading.engine.get_gateway", return_value=None):
        with pytest.raises(RuntimeError, match="DB 故障"):
            await pre_open("2026-08-03")
    assert job_ledger.latest_status("pre_open", "2026-08-03") == "failed"
```

- [ ] **Step 2：跑测试验证失败**

Run: `pytest tests/trading/test_pre_open_ledger.py -v`
Expected: FAIL（`pre_open` 尚无台账写入——`latest_status` 返回 None 断言失败）。

- [ ] **Step 3：实现 pre_open 薄包裹**

**3a. `trading/engine.py` 顶部 import**：`from trading import (calendar, dynamic_whitelist, ...)` 元组追加 `job_ledger`：

```python
from trading import (
    calendar,
    dynamic_whitelist,
    job_ledger,
    qmt_market_data,
    reconcile_job,
    trading_plan,
)
```

**3b. 现有 `async def pre_open(date: str) -> dict:` 改名**：

```python
async def _pre_open_impl(date: str) -> dict:
```

（函数体、docstring、所有内部代码原样不动。）

**3c. 在 `_pre_open_impl` 前插入薄包裹**：

```python
async def pre_open(date: str) -> dict:
    """T 日开盘前入口（C-8 V3 台账包裹）：running → done/skipped/failed。

    物理意图（spec §3.4）：cron（engine._pre_open）与启动补跑（trading.catchup）共用
    本函数，台账在此统一落（begin/finish）——「谁先完成谁生效」防双跑；
    skipped（gate 未过/无计划/未确认）不算完成，补跑窗口内可重试。
    实现 = 薄包裹 + 原逻辑改名 _pre_open_impl（行为零变更）。
    """
    try:
        job_ledger.begin_run("pre_open", date, clock.now().isoformat())
    except Exception:
        logger.exception("job_ledger begin_run 失败（不阻断 pre_open）")
    try:
        result = await _pre_open_impl(date)
    except Exception:
        try:
            job_ledger.finish_run("pre_open", date, "failed", "未预期异常")
        except Exception:
            logger.exception("job_ledger finish_run 失败（不阻断 pre_open）")
        raise
    status = "skipped" if (result.get("skipped") or result.get("reason")) else "done"
    message = str(result.get("skipped") or result.get("reason") or "")
    try:
        job_ledger.finish_run("pre_open", date, status, message)
    except Exception:
        logger.exception("job_ledger finish_run 失败（不阻断 pre_open）")
    return result
```

- [ ] **Step 4：跑测试验证通过**

Run: `pytest tests/trading/test_pre_open_ledger.py tests/trading/test_engine.py tests/trading/test_engine_pre_open_gate.py tests/trading/test_pre_open_l1_halt.py tests/trading/test_e2e_trading_flow.py -q`
Expected: 全 PASS（新 4 用例 + 既有 pre_open 相关用例零退化）。

- [ ] **Step 5：commit**

```bash
git add trading/engine.py tests/trading/test_pre_open_ledger.py
git commit -m "feat(c8-v3): pre_open 台账包裹（running→done/skipped/failed）

- pre_open 拆薄包裹 + _pre_open_impl（原逻辑零改动）
- gate 未过/无计划/未确认 → skipped（不算完成，补跑窗口内可重试）
- 主流程完成 → done；异常 → failed 后上抛（cron L1 语义不变）
- 4 用例：gate skip/无计划/success/异常

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 4 (V4)：启动补跑编排 trading/catchup.py

**Files:**
- Create: `trading/catchup.py`
- Test: `tests/trading/test_catchup.py`（新建）

**Interfaces:**
- Consumes: `job_ledger`（V1）、`pipeline_then_eod(engine, for_date=..., run_eod=...)`（V2）、`pre_open(date)`（V3）、`trading.calendar`（expected_latest_trade_day / next_trading_day / is_trading_day）、`trading.clock`、`ops.brief_all.run_brief_all`、`broadcast.__main__.last_brief_file/_read_last`。
- Produces（V5 依赖）:
  - `run_startup_catchup(engine) -> dict`，返回 `{"pipeline": bool, "brief": bool, "pre_open": bool, "pre_open_note": str, "error": str | None}`
  - `_catchup_until() -> datetime.time`（env `ENGINE_PRE_OPEN_CATCHUP_UNTIL`，缺省 `10:00`）

- [ ] **Step 1：写失败测试（新建 test_catchup.py）**

创建 `tests/trading/test_catchup.py`：

```python
# -*- coding: utf-8 -*-
"""C-8 V4：启动补跑编排单测（判定/裁剪/顺序/brief 兜底/失败语义）。

物理意图（spec §3.3）：只补最近一致态——pipeline(D) 未 done 且其 18:00 已过才补；
plan 日期已过 pre_open 窗口 → run_eod=False 只补数据；pre_open 窗口
[09:22, ENGINE_PRE_OPEN_CATCHUP_UNTIL) 内且未 done 才补；失败 → failed+CRITICAL 不 raise。
"""
import pytest
from datetime import datetime, time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from trading import job_ledger


def _now(y, m, d, hh, mm):
    """固定 now（测试用，patch trading.clock.now 返回）。"""
    return datetime(y, m, d, hh, mm)


@pytest.mark.asyncio
async def test_pipeline_catchup_skipped_when_done(monkeypatch):
    """pipeline(D) 已 done → 不补跑。"""
    from trading import calendar as cal
    from trading import clock as clk
    monkeypatch.setattr(clk, "now", lambda: _now(2026, 8, 3, 9, 40))
    monkeypatch.setattr(cal, "expected_latest_trade_day", lambda now: "2026-07-31")
    job_ledger.begin_run("pipeline", "2026-07-31", "t")
    job_ledger.finish_run("pipeline", "2026-07-31", "done")
    with patch("trading.orchestrate.pipeline.pipeline_then_eod", new=AsyncMock()) as pl:
        from trading.catchup import run_startup_catchup
        result = await run_startup_catchup(MagicMock())
    assert result["pipeline"] is False
    pl.assert_not_awaited()


@pytest.mark.asyncio
async def test_pipeline_catchup_runs_full_chain_in_window(monkeypatch):
    """窗口内（09:40）：pipeline(D) 补跑 run_eod=True，随后 pre_open 补挂。"""
    from trading import calendar as cal
    from trading import clock as clk
    monkeypatch.setattr(clk, "now", lambda: _now(2026, 8, 3, 9, 40))
    monkeypatch.setattr(cal, "expected_latest_trade_day", lambda now: "2026-07-31")
    monkeypatch.setattr(cal, "next_trading_day", lambda d: "2026-08-03")
    monkeypatch.setattr(cal, "is_trading_day", lambda d: True)
    with patch("trading.orchestrate.pipeline.pipeline_then_eod", new=AsyncMock()) as pl, \
         patch("trading.engine.pre_open", new=AsyncMock()) as po:
        from trading.catchup import run_startup_catchup
        result = await run_startup_catchup(MagicMock())
    assert result["pipeline"] is True
    pl.assert_awaited_once()
    assert pl.await_args.kwargs == {"for_date": "2026-07-31", "run_eod": True}
    assert result["pre_open"] is True
    po.assert_awaited_once_with("2026-08-03")


@pytest.mark.asyncio
async def test_pipeline_catchup_run_eod_false_after_window(monkeypatch):
    """窗口已过（10:30）：run_eod=False 只补数据；pre_open 不补 + CRITICAL 知会。"""
    from trading import calendar as cal
    from trading import clock as clk
    monkeypatch.setattr(clk, "now", lambda: _now(2026, 8, 3, 10, 30))
    monkeypatch.setattr(cal, "expected_latest_trade_day", lambda now: "2026-07-31")
    monkeypatch.setattr(cal, "next_trading_day", lambda d: "2026-08-03")
    monkeypatch.setattr(cal, "is_trading_day", lambda d: True)
    with patch("trading.orchestrate.pipeline.pipeline_then_eod", new=AsyncMock()) as pl, \
         patch("trading.engine.pre_open", new=AsyncMock()) as po, \
         patch("trading.catchup._alert_critical") as alert:
        from trading.catchup import run_startup_catchup
        result = await run_startup_catchup(MagicMock())
    assert pl.await_args.kwargs == {"for_date": "2026-07-31", "run_eod": False}
    assert result["pre_open"] is False
    po.assert_not_awaited()
    alert.assert_called_once()
    assert "窗口已过" in alert.call_args.args[0]


@pytest.mark.asyncio
async def test_pipeline_catchup_skipped_before_1800_same_day(monkeypatch):
    """D==today 且 now<18:00 → 不补跑（今晚 cron 将处理，防提前拉未清算数据）。"""
    from trading import calendar as cal
    from trading import clock as clk
    monkeypatch.setattr(clk, "now", lambda: _now(2026, 8, 3, 16, 0))
    monkeypatch.setattr(cal, "expected_latest_trade_day", lambda now: "2026-08-03")
    with patch("trading.orchestrate.pipeline.pipeline_then_eod", new=AsyncMock()) as pl:
        from trading.catchup import run_startup_catchup
        result = await run_startup_catchup(MagicMock())
    assert result["pipeline"] is False
    pl.assert_not_awaited()


@pytest.mark.asyncio
async def test_weekend_catchup_produces_monday_plan(monkeypatch):
    """周六补跑：D=周五，plan_date=周一（>today）→ run_eod=True；pre_open 非交易日跳过。"""
    from trading import calendar as cal
    from trading import clock as clk
    monkeypatch.setattr(clk, "now", lambda: _now(2026, 8, 1, 10, 0))   # 周六
    monkeypatch.setattr(cal, "expected_latest_trade_day", lambda now: "2026-07-31")
    monkeypatch.setattr(cal, "next_trading_day", lambda d: "2026-08-03")
    monkeypatch.setattr(cal, "is_trading_day", lambda d: False)
    with patch("trading.orchestrate.pipeline.pipeline_then_eod", new=AsyncMock()) as pl, \
         patch("trading.engine.pre_open", new=AsyncMock()) as po:
        from trading.catchup import run_startup_catchup
        result = await run_startup_catchup(MagicMock())
    assert pl.await_args.kwargs == {"for_date": "2026-07-31", "run_eod": True}
    assert result["pre_open"] is False
    po.assert_not_awaited()


@pytest.mark.asyncio
async def test_brief_catchup_when_last_file_stale(monkeypatch, tmp_path):
    """pipeline(D) done 但 .last 文件缺失/陈旧 → run_brief_all 补播一次。"""
    from trading import calendar as cal
    from trading import clock as clk
    monkeypatch.setattr(clk, "now", lambda: _now(2026, 8, 3, 9, 40))
    monkeypatch.setattr(cal, "expected_latest_trade_day", lambda now: "2026-07-31")
    monkeypatch.setattr(cal, "next_trading_day", lambda d: "2026-08-03")
    monkeypatch.setattr(cal, "is_trading_day", lambda d: True)
    job_ledger.begin_run("pipeline", "2026-07-31", "t")
    job_ledger.finish_run("pipeline", "2026-07-31", "done")
    # 三个幂等文件重定向到 tmp：trading 陈旧、其余缺失 → 判定需补播
    files = {
        "trading": tmp_path / ".last_trading_brief",
        "strategy": tmp_path / ".last_strategy_brief",
        "data": tmp_path / ".last_data_brief",
    }
    files["trading"].write_text("2026-07-30", encoding="utf-8")
    def _fake_last(bot):
        return files[bot]
    with patch("broadcast.__main__.last_brief_file", side_effect=_fake_last), \
         patch("ops.brief_all.run_brief_all", new=AsyncMock()) as rba, \
         patch("trading.engine.pre_open", new=AsyncMock()) as po:
        from trading.catchup import run_startup_catchup
        result = await run_startup_catchup(MagicMock())
    assert result["brief"] is True
    rba.assert_awaited_once()
    po.assert_awaited_once_with("2026-08-03")   # brief 兜底不影响 pre_open


@pytest.mark.asyncio
async def test_catchup_failure_alerts_and_does_not_raise(monkeypatch):
    """补跑异常 → error 记录 + CRITICAL，不 raise（不停调度/不阻断 uvicorn）。"""
    from trading import calendar as cal
    from trading import clock as clk
    monkeypatch.setattr(clk, "now", lambda: _now(2026, 8, 3, 9, 40))
    monkeypatch.setattr(cal, "expected_latest_trade_day", lambda now: "2026-07-31")
    monkeypatch.setattr(cal, "next_trading_day", lambda d: "2026-08-03")
    monkeypatch.setattr(cal, "is_trading_day", lambda d: True)
    async def _boom(*a, **kw):
        raise RuntimeError("采集挂了")
    with patch("trading.orchestrate.pipeline.pipeline_then_eod", new=_boom), \
         patch("trading.catchup._alert_critical") as alert:
        from trading.catchup import run_startup_catchup
        result = await run_startup_catchup(MagicMock())
    assert result["error"] == "采集挂了"
    alert.assert_called_once()


@pytest.mark.asyncio
async def test_pre_open_skipped_before_window(monkeypatch):
    """now<09:22 → pre_open 不补（09:22 cron 将处理），不告警。"""
    from trading import calendar as cal
    from trading import clock as clk
    monkeypatch.setattr(clk, "now", lambda: _now(2026, 8, 3, 9, 0))
    monkeypatch.setattr(cal, "expected_latest_trade_day", lambda now: "2026-07-31")
    monkeypatch.setattr(cal, "next_trading_day", lambda d: "2026-08-03")
    monkeypatch.setattr(cal, "is_trading_day", lambda d: True)
    with patch("trading.orchestrate.pipeline.pipeline_then_eod", new=AsyncMock()) as pl, \
         patch("trading.engine.pre_open", new=AsyncMock()) as po, \
         patch("trading.catchup._alert_critical") as alert:
        from trading.catchup import run_startup_catchup
        result = await run_startup_catchup(MagicMock())
    assert result["pre_open"] is False
    po.assert_not_awaited()
    alert.assert_not_called()
    pl.assert_awaited_once()   # pipeline 补跑不受窗口起点限制
```

- [ ] **Step 2：跑测试验证失败**

Run: `pytest tests/trading/test_catchup.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'trading.catchup'`）。

- [ ] **Step 3：实现 trading/catchup.py**

创建 `trading/catchup.py`：

```python
# -*- coding: utf-8 -*-
"""C-8 启动补跑编排：lifespan startup 后台任务，补到「当前可用的一致态」。

物理意图（spec §3.3）：生产机不 7x24，offline 跨任一触发点 → 启动时补跑：
    ① pipeline(D) 未 done 且其 18:00 已过 → 补 采集→校验→data_ready→eod→brief
       （D = expected_latest_trade_day(now) = 最近已收盘交易日）；
    ② plan 日期已过 pre_open 窗口 → run_eod=False 只补数据+brief（政策 A，不产废计划）；
    ③ brief 独立兜底：pipeline done 但 .last_<bot>_brief < D → 补播一次；
    ④ pre_open 窗口 [09:22, ENGINE_PRE_OPEN_CATCHUP_UNTIL) 内且未 done → 补挂单；
      窗口已过且未 done → CRITICAL 知会（政策 A 显式不静默）。

失败语义（spec §3.3 / review 点 1）：补跑任何异常 → 台账 failed + CRITICAL，
不停调度、不阻断 uvicorn——留今晚 18:00 cron 自然收敛（停调度会连收敛机会一起杀掉）。

线程模型：本模块只在 lifespan startup 被 asyncio.create_task 调一次；内部全 async
（pipeline_then_eod 的采集子进程 await proc.wait() 不阻塞事件循环）。
"""
from __future__ import annotations

import logging
import os
from datetime import time
from typing import Optional

from trading import calendar, clock, job_ledger

logger = logging.getLogger(__name__)

# pre_open 补跑窗口起点 = cron 09:22（起点前由 cron 处理，错过 cron 才补）。
WINDOW_START = time(9, 22)


def _catchup_until() -> time:
    """pre_open 补跑窗口截止（HH:MM，env ENGINE_PRE_OPEN_CATCHUP_UNTIL，缺省 10:00）。"""
    raw = os.getenv("ENGINE_PRE_OPEN_CATCHUP_UNTIL", "10:00")
    try:
        hh, mm = raw.split(":")
        return time(int(hh), int(mm))
    except Exception:
        logger.warning("ENGINE_PRE_OPEN_CATCHUP_UNTIL=%r 解析失败，回退 10:00", raw)
        return time(10, 0)


def _alert_critical(msg: str) -> None:
    """CRITICAL 钉钉（与 engine._alert_critical 同通道；失败软降级）。"""
    try:
        from infra.notifier import NotificationManager, fire_and_forget
        fire_and_forget(NotificationManager.get_default().notify_risk_event(msg, "CRITICAL"))
    except Exception:
        logger.exception("C-8 CRITICAL 告警发送失败（不阻断补跑）：%s", msg)


def _brief_missed(latest_day: str) -> bool:
    """任一 .last_<bot>_brief 文件缺失或内容 < latest_day → 需补播（幂等文件去重）。"""
    from broadcast.__main__ import _read_last, last_brief_file
    for bot in ("trading", "strategy", "data"):
        try:
            last = _read_last(last_brief_file(bot))
        except Exception:
            last = ""
        if not last or last < latest_day:
            return True
    return False


async def _catchup_pipeline(engine) -> bool:
    """补跑 pipeline 链（D=最近已收盘交易日）；返回是否执行了补跑。

    判定（spec §3.3）：D 未 running/done 且（D < today 或 now >= 18:00）→ 补跑；
    D == today 且 now < 18:00 → 不补（今晚 cron 正常处理，避免提前拉未清算数据）。
    run_eod 裁剪：plan_date=next_trading_day(D) ≤ today 且 now > 窗口截止 → False
    （不为已过期交易日产废计划，政策 A）。
    """
    from trading.orchestrate.pipeline import pipeline_then_eod
    now = clock.now()
    today = clock.today()
    d = calendar.expected_latest_trade_day(now)
    status = job_ledger.latest_status("pipeline", d)
    if status in ("running", "done"):
        logger.info("C-8 pipeline 补跑跳过：%s 已 %s", d, status)
        return False
    if d == today and now.time() < time(18, 0):
        logger.info("C-8 pipeline 补跑跳过：%s 今晚 18:00 cron 将处理", d)
        return False
    plan_date = calendar.next_trading_day(d)
    run_eod = not (plan_date <= today and now.time() > _catchup_until())
    logger.warning("C-8 启动补跑 pipeline：D=%s plan_date=%s run_eod=%s",
                   d, plan_date, run_eod)
    await pipeline_then_eod(engine, for_date=d, run_eod=run_eod)
    return True


async def _catchup_brief(latest_day: str) -> bool:
    """brief 独立兜底：pipeline done 但幂等文件 < D → 补播一次。"""
    if job_ledger.latest_status("pipeline", latest_day) != "done":
        return False
    if not _brief_missed(latest_day):
        return False
    from ops.brief_all import run_brief_all
    logger.warning("C-8 启动补跑 brief：D=%s（.last_*_brief 缺失/陈旧）", latest_day)
    await run_brief_all()
    return True


async def _catchup_pre_open() -> tuple[bool, str]:
    """补跑 pre_open（今日窗口内且未 done）；窗口已过且未 done → CRITICAL 知会。"""
    from trading.engine import pre_open
    now = clock.now()
    today = clock.today()
    if not calendar.is_trading_day(today):
        return False, "非交易日"
    status = job_ledger.latest_status("pre_open", today)
    if status in ("running", "done"):
        return False, f"已 {status}"
    until = _catchup_until()
    t = now.time()
    if t < WINDOW_START:
        return False, "窗口未开始（09:22 cron 将处理）"
    if t >= until:
        msg = f"C-8 pre_open 窗口已过（now={t:%H:%M} >= {until:%H:%M}），今日不补挂单"
        logger.warning(msg)
        _alert_critical(msg)
        return False, msg
    logger.warning("C-8 启动补跑 pre_open：today=%s", today)
    await pre_open(today)
    return True, "已补跑"


async def run_startup_catchup(engine) -> dict:
    """lifespan startup 后台补跑编排（幂等，只补最近一致态）。

    Returns:
        {"pipeline": bool, "brief": bool, "pre_open": bool,
         "pre_open_note": str, "error": str | None}
    """
    result = {"pipeline": False, "brief": False, "pre_open": False,
              "pre_open_note": "", "error": None}
    try:
        job_ledger.init_db()
        job_ledger.reset_stale_running()
    except Exception:
        logger.exception("C-8 台账初始化失败（跳过本次补跑）")
        result["error"] = "台账初始化失败"
        return result
    try:
        latest_day = calendar.expected_latest_trade_day(clock.now())
        result["pipeline"] = await _catchup_pipeline(engine)
        result["brief"] = await _catchup_brief(latest_day)
        result["pre_open"], result["pre_open_note"] = await _catchup_pre_open()
    except Exception as e:
        logger.exception("C-8 启动补跑失败")
        _alert_critical(f"C-8 启动补跑失败：{e}")
        result["error"] = str(e)
    return result
```


- [ ] **Step 4：跑测试验证通过**

Run: `pytest tests/trading/test_catchup.py -v`
Expected: 8 用例全 PASS。

- [ ] **Step 5：commit**

```bash
git add trading/catchup.py tests/trading/test_catchup.py
git commit -m "feat(c8-v4): 启动补跑编排（pipeline/brief/pre_open 判定 + 窗口裁剪 + 失败语义）

- D=expected_latest_trade_day(now) 未 done 且 18:00 已过 → 补跑链
- run_eod 裁剪：plan 日期≤today 且窗口过 → False（政策 A 不产废计划）
- pre_open 窗口 [09:22, 10:00)（env ENGINE_PRE_OPEN_CATCHUP_UNTIL）内补挂
- brief 独立兜底（.last_*_brief < D → 补播一次）
- 失败 → error+CRITICAL 不 raise（不停调度，留 18:00 cron 收敛）
- 8 用例：done 跳过/窗口内全链/窗口外裁剪/18:00 前/周末/ brief 兜底/失败/窗口前

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 5 (V5)：lifespan 接线 + 全量回归

**Files:**
- Modify: `presentation/server/main.py`（顶部加 `import asyncio` + startup 补跑任务 + shutdown cancel）
- Modify: `tests/presentation/test_lifespan_consolidation.py`（增补 2 用例）

**Interfaces:**
- Consumes: `trading.catchup.run_startup_catchup(engine)`（V4 产出）；`app.state.trading_engine`（既有）。
- Produces: `app.state.catchup_task`（asyncio.Task，shutdown cancel）。

- [ ] **Step 1：写失败测试（追加 test_lifespan_consolidation.py）**

在 `tests/presentation/test_lifespan_consolidation.py` 末尾追加：

```python
# ============ C-8 V5：启动补跑任务接线（engine start 后 create_task + shutdown cancel）============
# 物理意图（spec §3.6）：engine 装配+start 后，lifespan 创建后台补跑任务；仅
# sched.running=True（scheduler 已起）时触发——影子期不足时补跑无意义。


@pytest.mark.asyncio
async def test_lifespan_creates_catchup_task_when_engine_started():
    """engine 已 start → lifespan startup 创建 catchup_task 并 await run_startup_catchup。"""
    from fastapi import FastAPI
    from presentation.server.main import lifespan

    app = FastAPI()
    stack, _start, _stop, eng = _mock_lifespan_dependencies()
    eng.sched.running = True                              # engine 已 start
    stack.enter_context(patch("trading.__main__.check_shadow_gate", return_value=True))
    catchup = stack.enter_context(
        patch("trading.catchup.run_startup_catchup", new=AsyncMock()))
    with stack:
        async with lifespan(app):
            task = getattr(app.state, "catchup_task", None)
            assert task is not None                        # 任务已创建
            await task                                     # 等补跑协程完成（AsyncMock 秒完）
    catchup.assert_awaited_once()


@pytest.mark.asyncio
async def test_lifespan_skips_catchup_when_engine_not_started():
    """影子期不足（sched.running=False）→ 不创建 catchup_task。"""
    from fastapi import FastAPI
    from presentation.server.main import lifespan

    app = FastAPI()
    stack, _start, _stop, eng = _mock_lifespan_dependencies()   # 默认 running=False
    catchup = stack.enter_context(
        patch("trading.catchup.run_startup_catchup", new=AsyncMock()))
    with stack:
        async with lifespan(app):
            assert not hasattr(app.state, "catchup_task")
    catchup.assert_not_awaited()
```

- [ ] **Step 2：跑测试验证失败**

Run: `pytest tests/presentation/test_lifespan_consolidation.py -v -k catchup`
Expected: FAIL（lifespan 未创建 `catchup_task` → AttributeError/hasattr 断言失败）。

- [ ] **Step 3：实现 main.py 接线**

**3a. 顶部 import 追加**（`import logging` 行附近）：

```python
import asyncio
```

**3b. lifespan startup（C-7 discovery 补跑块之后、`yield` 之前）追加**：

```python
    # C-8 V5：全 job 启动补跑（台账 + 后台编排，spec §3.6）。
    # 物理意图：生产机不 7x24，offline 跨 18:00 pipeline / 09:22 pre_open 时，启动补跑
    # 到「当前可用的一致态」（采集→data_ready→eod→brief + pre_open 窗口内补挂）。
    # 仅 engine 已 start（sched.running）时触发——影子期不足 scheduler 缺席，补跑无意义。
    # 软降级：创建异常不阻断 uvicorn（与上方 engine/training/connect/discovery 同范式）。
    try:
        from trading.catchup import run_startup_catchup
        _eng_c8 = getattr(app.state, "trading_engine", None)
        if _eng_c8 is not None and getattr(_eng_c8.sched, "running", False):
            app.state.catchup_task = asyncio.create_task(run_startup_catchup(_eng_c8))
            logging.getLogger(__name__).info("C-8 启动补跑任务已创建")
    except Exception:
        logging.getLogger(__name__).exception("C-8 启动补跑创建异常（已忽略）")
```

**3c. lifespan shutdown（`yield` 之后、断网关之前）追加**：

```python
    # C-8 V5：shutdown 取消启动补跑任务（软降级；任务随事件循环销毁自然结束，
    # 显式 cancel 更干净——采集子进程 await proc.wait() 会随事件循环关闭被中断）。
    _cu_task = getattr(app.state, "catchup_task", None)
    if _cu_task is not None:
        try:
            _cu_task.cancel()
        except Exception:
            logging.getLogger(__name__).exception("C-8 启动补跑任务 cancel 异常（已忽略）")
```

- [ ] **Step 4：跑测试验证通过**

Run: `pytest tests/presentation/test_lifespan_consolidation.py -v`
Expected: 全 PASS（既有 + 新增 2 用例）。

- [ ] **Step 5：全量回归**

Run: `pytest tests -q`
Expected: **1180+ 新增（约 1180 + 21）passed / 0 failed**（`-m "not slow and not e2e_long"` 默认）。0 failed 硬指标。

若 failed：按失败信息回 Task 1-5 修复后重跑。

- [ ] **Step 6：spec §6 验收 1-8 逐条核对 + commit**

| # | 验收项 | 核对 |
|---|---|---|
| 1 | job_run 台账落库（pipeline/pre_open）+ 状态机 + 启动重置 | V1 测试 PASS |
| 2 | pipeline_then_eod/engine._eod 日期参数化，默认路径零变化 | V2 测试 PASS + 既有全绿 |
| 3 | lifespan 启动后台补跑：pipeline(D) 未 done → 全链；窗口过 → run_eod=False | V4/V5 测试 PASS |
| 4 | pre_open 窗口 [09:22, 截止)（env 可调），gate/幂等沿用，skipped 可重试 | V3/V4 测试 PASS |
| 5 | brief 独立兜底（.last 文件 < D → 补播一次） | V4 测试 PASS |
| 6 | cron vs 补跑无双跑（台账 running/done 守卫） | V2 守卫 + V4 测试 PASS |
| 7 | 补跑失败 → failed + CRITICAL，不 halt、不阻断 uvicorn；cron L1 不变 | V2/V4 测试 PASS |
| 8 | 全量回归 1180 基线零退化 | Step 5 `0 failed` |

```bash
git add presentation/server/main.py tests/presentation/test_lifespan_consolidation.py
git commit -m "feat(c8-v5): lifespan 启动补跑接线 + 全量回归零退化

- engine start 后 create_task(run_startup_catchup)（仅 sched.running=True）
- shutdown cancel catchup_task（软降级）
- 2 用例：engine started 创建任务 / 影子期跳过
- 全量 1180+21 passed / 0 failed，spec §6 验收 1-8 全绿

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Self-Review

**1. Spec 覆盖**：
- spec §3.1 job 台账 → Task 1 ✓
- spec §3.2 日期参数化（for_date/data_day/plan_date/run_eod）→ Task 2 ✓
- spec §3.3 补跑判定与编排（pipeline/brief/pre_open + 窗口裁剪 + 失败语义）→ Task 4 ✓
- spec §3.4 台账写入点（pipeline/pre_open cron 与补跑共用）→ Task 2/3 ✓
- spec §3.5 幂等与并发（running/done 守卫 + reset_stale_running）→ Task 1/2/4 ✓
- spec §3.6 lifespan 接线 → Task 5 ✓
- spec §6 验收 1-8 → Task 5 Step 6 逐条 ✓

**2. Placeholder 扫描**：无 TBD/TODO；每 step 含完整代码或精确 diff 描述。

**3. 类型一致性**：
- `job_ledger.begin_run/finish_run/latest_status/reset_stale_running` 签名在 V1-V5 一致（path=None fallback）✓
- `pipeline_then_eod(engine, *, for_date=None, run_eod=True)` 在 V2 产出、V4 消费一致 ✓
- `TradingEngine._eod(*, data_day=None, plan_date=None)` 在 V2 产出、V2 测试消费一致 ✓
- `run_startup_catchup(engine) -> dict` 在 V4 产出、V5 消费一致（返回 dict 不强制解包）✓
- `pre_open(date)` 签名不变（V3 包裹），`_pre_open_impl(date)` 新增内部名 ✓

**4. 关键设计决策（plan 阶段定）**：
- 台账隔离：`tests/conftest.py` autouse fixture 设 `TRADING_JOB_LEDGER_DB`，既有测试零改动（避免逐文件 monkeypatch 爆炸）。
- 补跑调用模块级 `pipeline_then_eod` / `pre_open`（不经 `@_critical_guard`），失败转 failed+CRITICAL 不停调度（spec review 点 1/2）。
- `run_eod=False` 放在 `pipeline_then_eod` 参数而非 catchup 重复编排——单链实现，brief 链尾自然带出。
- pre_open 窗口判定在任务执行时点求值（R1：09:21 boot → 09:22 cron gate 跳过 → 补跑链完成后窗口内重试）。
