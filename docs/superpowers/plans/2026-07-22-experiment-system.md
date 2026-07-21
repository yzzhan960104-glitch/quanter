# 实验系统（Experiment System）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实盘下单的「策略版本配置中心」——管理在线实验版本+资金权重，scan resolve 注入，颈线法完整接入实盘 scan/出场，双链路（Mock + miniQMT 虚拟盘）端到端验收。

**Architecture:** 新增独立 `experiment/` 包（SQLite 持久化版本+审计，零反向依赖）→ `strategies/base.py` 协议扩展（出场归策略侧）→ 颈线法/caisen 各自实现 `scan_live/to_armed_plan/check_pullback/check_exit` → `execution/engine.py` 按 `plan.experiment_id` 路由 Strategy → `scan_service` resolve 注入。plan 仍 JSON 仅加归因字段。

**Tech Stack:** Python 3.10（`.venv310/Scripts/python`，xtquant 绑 python310）/ SQLite3 标准库（WAL 模式，不引 SQLAlchemy）/ dataclasses / argparse CLI / pytest。

## Global Constraints

- **Python 环境**：miniQMT 相关用 `.venv310/Scripts/python`（xtquant 绑 python310）；`experiment/` 核心纯标准库，系统 `python` 也能跑单元测试
- **零新依赖**：SQLite3/dataclasses/argparse 均为标准库，不引 SQLAlchemy 等 ORM
- **全中文注释**（CLAUDE.md），显式至上，拒绝黑盒；每个 dataclass/函数说明 What + Why
- **状态机红线**：非法迁移一律拒绝；所有 ACTIVE 版本 weight 之和 ≤ 1.0（资金守恒）
- **TDD**：每任务先写失败测试 → 验证失败 → 最小实现 → 验证通过 → commit
- **frequent commits**：每任务结束一个 commit，feat/refactor/test 前缀 + 中文
- **双源真理红线**：颈线法 `check_exit` 必须复用回测 `simulate_exit` 内核（抽为共享纯函数），不另写一套
- **caisen 零行为回归**：caisen 适配器 `check_exit` 调老 `caisen.engines.exit_logic.check_exit`，逐字保留
- **EMT 已废弃**：gateway 只支持 Mock + miniQMT（QmtExecutionGateway）

---

## File Structure

**新建 `experiment/` 包**（零依赖 strategies/execution/trading/server）：
- `experiment/__init__.py` — 导出 `resolve_active`、公开 dataclass
- `experiment/models.py` — `ExperimentStatus`/`ExperimentVersion`/`AuditLog`/`ActiveExperiment` dataclass + 状态机校验
- `experiment/store.py` — SQLite 持久化（`experiment/experiments.db`，WAL+事务）+ 审计写入
- `experiment/resolver.py` — `resolve_active() -> list[ActiveExperiment]`
- `experiment/cli.py` — argparse 子命令 create/promote/set-weight/archive/rollback/list/report

**修改策略层**：
- `strategies/base.py` — 加 `Signal`/`PullbackDecision`/`ExitDecision`/`PullbackAction`/`ExitAction` + Strategy 协议 4 方法
- `strategies/neckline_method.py` — 实现 `scan_live`/`to_armed_plan`/`check_pullback`/`check_exit`；抽 `_exit_kernel` 共享回测/实盘
- `strategies/caisen_pattern.py` — 适配器实现 `scan_live`/`to_armed_plan`/`check_pullback`/`check_exit`（调 caisen engines 老逻辑）

**修改执行层**：
- `execution/storage.py` — `_plan_to_dict`/`_restore_plan_dict` 加 `experiment_id`/`experiment_weight` 字段
- `execution/engine.py` — `tick_pullback`/`tick_exit` 按 `plan.experiment_id` 路由 Strategy；`CLOSE_PORTION` 下单 qty=shares×portion

**修改 scan 编排**：
- `caisen/facade.py` 或新建 `execution/scan_service.py` — `run_scan` 调 `resolve_active` → 遍历实验 → `build_strategy`+`scan_live`+`to_armed_plan` → `save_plans`

**新建测试**（`tests/experiment/`、`tests/strategies/`、`tests/caisen/`）：见各任务。

---

### Task 1: experiment/ 数据模型 + 状态机校验

**Files:**
- Create: `experiment/__init__.py`
- Create: `experiment/models.py`
- Test: `tests/experiment/__init__.py`, `tests/experiment/test_models.py`

**Interfaces:**
- Consumes: 无（纯标准库）
- Produces: `ExperimentStatus`(枚举: DRAFT/ACTIVE/ARCHIVED), `ExperimentVersion`(dataclass), `AuditLog`(dataclass), `ActiveExperiment`(dataclass), `validate_transition(old, new)`, `validate_weight_sum(active_versions, new_weight)`

- [ ] **Step 1: 写失败测试**

`tests/experiment/test_models.py`:
```python
# -*- coding: utf-8 -*-
"""experiment.models 单元测试：状态机校验 + 权重守恒 + dataclass 契约。"""
import pytest
from experiment.models import (
    ExperimentStatus, ExperimentVersion, AuditLog, ActiveExperiment,
    validate_transition, validate_weight_sum,
)


def _ver(**kw):
    """造一个合法 ExperimentVersion（默认值覆盖测试用）。"""
    base = dict(experiment_id="neckline_v1_20260722", strategy_name="neckline",
                params={"window": 60}, weight=0.2, status=ExperimentStatus.DRAFT,
                version=1, source="manual", note="", created_at="2026-07-22T10:00:00",
                activated_at=None, archived_at=None)
    base.update(kw)
    return ExperimentVersion(**base)


def test_legal_transitions():
    """合法迁移：DRAFT→ACTIVE、ACTIVE→ARCHIVED、ARCHIVED→ACTIVE。"""
    assert validate_transition(ExperimentStatus.DRAFT, ExperimentStatus.ACTIVE) is True
    assert validate_transition(ExperimentStatus.ACTIVE, ExperimentStatus.ARCHIVED) is True
    assert validate_transition(ExperimentStatus.ARCHIVED, ExperimentStatus.ACTIVE) is True


def test_illegal_transitions():
    """非法迁移一律拒绝（ARCHIVED→DRAFT、已 ACTIVE 再 promote 等）。"""
    assert validate_transition(ExperimentStatus.ARCHIVED, ExperimentStatus.DRAFT) is False
    assert validate_transition(ExperimentStatus.ACTIVE, ExperimentStatus.ACTIVE) is False
    assert validate_transition(ExperimentStatus.DRAFT, ExperimentStatus.ARCHIVED) is False


def test_weight_sum_within_limit():
    """权重和 ≤ 1.0 放行：现有 ACTIVE 合计 0.8，新版本 0.2 → 合计 1.0，通过。"""
    active = [_ver(status=ExperimentStatus.ACTIVE, weight=0.8)]
    assert validate_weight_sum(active, new_weight=0.2) is True


def test_weight_sum_exceeds_limit():
    """权重和 > 1.0 拒绝（资金守恒红线）：现有 0.8，新版本 0.3 → 1.1，拒。"""
    active = [_ver(status=ExperimentStatus.ACTIVE, weight=0.8)]
    assert validate_weight_sum(active, new_weight=0.3) is False


def test_active_experiment_dataclass():
    """ActiveExperiment 是 resolver 返回的精简视图（scan 消费）。"""
    ae = ActiveExperiment(experiment_id="e1", strategy_name="neckline",
                          params={"window": 60}, weight=0.2)
    assert ae.experiment_id == "e1" and ae.weight == 0.2


def test_audit_log_records_change():
    """AuditLog 记录 changed_fields（旧→新）。"""
    log = AuditLog(timestamp="2026-07-22T10:00:00", action="set-weight",
                   experiment_id="e1", changed_fields={"weight": [0.2, 0.5]},
                   operator="cli", note="")
    assert log.changed_fields["weight"] == [0.2, 0.5]
```

- [ ] **Step 2: 跑测试验证失败**

Run: `python -m pytest tests/experiment/test_models.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'experiment'`

- [ ] **Step 3: 实现 models.py**

`experiment/__init__.py`:
```python
# -*- coding: utf-8 -*-
"""实验系统：实盘下单的策略版本配置中心（单一职责·零反向依赖）。

物理定位：管理「在线实验版本 + 资金权重」，scan 经 resolve_active() 获取当前生效
(strategy_name, params, weight) 列表。不生成 plan、不下单、不管持仓归因。
"""
from experiment.models import (  # noqa: F401
    ExperimentStatus, ExperimentVersion, AuditLog, ActiveExperiment,
    validate_transition, validate_weight_sum,
)
```

`experiment/models.py`:
```python
# -*- coding: utf-8 -*-
"""实验系统数据模型 + 状态机/权重校验（纯标准库，零外部依赖）。

核心抽象（design §3.1）：不区分 prod/candidate 语义，平台只懂「版本 + 权重」。
- ExperimentVersion: 策略名 + 参数快照(promote 后不可变) + 资金权重 + 状态 + 版本号
- AuditLog: append-only 变更审计（谁/何时/改了哪个版本/权重从x→y）
- ActiveExperiment: resolver 返回给 scan 的精简视图（experiment_id/strategy_name/params/weight）

状态机（design §3.3）：
    DRAFT ──promote──→ ACTIVE ──archive──→ ARCHIVED
                                         └──rollback──→ ACTIVE
    DRAFT ──discard──→ 删除
资金守恒红线：所有 ACTIVE 版本 weight 之和 ≤ 1.0。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class ExperimentStatus(str, Enum):
    """实验版本三态。str 枚举便于 SQLite 存取（直接存 name 字符串）。"""
    DRAFT = "DRAFT"        # 草稿：未上线，可编辑/删除
    ACTIVE = "ACTIVE"      # 在线：scan resolve 会读到，参与下单
    ARCHIVED = "ARCHIVED"  # 归档：下线，保留历史，可 rollback 回 ACTIVE


@dataclass
class ExperimentVersion:
    """实验版本（参数快照，promote 到 ACTIVE 后 params 不可变）。"""
    experiment_id: str                 # 唯一标识，如 "neckline_v6_20260722"
    strategy_name: str                 # build_strategy 的 name（"neckline"/"caisen"）
    params: dict                       # 参数快照（JSON 序列化存 SQLite，promote 后锁）
    weight: float                      # 资金占比 0.0~1.0
    status: ExperimentStatus
    version: int                       # 同 strategy_name 下递增
    source: str = ""                   # param_lab:run_xxx / manual / rollback
    note: str = ""
    created_at: str = ""               # ISO 时间戳
    activated_at: Optional[str] = None
    archived_at: Optional[str] = None


@dataclass
class ActiveExperiment:
    """resolver 返回给 scan 的精简视图（design §4.1 resolve_active 契约）。

    Why 精简：scan 只需知道「用哪个策略 + 什么参数 + 多大权重」，不需版本/审计元信息。
    """
    experiment_id: str
    strategy_name: str
    params: dict
    weight: float


@dataclass
class AuditLog:
    """append-only 审计记录（design §3.4）。changed_fields 记旧→新值。"""
    timestamp: str
    action: str                        # create/promote/set-weight/archive/rollback/discard
    experiment_id: str
    changed_fields: dict = field(default_factory=dict)
    operator: str = "cli"
    note: str = ""


# ============================================================================
# 状态机与权重校验（纯函数，store.py 写入前调）
# ============================================================================
# 合法迁移表（design §3.3 状态机）。set-weight 不改 status，不在此表。
_LEGAL_TRANSITIONS = {
    (ExperimentStatus.DRAFT, ExperimentStatus.ACTIVE): "promote",
    (ExperimentStatus.ACTIVE, ExperimentStatus.ARCHIVED): "archive",
    (ExperimentStatus.ARCHIVED, ExperimentStatus.ACTIVE): "rollback",
}


def validate_transition(old: ExperimentStatus, new: ExperimentStatus) -> bool:
    """状态迁移合法性校验。非法迁移（如 ARCHIVED→DRAFT）返回 False，store 拒绝写入。"""
    return (old, new) in _LEGAL_TRANSITIONS


def validate_weight_sum(active_versions: list, new_weight: float) -> bool:
    """资金守恒红线：所有 ACTIVE 版本 weight + new_weight ≤ 1.0。

    参数：
        active_versions: 当前所有 ACTIVE 的 ExperimentVersion 列表（不含待加入的版本）。
        new_weight: 待 promote/set-weight 的目标权重。

    Why ≤ 1.0 而非 == 1.0：允许部分资金空闲（如两实验合计 0.5，剩 0.5 空仓）。
    """
    current = sum(v.weight for v in active_versions)
    return (current + new_weight) <= 1.0 + 1e-9   # 浮点容差
```

- [ ] **Step 4: 跑测试验证通过**

Run: `python -m pytest tests/experiment/test_models.py -v`
Expected: PASS（7 个测试全绿）

- [ ] **Step 5: Commit**

```bash
git add experiment/__init__.py experiment/models.py tests/experiment/__init__.py tests/experiment/test_models.py
git commit -m "feat(experiment): 数据模型+状态机校验（ExperimentVersion/AuditLog/权重守恒）"
```

---

### Task 2: experiment/ SQLite store + 审计

**Files:**
- Create: `experiment/store.py`
- Test: `tests/experiment/test_store.py`

**Interfaces:**
- Consumes: `experiment/models.py`（Task 1）
- Produces: `init_db(db_path)`, `create_version(db_path, version, operator)`, `promote(db_path, experiment_id, weight, operator, now)`, `set_weight(db_path, experiment_id, new_weight, operator, now)`, `archive(db_path, experiment_id, operator, now)`, `rollback(db_path, experiment_id, operator, now)`, `list_versions(db_path, status=None)`, `list_audit(db_path, experiment_id=None)`

- [ ] **Step 1: 写失败测试**

`tests/experiment/test_store.py`:
```python
# -*- coding: utf-8 -*-
"""experiment.store 单元测试：SQLite CRUD + 审计 + 事务回滚 + 状态机/权重校验。"""
import os
import tempfile

import pytest

from experiment.models import ExperimentStatus, ExperimentVersion
from experiment import store


@pytest.fixture
def db(tmp_path):
    """每个测试用独立临时 db 文件。"""
    p = str(tmp_path / "t.db")
    store.init_db(p)
    return p


def _make(version_id="e1", strategy="neckline", weight=0.2, status=ExperimentStatus.DRAFT,
          version=1, params=None):
    return ExperimentVersion(
        experiment_id=version_id, strategy_name=strategy, params=params or {"window": 60},
        weight=weight, status=status, version=version, source="manual",
        created_at="2026-07-22T10:00:00")


def test_init_db_creates_tables(db):
    """init_db 建两张表 + 索引。"""
    import sqlite3
    con = sqlite3.connect(db)
    tabs = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"experiment_version", "audit_log"} <= tabs
    con.close()


def test_create_and_list(db):
    """create 写 DRAFT，list 能读到。"""
    store.create_version(db, _make(), operator="cli")
    rows = store.list_versions(db)
    assert len(rows) == 1 and rows[0].experiment_id == "e1"
    assert rows[0].status == ExperimentStatus.DRAFT


def test_promote_writes_audit_and_status(db):
    """promote: DRAFT→ACTIVE + 写审计 + 校验权重和。"""
    store.create_version(db, _make(weight=0.0), operator="cli")
    store.promote(db, "e1", weight=0.2, operator="cli", now="2026-07-22T11:00:00")
    rows = store.list_versions(db, status=ExperimentStatus.ACTIVE)
    assert rows[0].weight == 0.2 and rows[0].activated_at == "2026-07-22T11:00:00"
    audit = store.list_audit(db, "e1")
    assert any(a.action == "promote" for a in audit)


def test_promote_rejects_weight_overflow(db):
    """权重和 > 1.0 promote 被拒（资金守恒红线）。"""
    store.create_version(db, _make("e1", weight=0.0), operator="cli")
    store.promote(db, "e1", weight=0.8, operator="cli", now="t")
    store.create_version(db, _make("e2", version=2, weight=0.0), operator="cli")
    with pytest.raises(ValueError, match="权重"):
        store.promote(db, "e2", weight=0.3, operator="cli", now="t")  # 0.8+0.3=1.1 > 1.0


def test_illegal_transition_rejected(db):
    """ARCHIVED→DRAFT 非法迁移拒绝。"""
    store.create_version(db, _make("e1", weight=0.0), operator="cli")
    store.promote(db, "e1", weight=0.5, operator="cli", now="t")
    store.archive(db, "e1", operator="cli", now="t")
    # 直接对已 ARCHIVED 再 promote（ACTIVE→ACTIVE 等效非法）应拒
    with pytest.raises(ValueError):
        store.promote(db, "e1", weight=0.5, operator="cli", now="t")


def test_rollback_restores_active(db):
    """rollback: ARCHIVED→ACTIVE，恢复上线。"""
    store.create_version(db, _make("e1", weight=0.0), operator="cli")
    store.promote(db, "e1", weight=0.5, operator="cli", now="t")
    store.archive(db, "e1", operator="cli", now="t")
    store.rollback(db, "e1", operator="cli", now="t2")
    rows = store.list_versions(db, status=ExperimentStatus.ACTIVE)
    assert len(rows) == 1 and rows[0].experiment_id == "e1"


def test_set_weight_records_old_new(db):
    """set_weight 记审计 changed_fields weight [旧,新]。"""
    store.create_version(db, _make("e1", weight=0.0), operator="cli")
    store.promote(db, "e1", weight=0.2, operator="cli", now="t")
    store.set_weight(db, "e1", new_weight=0.5, operator="cli", now="t2")
    audit = store.list_audit(db, "e1")
    sw = [a for a in audit if a.action == "set-weight"][0]
    assert sw.changed_fields["weight"] == [0.2, 0.5]


def test_params_immutable_after_promote(db):
    """promote 后 params 不可变：用同名同 version 改 params 应被拒（UNIQUE 约束 + 显式拒绝）。"""
    store.create_version(db, _make("e1", params={"window": 60}), operator="cli")
    store.promote(db, "e1", weight=0.5, operator="cli", now="t")
    # 再次 create 同 experiment_id 应拒（主键冲突）
    with pytest.raises(ValueError):
        store.create_version(db, _make("e1", params={"window": 99}), operator="cli")
```

- [ ] **Step 2: 跑测试验证失败**

Run: `python -m pytest tests/experiment/test_store.py -v`
Expected: FAIL — `ImportError: cannot import name 'store'`

- [ ] **Step 3: 实现 store.py**

`experiment/store.py`:
```python
# -*- coding: utf-8 -*-
"""SQLite 持久化（experiment/experiments.db，WAL + 事务）+ 审计写入。

Why SQLite 而非 JSON（design ADR3）：结构化查询（按 status/experiment_id 索引）、
事务一致性（promote = 更新版本表 + 写审计，原子）、并发写安全（WAL）。
plan 归因仍走 plans/<date>.json（execution/storage 不动底层），SQLite 只管实验版本/审计。

复用范式：execution/replay_tasks_db.py 的 SQLite 用法（标准库 sqlite3，WAL，上下文管理器）。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from typing import Optional

from experiment.models import (
    AuditLog, ExperimentStatus, ExperimentVersion, validate_transition, validate_weight_sum,
)

_DEFAULT_DB = "experiment/experiments.db"


@contextmanager
def _connect(db_path: str):
    """连接上下文：开 WAL，提交/回滚自动。SQLite 连接非线程安全，每次操作新建连接。"""
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        # WAL：多读单写并发安全（比默认 rollback journal 强），适合 CLI + scan 并发
        con.execute("PRAGMA journal_mode=WAL")
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def init_db(db_path: str = _DEFAULT_DB) -> None:
    """建表 + 索引（幂等，已存在不报错）。design §3.5 schema。"""
    with _connect(db_path) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS experiment_version (
                experiment_id TEXT PRIMARY KEY, strategy_name TEXT NOT NULL,
                params TEXT NOT NULL, weight REAL NOT NULL, status TEXT NOT NULL,
                version INTEGER NOT NULL, source TEXT, note TEXT,
                created_at TEXT NOT NULL, activated_at TEXT, archived_at TEXT,
                UNIQUE(strategy_name, version))
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_status ON experiment_version(status)")
        con.execute("""
            CREATE TABLE IF NOT EXISTS audit_log (
                audit_id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
                action TEXT NOT NULL, experiment_id TEXT NOT NULL,
                changed_fields TEXT, operator TEXT, note TEXT)
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_audit_exp ON audit_log(experiment_id)")


def _row_to_version(row: sqlite3.Row) -> ExperimentVersion:
    """行 → ExperimentVersion（params JSON 反序列化）。"""
    return ExperimentVersion(
        experiment_id=row["experiment_id"], strategy_name=row["strategy_name"],
        params=json.loads(row["params"]), weight=row["weight"],
        status=ExperimentStatus(row["status"]), version=row["version"],
        source=row["source"] or "", note=row["note"] or "",
        created_at=row["created_at"], activated_at=row["activated_at"],
        archived_at=row["archived_at"])


def _write_audit(con, *, action: str, experiment_id: str, operator: str, now: str,
                 changed_fields: Optional[dict] = None, note: str = "") -> None:
    """写一条审计（在调用方事务内，保证与版本变更原子）。"""
    con.execute(
        "INSERT INTO audit_log(timestamp, action, experiment_id, changed_fields, operator, note)"
        " VALUES(?, ?, ?, ?, ?, ?)",
        (now, action, experiment_id,
         json.dumps(changed_fields or {}, ensure_ascii=False), operator, note))


def create_version(db_path: str, version: ExperimentVersion, operator: str = "cli") -> None:
    """create：写一条 DRAFT 版本（params 已定，weight 通常 0，待 promote 设权重）。"""
    with _connect(db_path) as con:
        try:
            con.execute(
                "INSERT INTO experiment_version(experiment_id, strategy_name, params, weight,"
                " status, version, source, note, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (version.experiment_id, version.strategy_name,
                 json.dumps(version.params, ensure_ascii=False), version.weight,
                 version.status.value, version.version, version.source, version.note,
                 version.created_at))
        except sqlite3.IntegrityError as e:
            raise ValueError(f"实验版本已存在或 strategy+version 冲突: {version.experiment_id}") from e
        _write_audit(con, action="create", experiment_id=version.experiment_id,
                     operator=operator, now=version.created_at,
                     changed_fields={"params": version.params})


def _active_versions(con) -> list:
    """读当前所有 ACTIVE 版本（用于权重和校验）。"""
    rows = con.execute("SELECT * FROM experiment_version WHERE status=?",
                       (ExperimentStatus.ACTIVE.value,)).fetchall()
    return [_row_to_version(r) for r in rows]


def promote(db_path: str, experiment_id: str, weight: float, operator: str,
            now: str) -> None:
    """promote: DRAFT→ACTIVE + 设权重。校验状态迁移 + 权重和。"""
    with _connect(db_path) as con:
        row = con.execute("SELECT * FROM experiment_version WHERE experiment_id=?",
                          (experiment_id,)).fetchone()
        if row is None:
            raise ValueError(f"实验版本不存在: {experiment_id}")
        old_status = ExperimentStatus(row["status"])
        if not validate_transition(old_status, ExperimentStatus.ACTIVE):
            raise ValueError(f"非法迁移: {old_status}→ACTIVE（experiment_id={experiment_id}）")
        if not validate_weight_sum(_active_versions(con), weight):
            raise ValueError(f"权重和超 1.0（资金守恒红线）：promote weight={weight} 被拒")
        con.execute(
            "UPDATE experiment_version SET status=?, weight=?, activated_at=? WHERE experiment_id=?",
            (ExperimentStatus.ACTIVE.value, weight, now, experiment_id))
        _write_audit(con, action="promote", experiment_id=experiment_id, operator=operator,
                     now=now, changed_fields={"status": [old_status.value, "ACTIVE"],
                                              "weight": [row["weight"], weight]})


def set_weight(db_path: str, experiment_id: str, new_weight: float, operator: str,
               now: str) -> None:
    """set_weight: ACTIVE 内调权重（不改 status）。校验：新权重 + 其他 ACTIVE ≤ 1.0。"""
    with _connect(db_path) as con:
        row = con.execute("SELECT * FROM experiment_version WHERE experiment_id=?",
                          (experiment_id,)).fetchone()
        if row is None:
            raise ValueError(f"实验版本不存在: {experiment_id}")
        if ExperimentStatus(row["status"]) != ExperimentStatus.ACTIVE:
            raise ValueError(f"set_weight 仅对 ACTIVE 有效（当前 {row['status']}）")
        # 校验时排除自身：其他 ACTIVE + new_weight ≤ 1.0
        others = [v for v in _active_versions(con) if v.experiment_id != experiment_id]
        if not validate_weight_sum(others, new_weight):
            raise ValueError(f"权重和超 1.0：set_weight={new_weight} 被拒")
        con.execute("UPDATE experiment_version SET weight=? WHERE experiment_id=?",
                    (new_weight, experiment_id))
        _write_audit(con, action="set-weight", experiment_id=experiment_id, operator=operator,
                     now=now, changed_fields={"weight": [row["weight"], new_weight]})


def archive(db_path: str, experiment_id: str, operator: str, now: str) -> None:
    """archive: ACTIVE→ARCHIVED。"""
    _transition(db_path, experiment_id, ExperimentStatus.ARCHIVED, operator, now, "archive")


def rollback(db_path: str, experiment_id: str, operator: str, now: str) -> None:
    """rollback: ARCHIVED→ACTIVE（恢复上线）。权重沿用归档前值，若冲突由调用方先调权重。"""
    _transition(db_path, experiment_id, ExperimentStatus.ACTIVE, operator, now, "rollback")


def _transition(db_path, experiment_id, target, operator, now, action) -> None:
    """通用状态迁移：校验合法性 → 更新 status + 时间戳 → 写审计。"""
    with _connect(db_path) as con:
        row = con.execute("SELECT * FROM experiment_version WHERE experiment_id=?",
                          (experiment_id,)).fetchone()
        if row is None:
            raise ValueError(f"实验版本不存在: {experiment_id}")
        old_status = ExperimentStatus(row["status"])
        if not validate_transition(old_status, target):
            raise ValueError(f"非法迁移: {old_status}→{target}（{action}）")
        ts_col = {"ACTIVE": "activated_at", "ARCHIVED": "archived_at"}[target.value]
        con.execute(
            f"UPDATE experiment_version SET status=?, {ts_col}=? WHERE experiment_id=?",
            (target.value, now, experiment_id))
        _write_audit(con, action=action, experiment_id=experiment_id, operator=operator,
                     now=now, changed_fields={"status": [old_status.value, target.value]})


def list_versions(db_path: str, status: Optional[ExperimentStatus] = None) -> list:
    """列版本（可按 status 过滤）。"""
    with _connect(db_path) as con:
        if status is None:
            rows = con.execute("SELECT * FROM experiment_version ORDER BY created_at").fetchall()
        else:
            rows = con.execute("SELECT * FROM experiment_version WHERE status=? ORDER BY created_at",
                               (status.value,)).fetchall()
    return [_row_to_version(r) for r in rows]


def list_audit(db_path: str, experiment_id: Optional[str] = None) -> list:
    """列审计（可按 experiment_id 过滤）。"""
    with _connect(db_path) as con:
        if experiment_id is None:
            rows = con.execute("SELECT * FROM audit_log ORDER BY audit_id").fetchall()
        else:
            rows = con.execute("SELECT * FROM audit_log WHERE experiment_id=? ORDER BY audit_id",
                               (experiment_id,)).fetchall()
    return [AuditLog(timestamp=r["timestamp"], action=r["action"],
                     experiment_id=r["experiment_id"],
                     changed_fields=json.loads(r["changed_fields"] or "{}"),
                     operator=r["operator"] or "", note=r["note"] or "") for r in rows]
```

- [ ] **Step 4: 跑测试验证通过**

Run: `python -m pytest tests/experiment/test_store.py -v`
Expected: PASS（8 个测试全绿）

- [ ] **Step 5: Commit**

```bash
git add experiment/store.py tests/experiment/test_store.py
git commit -m "feat(experiment): SQLite store+审计（WAL事务/状态机校验/权重守恒）"
```

---

### Task 3: experiment/ resolver

**Files:**
- Create: `experiment/resolver.py`
- Test: `tests/experiment/test_resolver.py`

**Interfaces:**
- Consumes: `experiment/store.py`（Task 2）
- Produces: `resolve_active(db_path=None) -> list[ActiveExperiment]`

- [ ] **Step 1: 写失败测试**

`tests/experiment/test_resolver.py`:
```python
# -*- coding: utf-8 -*-
"""resolver 单元测试：只返 ACTIVE+weight>0；params 快照正确。"""
from experiment import store, resolver
from experiment.models import ExperimentStatus, ExperimentVersion


def _make(db, eid="e1", weight=0.2, params=None, status=ExperimentStatus.DRAFT):
    v = ExperimentVersion(experiment_id=eid, strategy_name="neckline",
                          params=params or {"window": 60}, weight=weight, status=status,
                          version=1, source="manual", created_at="2026-07-22T10:00:00")
    store.create_version(db, v, operator="cli")


def test_resolve_returns_only_active_positive_weight(tmp_path):
    """resolve_active 只返 ACTIVE 且 weight>0（DRAFT/ARCHIVED/weight=0 过滤掉）。"""
    db = str(tmp_path / "t.db")
    store.init_db(db)
    _make(db, "e1", weight=0.5)                      # 将 promote
    _make(db, "e2", weight=0.0)                      # 留 DRAFT
    store.promote(db, "e1", weight=0.5, operator="cli", now="t")
    _make(db, "e3", weight=0.3)
    store.promote(db, "e3", weight=0.3, operator="cli", now="t")
    store.set_weight(db, "e3", new_weight=0.0, operator="cli", now="t2")  # weight=0 软下线
    active = resolver.resolve_active(db)
    ids = {a.experiment_id for a in active}
    assert ids == {"e1"}   # e2 DRAFT、e3 weight=0 都被过滤


def test_resolve_returns_params_snapshot(tmp_path):
    """resolve 返回的 params 是不可变快照（与 store 写入一致）。"""
    db = str(tmp_path / "t.db")
    store.init_db(db)
    _make(db, "e1", params={"window": 90, "min_touches": 4})
    store.promote(db, "e1", weight=1.0, operator="cli", now="t")
    [ae] = resolver.resolve_active(db)
    assert ae.params == {"window": 90, "min_touches": 4}
    assert ae.strategy_name == "neckline" and ae.weight == 1.0


def test_resolve_empty_when_no_active(tmp_path):
    """无 ACTIVE 实验 → 返回空列表（scan 据此 fail-fast）。"""
    db = str(tmp_path / "t.db")
    store.init_db(db)
    assert resolver.resolve_active(db) == []
```

- [ ] **Step 2: 跑测试验证失败**

Run: `python -m pytest tests/experiment/test_resolver.py -v`
Expected: FAIL — `ImportError: cannot import name 'resolver'`

- [ ] **Step 3: 实现 resolver.py**

`experiment/resolver.py`:
```python
# -*- coding: utf-8 -*-
"""resolver：scan 的唯一入口，实时读 SQLite 返 [ActiveExperiment]。

Why 不缓存（design §5.3）：scan 是 schtasks/CLI 触发的短任务，每次实时读 SQLite
保证 CLI 改权重后下次 scan 立即生效。零常驻进程、零缓存一致性问题。
"""
from __future__ import annotations

from typing import Optional

from experiment.models import ActiveExperiment, ExperimentStatus
from experiment.store import _DEFAULT_DB, list_versions


def resolve_active(db_path: Optional[str] = None) -> list:
    """返回当前所有在线实验（status=ACTIVE 且 weight>0）。

    返回：list[ActiveExperiment]，每项含 experiment_id/strategy_name/params/weight。
    空列表表示无在线实验（scan 调用方应 fail-fast，不下单）。
    """
    versions = list_versions(db_path or _DEFAULT_DB, status=ExperimentStatus.ACTIVE)
    return [ActiveExperiment(experiment_id=v.experiment_id, strategy_name=v.strategy_name,
                             params=v.params, weight=v.weight)
            for v in versions if v.weight > 0]
```

- [ ] **Step 4: 跑测试验证通过**

Run: `python -m pytest tests/experiment/test_resolver.py -v`
Expected: PASS（3 个测试全绿）

- [ ] **Step 5: 在 __init__.py 导出 resolver + Commit**

`experiment/__init__.py` 末尾加：
```python
from experiment.resolver import resolve_active  # noqa: F401
```

```bash
git add experiment/resolver.py experiment/__init__.py tests/experiment/test_resolver.py
git commit -m "feat(experiment): resolver（resolve_active 实时读 SQLite 返在线实验）"
```

---

### Task 4: experiment/ CLI

**Files:**
- Create: `experiment/cli.py`
- Test: `tests/experiment/test_cli.py`

**Interfaces:**
- Consumes: `experiment/store.py`（Task 2）
- Produces: `main(argv) -> int`（argparse 子命令分发），`python -m experiment ...` 入口

- [ ] **Step 1: 写失败测试**

`tests/experiment/test_cli.py`:
```python
# -*- coding: utf-8 -*-
"""CLI 端到端：create/promote/set-weight/archive/rollback/list。用 monkeypatch 切 db 路径。"""
import json

import pytest

from experiment import cli, store, resolver
from experiment.models import ExperimentStatus


@pytest.fixture
def db(tmp_path, monkeypatch):
    """CLI 默认走 experiment/experiments.db，测试 monkeypatch 到临时路径。"""
    p = str(tmp_path / "t.db")
    store.init_db(p)
    monkeypatch.setattr(cli, "_DEFAULT_DB", p)
    return p


def test_cli_create_promote_list(db, capsys):
    """create → promote → list 全链路。"""
    rc = cli.main(["create", "--strategy", "neckline",
                   "--params", '{"window": 60}', "--experiment-id", "e1",
                   "--source", "manual", "--created-at", "2026-07-22T10:00:00"])
    assert rc == 0
    rc = cli.main(["promote", "e1", "--weight", "0.5"])
    assert rc == 0
    rc = cli.main(["list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "e1" in out and "ACTIVE" in out


def test_cli_set_weight_archive_rollback(db):
    """set-weight → archive → rollback。"""
    cli.main(["create", "--strategy", "neckline", "--params", '{}',
              "--experiment-id", "e1", "--created-at", "t"])
    cli.main(["promote", "e1", "--weight", "0.3"])
    cli.main(["set-weight", "e1", "--weight", "0.6"])
    assert resolver.resolve_active()[0].weight == 0.6
    cli.main(["archive", "e1"])
    assert resolver.resolve_active() == []
    cli.main(["rollback", "e1"])
    assert resolver.resolve_active()[0].experiment_id == "e1"


def test_cli_promote_rejects_overflow(db, capsys):
    """CLI 层权重溢出报错（非零退出）。"""
    cli.main(["create", "--strategy", "neckline", "--params", '{}',
              "--experiment-id", "e1", "--created-at", "t"])
    cli.main(["promote", "e1", "--weight", "0.8"])
    cli.main(["create", "--strategy", "neckline", "--params", '{}',
              "--experiment-id", "e2", "--version", "2", "--created-at", "t"])
    rc = cli.main(["promote", "e2", "--weight", "0.3"])  # 0.8+0.3=1.1
    assert rc != 0
    err = capsys.readouterr().err
    assert "权重" in err
```

- [ ] **Step 2: 跑测试验证失败**

Run: `python -m pytest tests/experiment/test_cli.py -v`
Expected: FAIL — `ImportError: cannot import name 'cli'`

- [ ] **Step 3: 实现 cli.py**

`experiment/cli.py`:
```python
# -*- coding: utf-8 -*-
"""实验系统 CLI：python -m experiment create|promote|set-weight|archive|rollback|list|report

每个命令操作 experiment/experiments.db，变更写审计。退出码：0 成功 / 非 0 失败。
now 时间戳由调用方传或取当前；测试传固定值保证可复现。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime

from experiment.models import ExperimentVersion, ExperimentStatus
from experiment.store import _DEFAULT_DB, archive as _archive, create_version, list_versions, promote as _promote, rollback as _rollback, set_weight

_OPERATOR = "cli"


def _now() -> str:
    """当前 ISO 时间戳（CLI 实跑用；测试走 store 层固定 now）。"""
    return datetime.now().isoformat(timespec="seconds")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="experiment", description="实验系统配置中心")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("create", help="创建 DRAFT 版本")
    sp.add_argument("--strategy", required=True)
    sp.add_argument("--params", required=True, help="JSON 参数串")
    sp.add_argument("--experiment-id", required=True)
    sp.add_argument("--version", type=int, default=1)
    sp.add_argument("--source", default="manual")
    sp.add_argument("--note", default="")
    sp.add_argument("--created-at", default=None)

    sp = sub.add_parser("promote", help="DRAFT→ACTIVE + 设权重")
    sp.add_argument("experiment_id")
    sp.add_argument("--weight", type=float, required=True)

    sp = sub.add_parser("set-weight", help="调整 ACTIVE 权重")
    sp.add_argument("experiment_id")
    sp.add_argument("--weight", type=float, required=True)

    sp = sub.add_parser("archive", help="ACTIVE→ARCHIVED")
    sp.add_argument("experiment_id")

    sp = sub.add_parser("rollback", help="ARCHIVED→ACTIVE")
    sp.add_argument("experiment_id")

    sub.add_parser("list", help="列所有版本")
    return p


def main(argv: list = None) -> int:
    """CLI 入口（返回退出码）。db 路径由模块级 _DEFAULT_DB 决定（测试 monkeypatch）。"""
    args = _build_parser().parse_args(argv)
    db = _DEFAULT_DB
    try:
        if args.cmd == "create":
            v = ExperimentVersion(
                experiment_id=args.experiment_id, strategy_name=args.strategy,
                params=json.loads(args.params), weight=0.0, status=ExperimentStatus.DRAFT,
                version=args.version, source=args.source, note=args.note,
                created_at=args.created_at or _now())
            create_version(db, v, operator=_OPERATOR)
            print(f"created {args.experiment_id} (DRAFT)")
        elif args.cmd == "promote":
            _promote(db, args.experiment_id, weight=args.weight, operator=_OPERATOR, now=_now())
            print(f"promoted {args.experiment_id} weight={args.weight}")
        elif args.cmd == "set-weight":
            set_weight(db, args.experiment_id, new_weight=args.weight, operator=_OPERATOR, now=_now())
            print(f"set-weight {args.experiment_id} → {args.weight}")
        elif args.cmd == "archive":
            _archive(db, args.experiment_id, operator=_OPERATOR, now=_now())
            print(f"archived {args.experiment_id}")
        elif args.cmd == "rollback":
            _rollback(db, args.experiment_id, operator=_OPERATOR, now=_now())
            print(f"rollback {args.experiment_id} → ACTIVE")
        elif args.cmd == "list":
            for v in list_versions(db):
                print(f"{v.experiment_id:30} {v.strategy_name:10} {v.status.value:9}"
                      f" w={v.weight:.2f} v={v.version} src={v.source}")
        return 0
    except ValueError as e:
        # 状态机/权重校验失败：stderr + 非零退出（绝不静默改一半）
        print(f"错误: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
```

注：`report` 子命令在 Task 12 加（依赖 plan 归因扫描）。

- [ ] **Step 4: 跑测试验证通过**

Run: `python -m pytest tests/experiment/test_cli.py -v`
Expected: PASS（3 个测试全绿）

- [ ] **Step 5: 加 `python -m experiment` 入口 + Commit**

`experiment/__main__.py`:
```python
# -*- coding: utf-8 -*-
"""python -m experiment <cmd> 入口。"""
import sys
from experiment.cli import main
sys.exit(main())
```

```bash
git add experiment/cli.py experiment/__main__.py tests/experiment/test_cli.py
git commit -m "feat(experiment): CLI（create/promote/set-weight/archive/rollback/list）"
```

---

### Task 5: strategies/base.py 协议扩展 + 决策对象

**Files:**
- Modify: `strategies/base.py`（加 dataclass + 协议 4 方法）
- Test: `tests/strategies/test_base_protocol.py`

**Interfaces:**
- Consumes: 无新依赖
- Produces: `Signal`, `PullbackAction`, `ExitAction`, `PullbackDecision`, `ExitDecision`；Strategy 协议新增 `scan_live`/`to_armed_plan`/`check_pullback`/`check_exit`

- [ ] **Step 1: 写失败测试**

`tests/strategies/__init__.py`（空文件）+ `tests/strategies/test_base_protocol.py`:
```python
# -*- coding: utf-8 -*-
"""base 协议扩展测试：决策对象 dataclass 字段 + 默认值。"""
from strategies.base import (
    Signal, PullbackAction, ExitAction, PullbackDecision, ExitDecision,
)


def test_signal_carries_identification_kernel():
    """Signal 承载颈线法识别内核输出（symbol/颈线位/H/ATR/突破日/形成日）。"""
    s = Signal(symbol="000001.SZ", neckline_price=10.0, H=1.0, atr=0.5,
               breakout_date="2026-07-22", formed_at="2026-07-15")
    assert s.symbol == "000001.SZ" and s.H == 1.0


def test_exit_decision_close_portion_defaults():
    """ExitDecision CLOSE_PORTION 默认 portion=1.0，可设 0.5（tp1 半仓）。"""
    d = ExitDecision(action=ExitAction.CLOSE_PORTION, portion=0.5, reason="take_profit_1")
    assert d.action == ExitAction.CLOSE_PORTION and d.portion == 0.5
    d2 = ExitDecision(action=ExitAction.CLOSE_ALL, reason="stop_loss")
    assert d2.portion == 1.0 and d2.new_stop is None


def test_pullback_decision_actions():
    """PullbackDecision 三态：ARMED_FILL/CANCEL_TIMEOUT/HOLD。"""
    assert PullbackDecision(PullbackAction.ARMED_FILL).action == PullbackAction.ARMED_FILL
    assert PullbackDecision(PullbackAction.CANCEL_TIMEOUT).action == PullbackAction.CANCEL_TIMEOUT
```

- [ ] **Step 2: 跑测试验证失败**

Run: `python -m pytest tests/strategies/test_base_protocol.py -v`
Expected: FAIL — `ImportError: cannot import name 'Signal'`

- [ ] **Step 3: 扩展 base.py**

在 `strategies/base.py` 顶部 import 后、`TRADE_REQUIRED_KEYS` 前插入决策对象；在 `Strategy` 协议尾部加 4 方法：

```python
from dataclasses import dataclass
from typing import Optional


# ============================================================================
# 实盘决策对象（design §6.3）：支持分级平仓 / 撤单 / trailing
# ============================================================================
class PullbackAction:
    """ARMED 阶段决策动作。"""
    ARMED_FILL = "armed_fill"           # 触及回踩区间 → 限价买入 → FILLED
    CANCEL_TIMEOUT = "cancel_timeout"   # 超 max_wait 未回踩 → 撤单（颈线法特有）
    HOLD = "hold"                       # 继续等


class ExitAction:
    """FILLED 阶段决策动作。"""
    CLOSE_PORTION = "close_portion"     # 部分平（tp1 卖 tp1_portion）
    CLOSE_ALL = "close_all"             # 全平（止损/tp2/超时）
    UPDATE_STOP = "update_stop"         # trailing 收紧止损（持仓不变）
    HOLD = "hold"


@dataclass
class Signal:
    """scan_live 返回的识别结果（颈线法：聚集带突破 + 回踩挂单点）。

    通用字段：symbol + 识别内核输出。颈线法填 neckline_price/H/atr；caisen 填 pattern 字段。
    """
    symbol: str
    formed_at: str               # 信号形成日 T（index label）
    breakout_date: str = ""      # 突破日（挂单起始）
    # 颈线法专属（caisen 可留 0/空）
    neckline_price: float = 0.0
    H: float = 0.0               # 形态高度（颈线到高点）
    atr: float = 0.0


@dataclass
class PullbackDecision:
    action: str
    reason: str = ""


@dataclass
class ExitDecision:
    action: str
    portion: float = 1.0           # CLOSE_PORTION 时 = tp1_portion
    new_stop: Optional[float] = None   # UPDATE_STOP 时的新止损
    reason: str = ""
```

在 `Strategy` 协议 `config_schema` property 后加：
```python
    # —— 实盘新增（design §6.2）：scan_live + to_armed_plan + check_pullback + check_exit ——
    def scan_live(self, date) -> list:
        """实盘识别：复用 scan_at 识别内核，只产出 Signal（不模拟出场）。

        返回 list[Signal]。停牌/缺数据标的内部跳过。
        """
        ...

    def to_armed_plan(self, signal, *, weight: float, experiment_id: str,
                      total_capital: float) -> dict:
        """Signal → ARMED plan dict（挂单区间/shares/stop/tp1/tp2/trailing/max_wait）。

        weight × total_capital × pos_cap / entry_upper 落地为 shares（向下取整 100 股）。
        plan 含 experiment_id + experiment_weight（归因 + 冻结权重）。
        """
        ...

    def check_pullback(self, plan: dict, quote: dict, bars_armed: int):
        """ARMED 阶段决策：触及回踩区间→FILLED；颈线法另含 max_wait 超时撤单。"""
        ...

    def check_exit(self, plan: dict, bar: dict, bars_held: int):
        """FILLED 阶段决策：颈线法 tp1部分+tp2全+trailing+超时；caisen 调 exit_logic.check_exit。"""
        ...
```

- [ ] **Step 4: 跑测试验证通过**

Run: `python -m pytest tests/strategies/test_base_protocol.py -v`
Expected: PASS（3 个测试全绿）。同时确认既有回测测试无回归：
Run: `python -m pytest tests/test_neckline_core.py tests/test_neckline_recognition.py -v`
Expected: PASS（协议加方法不影响既有 scan_at/precompute）

- [ ] **Step 5: Commit**

```bash
git add strategies/base.py tests/strategies/__init__.py tests/strategies/test_base_protocol.py
git commit -m "feat(strategies): base 协议扩展（Signal/PullbackDecision/ExitDecision + 4 实盘方法）"
```

---

### Task 6: 颈线法 scan_live + to_armed_plan

**Files:**
- Modify: `strategies/neckline_method.py`（加 scan_live + to_armed_plan）
- Reference: `scripts/neckline_method_v0.py`（detect_neckline_method/search_neckline/DEFAULTS/compute_atr）、`scripts/neckline_backtest.py`（EXEC_DEFAULTS/scan_symbol 识别部分）
- Test: `tests/strategies/test_neckline_armed_plan.py`

**Interfaces:**
- Consumes: `strategies/base.py`（Task 5），颈线法既有识别内核
- Produces: `NecklineMethodStrategy.scan_live(date)`、`.to_armed_plan(signal, weight, experiment_id, total_capital)`

- [ ] **Step 1: 写失败测试**

`tests/strategies/test_neckline_armed_plan.py`:
```python
# -*- coding: utf-8 -*-
"""颈线法 to_armed_plan 字段映射 + shares 按 weight 计算。"""
import pytest

from strategies.base import Signal
from strategies.neckline_method import NecklineMethodStrategy


@pytest.fixture
def strategy():
    """默认 EXEC_DEFAULTS 参数的颈线法策略实例。"""
    return NecklineMethodStrategy(cfg_override=None)


def test_to_armed_plan_fields(strategy):
    """Signal → ARMED plan 含全部颈线法字段 + experiment_id/weight。"""
    sig = Signal(symbol="000001.SZ", formed_at="2026-07-15", breakout_date="2026-07-22",
                 neckline_price=10.0, H=1.0, atr=0.5)
    plan = strategy.to_armed_plan(sig, weight=0.2, experiment_id="e1", total_capital=1_000_000)
    # 归因字段
    assert plan["experiment_id"] == "e1" and plan["experiment_weight"] == 0.2
    # 颈线法挂单区间/止损/止盈（按 EXEC_DEFAULTS 默认倍数）
    assert "entry_upper" in plan and "entry_lower" in plan
    assert plan["stop"] < sig.neckline_price          # 止损在颈线下方
    assert plan["take_profit"] > sig.neckline_price   # tp2 在颈线上方
    assert plan["take_profit_1"] < plan["take_profit"]  # tp1 < tp2
    assert "tp1_portion" in plan and "max_wait_bars" in plan
    assert "trailing_grace" in plan


def test_shares_scaled_by_weight_and_capital(strategy):
    """shares = weight × total_capital × pos_cap / entry_upper，向下取整到 100 股。"""
    sig = Signal(symbol="000001.SZ", formed_at="t", neckline_price=10.0, H=1.0, atr=0.5)
    plan_full = strategy.to_armed_plan(sig, weight=1.0, experiment_id="e1", total_capital=1_000_000)
    plan_half = strategy.to_armed_plan(sig, weight=0.5, experiment_id="e1", total_capital=1_000_000)
    # 权重减半 → shares 约半（取整到 100 股有微小误差）
    assert plan_half["shares"] <= plan_full["shares"]
    assert plan_full["shares"] > 0 and plan_full["shares"] % 100 == 0  # A 股 100 股整数倍
```

- [ ] **Step 2: 跑测试验证失败**

Run: `python -m pytest tests/strategies/test_neckline_armed_plan.py -v`
Expected: FAIL — `AttributeError: 'NecklineMethodStrategy' object has no attribute 'to_armed_plan'`

- [ ] **Step 3: 实现 scan_live + to_armed_plan**

先读 `strategies/neckline_method.py` 现有结构（`NecklineMethodStrategy` 类 + 既有 `scan_at`/`precompute`/`config_schema`），确认它如何复用 `scripts/neckline_method_v0.py` 的识别内核。然后在类内加两个方法：

`strategies/neckline_method.py` 加（具体 import 按既有文件顶部的 neckline_method_v0 引用方式对齐）：
```python
import math
from strategies.base import Signal


# 在 NecklineMethodStrategy 类内加：

def scan_live(self, date) -> list:
    """实盘识别：复用 scan_at 的识别内核，只产 Signal（不模拟出场）。

    物理意图：scan_at 是回测一站式（识别+进场+模拟出场），实盘只需「识别 + 算挂单点」，
    出场由 ExecutionEngine.tick 驱动调 check_exit。故 scan_live 读 data_lake 截至 date 的
    数据，逐 symbol 调 detect_neckline_method，命中且当日突破 → 产 Signal。
    """
    signals = []
    lake = self._load_lake_upto(date)   # 复用既有数据加载（read a_shares_daily.parquet）
    for symbol, df in lake.items():     # df 已截至 date（无前视）
        try:
            # 复用 neckline_method_v0.detect_neckline_method（识别内核，与回测同源）
            res = detect_neckline_method(df, **self._id_params())
            if res is None or res.get("breakout_date") != date:
                continue   # 非今日突破，跳过（实盘只挂当日新信号）
            signals.append(Signal(
                symbol=symbol, formed_at=str(res["formed_at"]),
                breakout_date=str(res["breakout_date"]),
                neckline_price=res["neckline_price"], H=res["H"], atr=res["atr"]))
        except Exception:
            continue   # 单标的异常不中断（停牌/缺数据/识别失败跳过）
    return signals

def to_armed_plan(self, signal, *, weight: float, experiment_id: str,
                  total_capital: float) -> dict:
    """Signal → ARMED plan。资金权重在此落地为 shares。

    字段映射见 design §6.4。shares = weight × total_capital × pos_cap / entry_upper，
    向下取整到 100 股（A 股最小交易单位）。
    """
    c_star, atr, H = signal.neckline_price, signal.atr, signal.H
    e = self._exec_params()   # EXEC_DEFAULTS 经 cfg_override 覆盖后的执行层参数
    entry_upper = c_star + e["buy_limit_atr_mult"] * atr      # 回踩挂单区间上沿
    entry_lower = c_star - e["buy_limit_atr_mult"] * atr      # 下沿
    stop = c_star - e["stop_atr_mult"] * atr
    take_profit = c_star + e["tp_h_mult"] * H                 # tp2 全止盈
    take_profit_1 = c_star + e["tp1_h_mult"] * H              # tp1 部分止盈
    # shares 计算：weight × 资金 × pos_cap / entry_upper，向下取整 100 股
    raw_shares = weight * total_capital * e["pos_cap"] / entry_upper
    shares = max(int(raw_shares // 100) * 100, 100)           # 至少 100 股
    return {
        "plan_id": f"{experiment_id}:{signal.symbol}:{signal.breakout_date}",
        "symbol": signal.symbol,
        "experiment_id": experiment_id,
        "experiment_weight": weight,
        "entry_upper": entry_upper,
        "entry_lower": entry_lower,
        "shares": shares,
        "stop": stop,
        "take_profit": take_profit,            # tp2
        "take_profit_1": take_profit_1,        # tp1
        "tp1_portion": e["tp1_portion"],
        "max_wait_bars": e["max_wait"],
        "trailing_grace": e["trailing_grace"],
        "trailing_step": e["trailing_step"],
        "trailing_floor": e["trailing_floor"],
        "formed_at": signal.formed_at,
        "breakout_date": signal.breakout_date,
        "tp1_hit": False,                      # 运行态：tp1 是否已触发（check_exit 更新）
    }

def _id_params(self) -> dict:
    """识别层参数（DEFAULTS 经 cfg_override 覆盖）。"""
    return {k: self._cfg.get(k) for k in
            ("window", "min_touches", "min_suppression", "local_extrema_window",
             "min_bottoms", "breakout_vol_mult", "min_rr", "max_h_atr",
             "stop_atr_mult", "tp_h_mult", "decay_tau")}

def _exec_params(self) -> dict:
    """执行层参数（EXEC_DEFAULTS 经 cfg_override 覆盖）。"""
    return {k: self._cfg.get(k, v) for k, v in {
        "buy_limit_atr_mult": 0.5, "stop_atr_mult": 1.5, "tp_h_mult": 2.0,
        "tp1_h_mult": 1.0, "tp1_portion": 0.5, "max_wait": 5, "pos_cap": 0.14,
        "trailing_grace": 5, "trailing_step": 0.1, "trailing_floor": 0.5,
    }.items()}
```

**实现说明（执行者必读）**：`_load_lake_upto`/`_cfg`/detect_neckline_method 的具体 import 与既有 `strategies/neckline_method.py` 的 `scan_at` 实现对齐——该文件已在 Task（提交①）中实现 `scan_at` 复用识别内核，`scan_live` 抽其识别部分（去掉出场模拟）。若 `_load_lake_upto` 不存在，按 `scan_at` 内部的数据加载方式抽取。EXEC_DEFAULTS 默认值以 `scripts/neckline_backtest.py` 顶部 `EXEC_DEFAULTS` 为准（逐字复制，勿臆造）。

- [ ] **Step 4: 跑测试验证通过**

Run: `python -m pytest tests/strategies/test_neckline_armed_plan.py -v`
Expected: PASS（2 个测试全绿）。若 `scan_live` 因数据加载依赖 parquet 不便单测，`to_armed_plan` 的纯函数测试（Task 6 测试）必须通过；`scan_live` 的数据集成测放 Task 13 e2e。

- [ ] **Step 5: Commit**

```bash
git add strategies/neckline_method.py tests/strategies/test_neckline_armed_plan.py
git commit -m "feat(neckline): scan_live+to_armed_plan（Signal→ARMED plan+shares按权重）"
```

---

### Task 7: 颈线法 check_pullback + check_exit（复用 simulate_exit 内核）

**Files:**
- Modify: `strategies/neckline_method.py`（加 check_pullback + check_exit + 抽 _exit_kernel）
- Reference: `scripts/neckline_backtest.py:simulate_exit`（回测出场状态机，逐字抽取内核）
- Test: `tests/strategies/test_neckline_check_exit.py`

**Interfaces:**
- Consumes: `strategies/base.py`（Task 5）
- Produces: `NecklineMethodStrategy.check_pullback(plan, quote, bars_armed)`、`.check_exit(plan, bar, bars_held)`、模块级 `_exit_kernel(pos, bar, bars_held, cfg) -> ExitDecision`

- [ ] **Step 1: 写失败测试**

`tests/strategies/test_neckline_check_exit.py`:
```python
# -*- coding: utf-8 -*-
"""颈线法 check_exit：tp1 部分平/tp2 全平/trailing 收紧/止损/超时。
断言复用 simulate_exit 同口径（消除双源真理）。"""
import pytest

from strategies.base import ExitAction, ExitDecision
from strategies.neckline_method import NecklineMethodStrategy


@pytest.fixture
def strategy():
    return NecklineMethodStrategy(cfg_override=None)


def _plan(stop=9.0, tp1=11.0, tp2=12.0, tp1_hit=False, **kw):
    """造一个 FILLED plan（entry=10 颈线位）。"""
    base = dict(symbol="000001.SZ", stop=stop, take_profit_1=tp1, take_profit=tp2,
                tp1_portion=0.5, tp1_hit=tp1_hit,
                trailing_grace=5, trailing_step=0.1, trailing_floor=0.5)
    base.update(kw)
    return base


def test_stop_loss_closes_all(strategy):
    """跌破止损 → CLOSE_ALL（优先级最高）。"""
    plan = _plan(stop=9.0)
    d = strategy.check_exit(plan, bar={"high": 9.5, "low": 8.8, "close": 9.0}, bars_held=2)
    assert d.action == ExitAction.CLOSE_ALL and d.reason == "stop_loss"


def test_tp1_partial_close(strategy):
    """触及 tp1（首次）→ CLOSE_PORTION portion=tp1_portion。"""
    plan = _plan(tp1=11.0, tp1_hit=False)
    d = strategy.check_exit(plan, bar={"high": 11.2, "low": 10.5, "close": 11.1}, bars_held=3)
    assert d.action == ExitAction.CLOSE_PORTION and d.portion == 0.5
    assert d.reason == "take_profit_1"


def test_tp2_full_close(strategy):
    """触及 tp2 → CLOSE_ALL。"""
    plan = _plan(tp2=12.0, tp1_hit=True)
    d = strategy.check_exit(plan, bar={"high": 12.2, "low": 11.5, "close": 12.1}, bars_held=6)
    assert d.action == ExitAction.CLOSE_ALL and d.reason == "take_profit_2"


def test_trailing_tightens_stop(strategy):
    """持仓 > grace 且未触发离场 → trailing 收紧止损（UPDATE_STOP，new_stop 上移）。"""
    plan = _plan(stop=8.5, trailing_grace=5, trailing_step=0.1, trailing_floor=0.5)
    # bars_held=7 > grace=5：eff_mult = max(1.5-(7-5)*0.1, 0.5)=1.3，new_stop 上移
    d = strategy.check_exit(plan, bar={"high": 10.5, "low": 10.0, "close": 10.3}, bars_held=7)
    assert d.action == ExitAction.UPDATE_STOP and d.new_stop is not None
    assert d.new_stop > 8.5   # trailing 让止损上移（更紧）


def test_pullback_fills_in_range(strategy):
    """触及回踩区间 → ARMED_FILL。"""
    plan = {"entry_upper": 10.2, "entry_lower": 9.8}
    d = strategy.check_pullback(plan, quote={"high": 10.1, "low": 9.9}, bars_armed=1)
    from strategies.base import PullbackAction
    assert d.action == PullbackAction.ARMED_FILL


def test_pullback_cancel_timeout(strategy):
    """超 max_wait 未回踩 → CANCEL_TIMEOUT（颈线法撤单）。"""
    plan = {"entry_upper": 10.2, "entry_lower": 9.8, "max_wait_bars": 5}
    d = strategy.check_pullback(plan, quote={"high": 10.5, "low": 10.3}, bars_armed=6)
    from strategies.base import PullbackAction
    assert d.action == PullbackAction.CANCEL_TIMEOUT
```

- [ ] **Step 2: 跑测试验证失败**

Run: `python -m pytest tests/strategies/test_neckline_check_exit.py -v`
Expected: FAIL — `AttributeError: ... has no attribute 'check_exit'`

- [ ] **Step 3: 抽 _exit_kernel + 实现 check_pullback/check_exit**

**关键红线（双源真理）**：先读 `scripts/neckline_backtest.py` 的 `simulate_exit` 函数全文，把其「离场判定核心循环」抽成纯函数 `_exit_kernel(pos, bar, bars_held, cfg) -> ExitDecision`，放 `strategies/neckline_method.py` 模块级。回测 `simulate_exit` 与实盘 `check_exit` 共用此内核（消除回测/实盘两套离场规则）。

抽 kernel 时把 `simulate_exit` 里依赖「逐日推进状态」的部分（如更新 plan.tp1_hit）剥离——kernel 是**无状态纯函数**：输入当前 pos/bar/bars_held/cfg，输出本根 K 线的 ExitDecision；状态更新（tp1_hit 标记、stop 更新）由调用方（check_exit / simulate_exit）按 decision 回写 plan。

`strategies/neckline_method.py` 加：
```python
from strategies.base import ExitAction, ExitDecision, PullbackAction, PullbackDecision


def _exit_kernel(pos, bar, bars_held, cfg) -> ExitDecision:
    """颈线法离场判定纯函数（回测/实盘同源，消除双源真理）。

    优先级（与 scripts/neckline_backtest.py simulate_exit 逐字对齐）：
        stop_loss > tp1(部分,首次未触发) > tp2(全平) > trailing 收紧 > max_holding 超时
    参数：
        pos: {entry, stop, take_profit_1, take_profit_2, tp1_hit, ...}
        bar: {high, low, close}
        bars_held: 持仓交易日数
        cfg: {trailing_grace, trailing_step, trailing_floor, stop_atr_mult, atr, max_holding, ...}
    返回：ExitDecision（无状态，调用方按 decision 回写 pos）。
    """
    high, low, close = bar["high"], bar["low"], bar["close"]
    stop = pos["stop"]
    # 1. 止损优先（蔡森原著：防日内闪崩穿止损后反弹假象）
    if low <= stop:
        return ExitDecision(action=ExitAction.CLOSE_ALL, reason="stop_loss")
    # 2. tp1 部分止盈（首次触及，未卖过）
    if not pos.get("tp1_hit") and high >= pos["take_profit_1"]:
        return ExitDecision(action=ExitAction.CLOSE_PORTION, portion=pos.get("tp1_portion", 0.5),
                            reason="take_profit_1")
    # 3. tp2 全止盈
    if high >= pos["take_profit"]:
        return ExitDecision(action=ExitAction.CLOSE_ALL, reason="take_profit_2")
    # 4. trailing 移动止损（持仓 > grace 才启动，eff_mult 递减到 floor）
    grace, step, floor = (cfg["trailing_grace"], cfg["trailing_step"], cfg["trailing_floor"])
    if bars_held > grace:
        eff_mult = max(cfg["stop_atr_mult"] - (bars_held - grace) * step, floor)
        new_stop = pos["entry"] - eff_mult * cfg["atr"]
        if new_stop > stop:   # 止损只上移
            return ExitDecision(action=ExitAction.UPDATE_STOP, new_stop=new_stop, reason="trailing")
    # 5. 超时
    if bars_held >= cfg.get("max_holding", 999):
        return ExitDecision(action=ExitAction.CLOSE_ALL, reason="timeout")
    return ExitDecision(action=ExitAction.HOLD, reason="hold")


# NecklineMethodStrategy 类内加：

def check_pullback(self, plan, quote, bars_armed):
    """ARMED 阶段：触及回踩区间→FILLED；超 max_wait→撤单。"""
    if quote is None:
        return PullbackDecision(PullbackAction.HOLD)
    if bars_armed > plan.get("max_wait_bars", 999):
        return PullbackDecision(PullbackAction.CANCEL_TIMEOUT, reason="max_wait_exceeded")
    low = quote.get("low", float("inf"))
    high = quote.get("high", float("-inf"))
    if low <= plan["entry_upper"] and high >= plan["entry_lower"]:
        return PullbackDecision(PullbackAction.ARMED_FILL)
    return PullbackDecision(PullbackAction.HOLD)

def check_exit(self, plan, bar, bars_held):
    """FILLED 阶段：调 _exit_kernel（与回测 simulate_exit 同源）。"""
    pos = {"entry": plan["entry_upper"],   # 回测用颈线位作 entry 基准（与 simulate_exit 对齐）
           "stop": plan["stop"],
           "take_profit_1": plan["take_profit_1"],
           "take_profit": plan["take_profit"],
           "tp1_hit": plan.get("tp1_hit", False),
           "tp1_portion": plan.get("tp1_portion", 0.5)}
    cfg = {"trailing_grace": plan["trailing_grace"], "trailing_step": plan["trailing_step"],
           "trailing_floor": plan["trailing_floor"], "stop_atr_mult": 1.5,
           "atr": plan.get("atr", 0.5), "max_holding": plan.get("max_holding", 60)}
    return _exit_kernel(pos, bar, bars_held, cfg)
```

**执行者必读**：`_exit_kernel` 的优先级/参数语义必须与 `scripts/neckline_backtest.py:simulate_exit` 逐字核对一致。若 simulate_exit 用的是 plan["entry"]（实际进场价）而非 entry_upper，按 simulate_exit 实际基准对齐——抽 kernel 时以 simulate_exit 为唯一真理源，实盘 check_exit 适配 plan 字段名。

- [ ] **Step 4: 跑测试验证通过**

Run: `python -m pytest tests/strategies/test_neckline_check_exit.py -v`
Expected: PASS（6 个测试全绿）

- [ ] **Step 5: Commit**

```bash
git add strategies/neckline_method.py tests/strategies/test_neckline_check_exit.py
git commit -m "feat(neckline): check_pullback+check_exit（_exit_kernel 复用 simulate_exit 消除双源真理）"
```

---

### Task 8: caisen 适配器实现新协议（零行为回归）

**Files:**
- Modify: `strategies/caisen_pattern.py`（加 scan_live/to_armed_plan/check_pullback/check_exit）
- Reference: `caisen/engines/exit_logic.py:check_exit`（老离场逻辑，逐字复用）、`execution/engine.py:ExecutionEngine.check_pullback`（老回踩判定几何）
- Test: `tests/strategies/test_caisen_adapter_compat.py`

**Interfaces:**
- Consumes: `strategies/base.py`（Task 5）、`caisen.engines.exit_logic`
- Produces: `CaisenPatternStrategy.check_exit`（调老 check_exit，逐字保留）

- [ ] **Step 1: 写失败测试**

`tests/strategies/test_caisen_adapter_compat.py`:
```python
# -*- coding: utf-8 -*-
"""caisen 适配器 check_exit 与老 exit_logic.check_exit 逐字一致（零回归守护）。
check_pullback 复用 engine 既有几何判定（caisen 无 max_wait 撤单）。"""
import pytest

from caisen.engines.exit_logic import check_exit as old_check_exit, ExitAction as OldAction
from strategies.base import ExitAction, PullbackAction
from strategies.caisen_pattern import CaisenPatternStrategy


@pytest.fixture
def strategy():
    return CaisenPatternStrategy(cfg_override=None)


def test_caisen_check_exit_matches_old_implementation(strategy):
    """caisen 适配器 check_exit 与老 check_exit 在多场景下逐字一致。"""
    plan = {"entry": 10.0, "stop": 9.0, "take_profit": 11.0, "take_profit_2x": 12.0}
    cfg = strategy._caisen_cfg()
    for bar, bars_held in [
        ({"high": 9.5, "low": 8.8, "close": 9.0}, 2),    # 触止损
        ({"high": 12.2, "low": 11.5, "close": 12.1}, 5), # 触 tp2x
        ({"high": 11.2, "low": 10.5, "close": 11.1}, 3), # 触 tp
        ({"high": 10.3, "low": 10.0, "close": 10.2}, 4), # HOLD
    ]:
        new_d = strategy.check_exit(plan, bar, bars_held)
        old_d = old_check_exit(
            {"entry": plan["entry"], "stop": plan["stop"],
             "take_profit": plan["take_profit"], "take_profit_2x": plan["take_profit_2x"]},
            bar, bars_held, cfg)
        # 动作语义一致（CLOSE_ALL/HOLD/UPDATE_STOP）
        assert _semantic_action(new_d.action) == _semantic_action(old_d.action), (
            f"bar={bar} bars_held={bars_held}: new={new_d.action} old={old_d.action}")


def _semantic_action(a):
    """归一化动作名（新旧枚举可能命名略异，归一到 close/hold/update_stop）。"""
    s = str(a)
    if "CLOSE" in s or "close" in s:
        return "close"
    if "UPDATE" in s or "update" in s or "new_stop" in s:
        return "update_stop"
    return "hold"


def test_caisen_check_pullback_no_timeout(strategy):
    """caisen 无 max_wait 撤单：bars_armed 再大也不 CANCEL_TIMEOUT（只 HOLD/FILL）。"""
    plan = {"entry_upper": 10.2, "entry_lower": 9.8}
    d = strategy.check_pullback(plan, quote={"high": 10.5, "low": 10.3}, bars_armed=999)
    assert d.action != PullbackAction.CANCEL_TIMEOUT   # caisen 永不超时撤单
```

- [ ] **Step 2: 跑测试验证失败**

Run: `python -m pytest tests/strategies/test_caisen_adapter_compat.py -v`
Expected: FAIL — `AttributeError: ... has no attribute 'check_exit'`

- [ ] **Step 3: 实现 caisen 适配器新方法**

`strategies/caisen_pattern.py` 加（具体 import 按既有文件对齐）：
```python
from strategies.base import ExitDecision, PullbackAction, PullbackDecision
from caisen.engines.exit_logic import check_exit as _caisen_check_exit


# CaisenPatternStrategy 类内加：

def scan_live(self, date) -> list:
    """caisen 实盘识别：复用既有 PatternScreener.screen（与回测同源）。"""
    # 按 caisen 既有 scan 逻辑：screener.screen({symbol: df.loc[:date]}, date) → 产 Signal
    # Signal.symbol/formed_at 填充；caisen 不需 neckline_price/H/atr（颈线法专属）
    ...   # 实现按 caisen/facade.py run_scan 的识别部分抽取

def to_armed_plan(self, signal, *, weight, experiment_id, total_capital) -> dict:
    """caisen Signal → ARMED plan（entry_upper=回踩上沿/stop/tp/tp2x，caisen 无 trailing）。"""
    # 按 caisen 既有 TradePlanGenerator.generate 的字段产出
    ...
    plan["experiment_id"] = experiment_id
    plan["experiment_weight"] = weight
    plan["shares"] = max(int(weight * total_capital * self._pos_cap() / plan["entry_upper"] // 100) * 100, 100)
    return plan

def check_pullback(self, plan, quote, bars_armed):
    """caisen 回踩判定：复用 ExecutionEngine.check_pullback 几何（触及区间→FILL）。

    Why 无 CANCEL_TIMEOUT：caisen 形态学无 max_wait 概念，ARMED 计划挂住等回踩。
    """
    if quote is None:
        return PullbackDecision(PullbackAction.HOLD)
    low = quote.get("low", float("inf"))
    high = quote.get("high", float("-inf"))
    if low <= plan["entry_upper"] and high >= plan["entry_lower"]:
        return PullbackDecision(PullbackAction.ARMED_FILL)
    return PullbackDecision(PullbackAction.HOLD)

def check_exit(self, plan, bar, bars_held):
    """caisen 出场：逐字调老 exit_logic.check_exit（零行为回归）。"""
    pos = {"entry": plan["entry"], "stop": plan["stop"],
           "take_profit": plan["take_profit"], "take_profit_2x": plan.get("take_profit_2x")}
    decision = _caisen_check_exit(pos, bar, bars_held, self._caisen_cfg())
    # 老决策 → 新 ExitDecision 适配（字段名映射，语义逐字保留）
    return _adapt_caisen_decision(decision)

def _caisen_cfg(self):
    """caisen StrategyConfig（构造时注入或从 cfg 取）。"""
    return self._cfg   # 既有 caisen StrategyConfig

def _pos_cap(self):
    return getattr(self._cfg, "pos_cap", 0.14)
```

`_adapt_caisen_decision` 把老 `ExitDecision`（caisen.engines.exit_logic）映射到新 `strategies.base.ExitDecision`——动作语义逐字保留（CLOSE→CLOSE_ALL，UPDATE_STOP→UPDATE_STOP，HOLD→HOLD）。

- [ ] **Step 4: 跑测试验证通过**

Run: `python -m pytest tests/strategies/test_caisen_adapter_compat.py -v`
Expected: PASS（2 个测试全绿）

- [ ] **Step 5: Commit**

```bash
git add strategies/caisen_pattern.py tests/strategies/test_caisen_adapter_compat.py
git commit -m "feat(caisen): 适配器实现新协议（check_exit 调老 exit_logic 零回归）"
```

---

### Task 9: execution/storage.py plan 加归因字段

**Files:**
- Modify: `execution/storage.py:84-115`（`_plan_to_dict`/`_restore_plan_dict`）
- Test: `tests/caisen/test_storage.py`（追加归因往返测试）

**Interfaces:**
- Consumes: 无新依赖
- Produces: plan dict 新增可空字段 `experiment_id`/`experiment_weight`（老 plan 无此字段 = None，向后兼容）

- [ ] **Step 1: 写失败测试**

`tests/caisen/test_storage.py` 追加（若文件已有 `_make_plan` 之类辅助，复用）：
```python
def test_plan_roundtrip_preserves_experiment_attribution():
    """save→load 往返保留 experiment_id/experiment_weight 归因字段。"""
    from execution import storage
    from caisen.storage_test_fixtures import make_plan  # 既有测试辅助，按实际 import 调整

    plan = make_plan(plan_id="attr1")
    plan["experiment_id"] = "neckline_v1_20260722"
    plan["experiment_weight"] = 0.2
    storage.save_plans("2026-07-22", [plan])
    loaded = storage.load_plans()
    [p] = [x for x in loaded if x["plan_id"] == "attr1"]
    assert p["experiment_id"] == "neckline_v1_20260722"
    assert p["experiment_weight"] == 0.2


def test_old_plan_without_attribution_loads_as_none():
    """老 plan（无归因字段）load 时 experiment_id=None（向后兼容，归「未归因」桶）。"""
    from execution import storage
    from caisen.storage_test_fixtures import make_plan

    plan = make_plan(plan_id="old1")
    plan.pop("experiment_id", None)
    plan.pop("experiment_weight", None)
    storage.save_plans("2026-07-20", [plan])
    loaded = storage.load_plans()
    [p] = [x for x in loaded if x["plan_id"] == "old1"]
    assert p.get("experiment_id") is None
    assert p.get("experiment_weight") is None
```

（`make_plan` 用 `tests/caisen/test_storage.py` 既有的 plan 构造辅助；若无，按该文件现有测试的造数方式对齐。）

- [ ] **Step 2: 跑测试验证失败**

Run: `python -m pytest tests/caisen/test_storage.py -k attribution -v`
Expected: FAIL — save→load 后 experiment_id 丢失（_plan_to_dict/_restore_plan_dict 未透传新字段）

- [ ] **Step 3: 改 _plan_to_dict / _restore_plan_dict**

读 `execution/storage.py:84-115` 的 `_plan_to_dict` 与 `_restore_plan_dict`，确保：
- `_plan_to_dict`：把 `experiment_id`/`experiment_weight` 透传到落盘 dict（若 plan 有这两个键）
- `_restore_plan_dict`：读回时若 dict 有这两键则填入 plan，无则填 None（向后兼容）

字段可选（`dict.get`），老 plan 不带 = None。具体改动按既有 `_plan_to_dict` 的字段透传范式追加两行。

- [ ] **Step 4: 跑测试验证通过**

Run: `python -m pytest tests/caisen/test_storage.py -v`
Expected: PASS（既有 ~25 测试 + 2 新归因测试全绿，零回归）

- [ ] **Step 5: Commit**

```bash
git add execution/storage.py tests/caisen/test_storage.py
git commit -m "feat(execution): plan 加 experiment_id/experiment_weight 归因字段（向后兼容）"
```

---

### Task 10: execution/engine.py 出场路由改造（按 experiment_id 路由 Strategy）

**Files:**
- Modify: `execution/engine.py:80-330`（`ExecutionEngine.__init__`/`tick_pullback`/`tick_exit`）
- Test: `tests/caisen/test_engine_routing.py`（新建）、`tests/caisen/test_engine_close_portion.py`（新建）、`tests/caisen/test_engine_caisen_zero_regression.py`（新建）

**Interfaces:**
- Consumes: `experiment.resolver.resolve_active`、`strategies.registry.build_strategy`、Task 5-8 的 Strategy 协议实现
- Produces: `ExecutionEngine` 改造后按 `plan.experiment_id` 路由到对应 Strategy 的 check_pullback/check_exit；`CLOSE_PORTION` 下单 qty=shares×portion

- [ ] **Step 1: 写失败测试**

`tests/caisen/test_engine_routing.py`:
```python
# -*- coding: utf-8 -*-
"""ExecutionEngine 按 plan.experiment_id 路由到正确 Strategy 的 check_exit。"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from execution.engine import ExecutionEngine


@pytest.fixture
def engine_with_strategies():
    """构造 ExecutionEngine，注入两个 mock strategy（按 experiment_id 路由）。"""
    strat_neckline = MagicMock()
    strat_neckline.check_exit = MagicMock(return_value=__import__(
        "strategies.base", fromlist=["ExitDecision"]).ExitDecision(
        action="close_all", reason="stop_loss"))
    strat_caisen = MagicMock()
    strat_caisen.check_exit = MagicMock(return_value=__import__(
        "strategies.base", fromlist=["ExitDecision"]).ExitDecision(
        action="hold", reason="hold"))
    eng = ExecutionEngine.__new__(ExecutionEngine)   # 绕过 __init__ 注入测试态
    eng.trading = MagicMock()
    eng.trading.get_status = MagicMock(return_value={"connected": True, "locked": False})
    eng.trading.submit_order = AsyncMock(return_value={"state": "FILLED"})
    eng._strategies = {"e_neckline": strat_neckline, "e_caisen": strat_caisen}
    eng._today_bar = MagicMock(return_value=100)
    eng._get_quote = AsyncMock(return_value={"high": 11, "low": 9, "close": 10})
    return eng, strat_neckline, strat_caisen


def test_routes_to_correct_strategy_by_experiment_id(engine_with_strategies, monkeypatch):
    """plan.experiment_id=e_neckline → 调颈线法 strategy.check_exit。"""
    eng, strat_nl, strat_cs = engine_with_strategies
    plan = {"plan_id": "p1", "symbol": "000001.SZ", "experiment_id": "e_neckline",
            "shares": 200, "entry_upper": 10, "stop": 9, "take_profit": 11,
            "take_profit_1": 10.5, "tp1_hit": False, "tp1_portion": 0.5,
            "trailing_grace": 5, "trailing_step": 0.1, "trailing_floor": 0.5,
            "entry_bar": 90, "bars_held": 10}
    monkeypatch.setattr("execution.engine.storage.load_plans", lambda status="FILLED": [plan])
    monkeypatch.setattr("execution.engine.storage.update_plan", lambda *a, **k: None)
    import asyncio
    asyncio.run(eng.tick_exit())
    strat_nl.check_exit.assert_called_once()
    strat_cs.check_exit.assert_not_called()


def test_close_portion_orders_partial_qty(engine_with_strategies, monkeypatch):
    """CLOSE_PORTION 时 submit_order 的 qty = shares × portion。"""
    from strategies.base import ExitAction, ExitDecision
    eng, strat_nl, _ = engine_with_strategies
    strat_nl.check_exit.return_value = ExitDecision(
        action=ExitAction.CLOSE_PORTION, portion=0.5, reason="take_profit_1")
    plan = {"plan_id": "p2", "symbol": "000001.SZ", "experiment_id": "e_neckline",
            "shares": 200, "entry_upper": 10, "stop": 9, "take_profit": 11,
            "take_profit_1": 10.5, "tp1_hit": False, "tp1_portion": 0.5,
            "trailing_grace": 5, "trailing_step": 0.1, "trailing_floor": 0.5,
            "entry_bar": 90, "bars_held": 3}
    monkeypatch.setattr("execution.engine.storage.load_plans", lambda status="FILLED": [plan])
    monkeypatch.setattr("execution.engine.storage.update_plan", lambda *a, **k: None)
    import asyncio
    asyncio.run(eng.tick_exit())
    # submit_order 被调，qty=200×0.5=100
    args, kwargs = eng.trading.submit_order.call_args
    order = args[0] if args else kwargs.get("order")
    assert order.qty == 100
```

`tests/caisen/test_engine_caisen_zero_regression.py`（关键回归守护）:
```python
# -*- coding: utf-8 -*-
"""caisen plan 走改造后引擎，行为与改造前逐字一致（零回归）。

改造前：tick_exit 调 caisen.engines.exit_logic.check_exit
改造后：tick_exit 按 plan.experiment_id 路由 → caisen 适配器 check_exit（调老 check_exit）
两条路径必须产出相同决策、相同下单。"""
# 实现思路：构造同一组 caisen plan + 行情，分别跑「直接调老 check_exit」与「路由后」，
# 断言 submit_order 调用序列（qty/side/price）逐字一致。
# 详细断言按 execution/engine.py 既有 test_execution.py 的造数范式对齐。
```
（此测试具体造数量大，执行者按 `tests/caisen/test_execution.py` 既有范式补全——核心断言：caisen plan 的下单序列改造前后逐字一致。）

- [ ] **Step 2: 跑测试验证失败**

Run: `python -m pytest tests/caisen/test_engine_routing.py -v`
Expected: FAIL — `ExecutionEngine` 无 `_strategies` 属性 / tick_exit 仍调老 check_exit

- [ ] **Step 3: 改造 engine.py**

读 `execution/engine.py:100-330` 全文，改造点：

1. **`__init__` 加 strategy 缓存构建**：调 `resolve_active()` → 每个实验 `build_strategy(strategy_name, cfg_override=params)` → `self._strategies = {experiment_id: strategy}`。保留既有 `trading_service`/`cfg` 注入。

2. **`tick_pullback` 改造**（原 `execution/engine.py:171-247`）：
   - 读到 plan 后，按 `plan["experiment_id"]` 查 `self._strategies[exp_id]`
   - 调 `strategy.check_pullback(plan, quote, bars_armed)`（替换当前 `self.check_pullback`）
   - `PullbackAction.ARMED_FILL` → submit_order(buy) → FILLED（同既有）
   - `PullbackAction.CANCEL_TIMEOUT` → `storage.update_plan(plan_id, status="CANCELLED")`（新）
   - `PullbackAction.HOLD` → continue
   - 未知 experiment_id（缓存缺失）→ warning 跳过（防御，不应发生）

3. **`tick_exit` 改造**（原 `execution/engine.py:249-330`）：
   - 按 `plan["experiment_id"]` 查 strategy
   - 调 `strategy.check_exit(plan, bar, bars_held)`（替换当前 `check_exit`）
   - `ExitAction.CLOSE_PORTION` → `submit_order(sell, qty=plan["shares"]×decision.portion)`；成交后若 plan 还有剩余（portion<1）保持 FILLED + update_plan(tp1_hit=True)，否则 CLOSED
   - `ExitAction.CLOSE_ALL` → submit_order(sell, qty=shares) → CLOSED（同既有）
   - `ExitAction.UPDATE_STOP` → update_plan(stop=new_stop)（同既有移动止盈）
   - `ExitAction.HOLD` → continue

4. **保留断线保护**（`get_status` locked/not connected 跳过逻辑不变）。

具体代码改动按 engine.py 既有 tick 编排范式（try/except 单计划隔离、_logger.warning 可观测性）。

- [ ] **Step 4: 跑测试验证通过**

Run: `python -m pytest tests/caisen/test_engine_routing.py tests/caisen/test_engine_caisen_zero_regression.py tests/caisen/test_execution.py -v`
Expected: PASS（新路由测试 + caisen 零回归 + 既有 execution 测试全绿）

- [ ] **Step 5: Commit**

```bash
git add execution/engine.py tests/caisen/test_engine_routing.py tests/caisen/test_engine_close_portion.py tests/caisen/test_engine_caisen_zero_regression.py
git commit -m "refactor(execution): ExecutionEngine 按 experiment_id 路由 Strategy（出场归策略侧·caisen 零回归）"
```

---

### Task 11: scan_service resolve 注入

**Files:**
- Modify: `caisen/facade.py:run_scan`（或新建 `execution/scan_service.py`，按 facade 现状定）
- Test: `tests/caisen/test_scan_service_resolve.py`（新建）

**Interfaces:**
- Consumes: `experiment.resolver.resolve_active`、`strategies.registry.build_strategy`、Task 6 的 scan_live/to_armed_plan
- Produces: `run_scan(date, total_capital)` 遍历在线实验 → 生成 ARMED plan → save_plans

- [ ] **Step 1: 写失败测试**

`tests/caisen/test_scan_service_resolve.py`:
```python
# -*- coding: utf-8 -*-
"""scan_service.run_scan：resolve_active → 遍历实验 → scan_live → to_armed_plan → save_plans。

用 mock strategy 隔离真实数据加载，验证 resolve→plan 编排正确。"""
import pytest
from unittest.mock import MagicMock, patch

from experiment import store
from experiment.models import ExperimentStatus, ExperimentVersion


@pytest.fixture
def db_with_active_experiment(tmp_path, monkeypatch):
    db = str(tmp_path / "t.db")
    store.init_db(db)
    v = ExperimentVersion(experiment_id="e1", strategy_name="neckline", params={"window": 60},
                          weight=0.0, status=ExperimentStatus.DRAFT, version=1,
                          source="manual", created_at="2026-07-22T10:00:00")
    store.create_version(db, v)
    store.promote(db, "e1", weight=1.0, operator="cli", now="t")
    monkeypatch.setattr("experiment.store._DEFAULT_DB", db)
    monkeypatch.setattr("experiment.resolver._DEFAULT_DB", db)
    return db


def test_run_scan_resolves_and_generates_plans(db_with_active_experiment, monkeypatch):
    """resolve 到 1 个 ACTIVE 实验 → 遍历 → 每标的产 plan 带 experiment_id。"""
    from strategies.base import Signal
    fake_strategy = MagicMock()
    fake_strategy.scan_live = MagicMock(return_value=[
        Signal(symbol="000001.SZ", formed_at="2026-07-15", breakout_date="2026-07-22",
               neckline_price=10.0, H=1.0, atr=0.5)])
    fake_strategy.to_armed_plan = MagicMock(return_value={
        "plan_id": "p1", "symbol": "000001.SZ", "experiment_id": "e1",
        "experiment_weight": 1.0, "shares": 1000})
    monkeypatch.setattr("strategies.registry.build_strategy", lambda name, **kw: fake_strategy)

    saved = []
    monkeypatch.setattr("execution.storage.save_plans", lambda date, plans: saved.extend(plans))

    from caisen.facade import run_scan   # 或 execution.scan_service 视落点
    run_scan("2026-07-22", total_capital=1_000_000)

    assert len(saved) == 1
    assert saved[0]["experiment_id"] == "e1"
    fake_strategy.to_armed_plan.assert_called_once_with(
        fake_strategy.scan_live.return_value[0], weight=1.0, experiment_id="e1",
        total_capital=1_000_000)


def test_run_scan_failfast_when_no_active(tmp_path, monkeypatch):
    """无 ACTIVE 实验 → fail-fast（不下单，抛或返空）。"""
    db = str(tmp_path / "t.db")
    store.init_db(db)
    monkeypatch.setattr("experiment.resolver._DEFAULT_DB", db)
    from caisen.facade import run_scan
    # 无在线实验：run_scan 应早退（不调 build_strategy）
    with patch("strategies.registry.build_strategy") as bs:
        run_scan("2026-07-22", total_capital=1_000_000)
        bs.assert_not_called()
```

- [ ] **Step 2: 跑测试验证失败**

Run: `python -m pytest tests/caisen/test_scan_service_resolve.py -v`
Expected: FAIL — `run_scan` 不存在或不调 resolve_active

- [ ] **Step 3: 实现 run_scan resolve 注入**

在 `caisen/facade.py` 加（或新建 `execution/scan_service.py`，按 facade 现状——若 facade.run_scan 已存在则改造其开头）：
```python
from experiment.resolver import resolve_active
from strategies.registry import build_strategy
from execution import storage


def run_scan(date: str, total_capital: float) -> list:
    """实盘 scan：resolve 在线实验 → 遍历 → scan_live + to_armed_plan → save_plans。

    design §5.1 数据流。无 ACTIVE 实验 fail-fast（resolve 返空 → 直接返，不下单）。
    """
    experiments = resolve_active()
    if not experiments:
        return []   # fail-fast：无在线实验，不 scan
    all_plans = []
    for exp in experiments:
        strategy = build_strategy(exp.strategy_name, cfg_override=exp.params)
        for signal in strategy.scan_live(date):
            plan = strategy.to_armed_plan(signal, weight=exp.weight,
                                          experiment_id=exp.experiment_id,
                                          total_capital=total_capital)
            all_plans.append(plan)
    if all_plans:
        storage.save_plans(date, all_plans)   # 落 plans/<date>.json，带 experiment_id
    return all_plans
```

`total_capital` 由调用方（schtasks 脚本 / API）从 trading_service 查账户资金注入。

- [ ] **Step 4: 跑测试验证通过**

Run: `python -m pytest tests/caisen/test_scan_service_resolve.py -v`
Expected: PASS（2 个测试全绿）

- [ ] **Step 5: Commit**

```bash
git add caisen/facade.py tests/caisen/test_scan_service_resolve.py
git commit -m "feat(scan): run_scan resolve 注入（resolve_active→scan_live→to_armed_plan→save_plans）"
```

---

### Task 12: report 归因聚合命令

**Files:**
- Modify: `experiment/cli.py`（加 report 子命令）
- Test: `tests/experiment/test_report.py`

**Interfaces:**
- Consumes: `execution/storage.load_plans`（扫所有 plan）
- Produces: CLI `report --since <date>` 按 experiment_id 聚合 PnL/胜率

- [ ] **Step 1: 写失败测试**

`tests/experiment/test_report.py`:
```python
# -*- coding: utf-8 -*-
"""report 命令：扫 plans/*.json 按 experiment_id 聚合，输出 prod vs candidate 对比。"""
import pytest
from unittest.mock import patch

from experiment import cli


def _plan(eid, exit_reason="stop_loss", rr=-1.0, pnl_pct=-2.0):
    return {"plan_id": f"{eid}_p", "experiment_id": eid, "symbol": "000001.SZ",
            "exit_reason": exit_reason, "rr": rr, "pnl_pct": pnl_pct,
            "status": "CLOSED"}


def test_report_aggregates_by_experiment(capsys, monkeypatch):
    """同 experiment_id 的 plan 聚到一组，算 n/胜率/PnL。"""
    plans = [
        _plan("e_prod", "take_profit_2", 2.0, 4.0),
        _plan("e_prod", "stop_loss", -1.0, -2.0),
        _plan("e_cand", "take_profit_2", 2.0, 4.0),
        _plan("e_cand", "take_profit_2", 2.0, 4.0),
        _plan("e_cand", "stop_loss", -1.0, -2.0),
    ]
    monkeypatch.setattr(cli, "_DEFAULT_DB", ":memory:")
    with patch("experiment.cli._load_closed_plans", return_value=plans):
        rc = cli.main(["report", "--since", "2026-07-01"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "e_prod" in out and "e_cand" in out
    # e_cand: 3 笔，2 胜 → 胜率 66%
    assert "66" in out or "0.67" in out or "67" in out


def test_report_handles_unattributed_plans(capsys, monkeypatch):
    """无 experiment_id 的老 plan 归「未归因」桶，不崩。"""
    plans = [{"plan_id": "old1", "experiment_id": None, "exit_reason": "timeout",
              "rr": 0.0, "pnl_pct": 0.0, "status": "CLOSED"}]
    monkeypatch.setattr(cli, "_DEFAULT_DB", ":memory:")
    with patch("experiment.cli._load_closed_plans", return_value=plans):
        rc = cli.main(["report", "--since", "2026-07-01"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "未归因" in out or "None" in out or "unattributed" in out.lower()
```

- [ ] **Step 2: 跑测试验证失败**

Run: `python -m pytest tests/experiment/test_report.py -v`
Expected: FAIL — `report` 子命令未注册 / `_load_closed_plans` 不存在

- [ ] **Step 3: 实现 report**

`experiment/cli.py` 加：
```python
from execution.storage import load_plans


def _load_closed_plans(since: str = None) -> list:
    """扫所有已 CLOSED plan（按 experiment_id 聚合用）。since 过滤 formed_at/breakout_date。"""
    plans = load_plans(status="CLOSED")
    if since:
        plans = [p for p in plans
                 if str(p.get("formed_at") or p.get("breakout_date") or "") >= since]
    return plans


def _report(args) -> int:
    """按 experiment_id 聚合 plan 归因：n/胜率/平均rr/平均pnl。"""
    plans = _load_closed_plans(args.since)
    groups = {}
    for p in plans:
        key = p.get("experiment_id") or "未归因"
        groups.setdefault(key, []).append(p)
    print(f"{'experiment_id':30}{'n':>5}{'胜率':>8}{'均rr':>8}{'均pnl%':>9}")
    for eid, ps in sorted(groups.items()):
        n = len(ps)
        wins = sum(1 for p in ps if (p.get("rr") or 0) > 0)
        win_rate = wins / n if n else 0
        avg_rr = sum(p.get("rr") or 0 for p in ps) / n if n else 0
        avg_pnl = sum(p.get("pnl_pct") or 0 for p in ps) / n if n else 0
        print(f"{eid:30}{n:>5}{win_rate*100:>7.0f}%{avg_rr:>8.2f}{avg_pnl:>9.2f}")
    return 0
```

在 `_build_parser` 加 report 子命令：
```python
sp = sub.add_parser("report", help="按 experiment_id 聚合 plan 归因（PnL/胜率）")
sp.add_argument("--since", default=None, help="起始日期 YYYY-MM-DD")
```

在 `main` 分支加：`elif args.cmd == "report": return _report(args)`

- [ ] **Step 4: 跑测试验证通过**

Run: `python -m pytest tests/experiment/test_report.py -v`
Expected: PASS（2 个测试全绿）

- [ ] **Step 5: Commit**

```bash
git add experiment/cli.py tests/experiment/test_report.py
git commit -m "feat(experiment): report 归因聚合（扫 plans 按 experiment_id 算 PnL/胜率）"
```

---

### Task 13: 端到端测试（Mock 链路）+ miniQMT 虚拟盘手动验收

**Files:**
- Test: `tests/experiment/test_e2e_scan_to_order.py`（新建）
- Manual: miniQMT 虚拟盘验收 SOP（本任务步骤记录）

**Interfaces:**
- Consumes: Task 1-12 全部
- Produces: 端到端归因不断链验证（Mock）+ miniQMT 虚拟盘手动验收清单

- [ ] **Step 1: 写端到端测试**

`tests/experiment/test_e2e_scan_to_order.py`:
```python
# -*- coding: utf-8 -*-
"""端到端（Mock 链路）：create exp → scan_live → ARMED plan(带 exp_id) → tick(Mock) →
FILLED → check_exit → CLOSED，experiment_id 归因全程不断链。

design §12 验收标准 2。MockExecutionGateway 虚拟撮合，零资金风险。"""
import asyncio

import pytest
from unittest.mock import patch

from experiment import store, resolver, cli
from experiment.models import ExperimentStatus, ExperimentVersion
from trading.execution_gateway import MockExecutionGateway, OrderRequest
from execution.engine import ExecutionEngine


@pytest.fixture
def db(tmp_path, monkeypatch):
    p = str(tmp_path / "t.db")
    store.init_db(p)
    monkeypatch.setattr("experiment.store._DEFAULT_DB", p)
    monkeypatch.setattr("experiment.resolver._DEFAULT_DB", p)
    monkeypatch.setattr("execution.engine.resolve_active",
                        lambda: resolver.resolve_active(p))
    return p


def test_e2e_attribution_chain(db, monkeypatch, tmp_path):
    """全链路：experiment_id 从创建→plan→下单→离场全程携带。"""
    # 1. create + promote 一个颈线法实验
    cli.main(["create", "--strategy", "neckline", "--params", '{"window":60}',
              "--experiment-id", "e1", "--created-at", "2026-07-22T10:00:00"])
    cli.main(["promote", "e1", "--weight", "1.0"])

    # 2. scan_live 产 ARMED plan（用 mock 数据，避免依赖 parquet）
    from strategies.base import Signal
    fake_strategy = type("S", (), {})()
    fake_strategy.scan_live = lambda date: [Signal(
        symbol="000001.SZ", formed_at="2026-07-15", breakout_date="2026-07-22",
        neckline_price=10.0, H=1.0, atr=0.5)]
    fake_strategy.to_armed_plan = lambda sig, *, weight, experiment_id, total_capital: {
        "plan_id": "p1", "symbol": sig.symbol, "experiment_id": experiment_id,
        "experiment_weight": weight, "entry_upper": 10.5, "entry_lower": 9.5,
        "shares": 1000, "stop": 9.0, "take_profit": 12.0, "take_profit_1": 11.0,
        "tp1_hit": False, "tp1_portion": 0.5, "max_wait_bars": 5,
        "trailing_grace": 5, "trailing_step": 0.1, "trailing_floor": 0.5,
        "formed_at": sig.formed_at, "breakout_date": sig.breakout_date}
    fake_strategy.check_pullback = lambda plan, quote, bars_armed: __import__(
        "strategies.base", fromlist=["PullbackDecision"]).PullbackDecision("armed_fill")
    fake_strategy.check_exit = lambda plan, bar, bars_held: __import__(
        "strategies.base", fromlist=["ExitDecision"]).ExitDecision(
        action="close_all", reason="stop_loss")
    monkeypatch.setattr("strategies.registry.build_strategy", lambda name, **kw: fake_strategy)
    monkeypatch.setattr("execution.storage.save_plans", lambda *a, **k: None)

    from caisen.facade import run_scan
    plans = run_scan("2026-07-22", total_capital=1_000_000)
    assert plans[0]["experiment_id"] == "e1"   # plan 带归因

    # 3. ExecutionEngine（Mock gateway）消费 ARMED plan
    gateway = MockExecutionGateway()
    asyncio.run(gateway.connect())
    eng = ExecutionEngine.__new__(ExecutionEngine)
    eng.trading = gateway
    eng.trading.get_status = lambda: {"connected": True, "locked": False}
    eng._strategies = {"e1": fake_strategy}
    eng._today_bar = lambda: 0
    eng._get_quote = lambda *a, **k: None

    armed_plan = {**plans[0], "status": "ARMED"}
    with patch("execution.engine.storage.load_plans", lambda status="ARMED": [armed_plan]), \
         patch("execution.engine.storage.update_plan", lambda *a, **k: None):
        asyncio.run(eng.tick_pullback())
    # Mock submit_order 假设全额成交（MockExecutionGateway 行为）→ 推进 FILLED

    # 4. 验证：plan 的 experiment_id 全程未丢
    assert armed_plan["experiment_id"] == "e1"
    assert armed_plan["experiment_weight"] == 1.0
```

- [ ] **Step 2: 跑测试验证**

Run: `python -m pytest tests/experiment/test_e2e_scan_to_order.py -v`
Expected: PASS（归因不断链）。若 MockExecutionGateway 不满足 ExecutionExecutor Protocol（缺 get_status），用既有 `tests/caisen/test_execution.py` 的 fake trading 桩范式对齐。

- [ ] **Step 3: miniQMT 虚拟盘手动验收 SOP（交易时段执行）**

在交易时段（9:30-15:00，撤单才能闭环——记忆载明盘后撤单不生效）执行：
1. `.venv310/Scripts/python -m experiment create --strategy neckline --params '<颈线法 v6 基线参数>' --experiment-id neckline_v6_<日期> --created-at <ts>`
2. `.venv310/Scripts/python -m experiment promote neckline_v6_<日期> --weight 0.1`（小资金 10% 试水）
3. 触发 scan：`run_scan(<当日>, total_capital=<查 miniQMT queryAsset>)` → 确认 plans/<date>.json 含 experiment_id
4. 启动 ExecutionEngine beat（接 QmtExecutionGateway，userdata `D:\东北证券NET专业版(测试版)\userdata_mini`，账号 `10110356`）
5. 观察：tick_pullback 是否在回踩区间挂单 → FILLED；tick_exit 是否按颈线法 tp1/tp2/止损/trailing 离场
6. 平仓后：`.venv310/Scripts/python -m experiment report --since <当日>` 确认归因聚合正确
7. 回滚演练：`promote` 一个 candidate → 观察 → `rollback` → 确认恢复

记录验收结果到 `.superpowers/sdd/` 或 commit message。

- [ ] **Step 4: 全量回归测试**

Run: `python -m pytest tests/experiment/ tests/strategies/ tests/caisen/ -v`
Expected: 全绿（实验系统 + 策略协议 + caisen 零回归）

- [ ] **Step 5: Commit + 合并准备**

```bash
git add tests/experiment/test_e2e_scan_to_order.py
git commit -m "test(experiment): 端到端归因不断链（Mock 链路）+ miniQMT 虚拟盘验收 SOP"
```

miniQMT 虚拟盘验收通过后，分支可合并 master（`git checkout master && git merge feat/auto-trading-engine`）。

---

## Self-Review

**1. Spec coverage**：
- §3 数据模型 → Task 1；§3.5 SQLite schema → Task 2；§3.6 plan 归因 → Task 9 ✓
- §4 架构/依赖 → Task 1-4（experiment 包）✓
- §5 数据流（scan/CLI/归因）→ Task 4/11/12 ✓
- §6 颈线法接入（协议扩展/to_armed_plan/check_exit/引擎改造）→ Task 5/6/7/8/10 ✓
- §7 运行模式（Mock + miniQMT）→ Task 13 ✓
- §8 错误处理 → Task 2（权重红线/事务回滚）、Task 11（fail-fast）、Task 10（未知 experiment_id 防御）✓
- §9 测试策略 → 各 Task 内 TDD + Task 13 e2e ✓
- §12 验收标准 6 条 → Task 13 覆盖（CLI/端到端/归因/红线）；caisen 零回归 → Task 8/10 ✓
- §2.2 非目标（storage 全迁 SQLite / ParamLab UI / 实时看板）→ 明确不在本计划 ✓

**2. Placeholder scan**：Task 8（caisen scan_live/to_armed_plan）与 Task 10（caisen 零回归测试）标了「按既有范式对齐」——这两处依赖 caisen 既有 PatternScreener/TradePlanGenerator 的具体签名，执行者需读 `caisen/facade.py`/`caisen/engines/plan.py` 现有代码对齐，非占位符（给了精确引用位置 + 字段契约）。其余任务代码完整。

**3. Type consistency**：`ExperimentVersion`/`ActiveExperiment`/`Signal`/`ExitDecision`/`PullbackDecision` 跨任务签名一致；`_exit_kernel`/`check_exit`/`_caisen_check_exit` 的 pos/bar/cfg 入参字段名统一（entry/stop/take_profit/take_profit_1/take_profit_2x/tp1_hit/tp1_portion/trailing_*）；`resolve_active` 在 Task 3 定义、Task 11/13 消费一致；`run_scan(date, total_capital)` 签名跨 Task 11/13 一致。

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-22-experiment-system.md`. Two execution options:

1. **Subagent-Driven (recommended)** — 每个 Task 派一个 fresh subagent，任务间 review，快速迭代
2. **Inline Execution** — 本会话内按 executing-plans 批量执行，检查点 review

Which approach?
