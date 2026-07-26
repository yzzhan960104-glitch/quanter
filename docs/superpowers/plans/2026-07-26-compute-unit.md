# Mac 远程计算单元(compute_unit)实施计划 · Phase 1 MVP

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 打包一个自包含的 `compute_unit` 计算单元 + Win 端 `task_export`,让 Mac M1Max(封闭工作机·只 git pull)能离线跑 discovery 参数回测,输出 top-N 摘要供人 AirDrop 回 Win,Win discovery 内核零改动。

**Architecture:** 单仓库 `quanter`,代码 / `a_shares_daily.parquet` / `task.json` 全进 git,Mac 只用 `git pull`。Mac 端 `compute_unit` 只借 discovery 三个纯函数(`freeze`/`evaluate`/`holdout_split`),输入 `task.json`(Win 端预算 trial_id + 三件哈希),输出 `result.json` + 人可读摘要。跨机一致性靠 `git_commit`/`engine_hash`/`parquet_sha256` 三件哈希 + snapshot 双校验。结果不回库(路径 2),好参数 Win 手动补跑(trial_id 一致自动去重)。

**Tech Stack:** Python 3.10 stdlib(dataclasses / json / argparse / multiprocessing / hashlib / subprocess / pathlib / datetime / uuid)+ pandas/numpy/pyarrow(已有)。零新依赖。`mp.get_context("spawn")` 跨平台并发(沿用 `discovery/worker.py` 四铁律)。

## Global Constraints

> 以下为 spec 全局约束,逐字抄录,每个任务的隐含前置:

- **C1(discovery 内核零改动)**:`discovery/{runner,worker,store,objective,snapshot,split}.py` **只读 import,不改一行**;Phase 1 不挂任何导出钩子。
- **C2(Mac 只借三纯函数)**:`compute_unit` 只 `from discovery.{objective,snapshot,split} import {evaluate,freeze,holdout_split}`,不碰 discovery 的搜索/落库/调度。
- **C3(全中文注释)**:所有新增代码配备像素级中文注释(CLAUDE.md C7),说明 What + Why。
- **C4(跨机一致性)**:`env_check` 必须校验 `git_commit`/`engine_hash`/`parquet_sha256` 三件哈希 + Mac 本地 `freeze` 重算的 `universe_count`/`date_range` 双校验,任一漂移抛 `EnvDriftError`(退出码 3)。
- **C5(TDD)**:每个组件先写失败测试,再实现,再跑通过,再提交。
- **C6(同机等价红线)**:`compute_unit` 跑出的 inner/outer/n_total 必须与 `discovery.objective.evaluate` 直跑**逐字段相等**(同机 Win 本机,浮点完全一致)。命根子——compute_unit 不能偷偷改指标口径。
- **C7(spawn 四铁律)**:`_init_worker`/`_eval_worker`/`_eval_one` 顶层定义(可 pickle);子进程顶层无副作用;universe 经 initializer 注入模块全局(不随每 params pickle);单组异常→`failed`、`n_total==0`→`degenerate`。
- **C8(trial_id Win 权威)**:`trial_id` 由 Win 端 `task_export` 用 `discovery.store.trial_id_of` 预算放进 `task.json`,Mac 不算、原样回填,保证跨机必然一致。
- **C9(Python 解释器)**:所有 `python` 命令在 Win 本机用 `F:/quanter/.venv310/Scripts/python.exe`(系统 python 无依赖)。

---

## File Structure

| 文件 | 动作 | 责任 |
|------|------|------|
| `compute_unit/__init__.py` | **新建** | 包标识(空 docstring) |
| `compute_unit/protocol.py` | **新建** | `Task`/`Result`/`TrialSpec`/`TrialResult`/`SegmentSpec`/`SplitSpec`/`SnapshotMetaSpec` dataclass + JSON 序列化(date↔str) |
| `compute_unit/env_check.py` | **新建** | `EnvDriftError` + `_check_hashes` + `_check_snapshot` + `verify_and_freeze`/`verify` |
| `compute_unit/runner.py` | **新建** | `_eval_one`(纯函数)+ `_eval_worker`(spawn 包装)+ `_init_worker` + `_eval_batch` + `run` |
| `compute_unit/summary.py` | **新建** | `summarize(result, top_n)` → 钉钉友好中文文本 |
| `compute_unit/__main__.py` | **新建** | CLI:`verify` / `run` / `summary` 三子命令 |
| `compute_unit/task_export.py` | **新建** | Win 端:`export_task(params_list, ...)` + CLI,预算 trial_id + 三件哈希 |
| `tests/compute_unit/__init__.py` | **新建** | 测试包标识(空) |
| `tests/compute_unit/conftest.py` | **新建** | `synth_sym_df` / `fixed_params` / `synth_universe` fixture(与 `tests/discovery/conftest.py` 同口径) |
| `tests/compute_unit/test_protocol.py` | **新建** | 序列化往返 |
| `tests/compute_unit/test_env_check.py` | **新建** | 三件哈希 + snapshot 双校验各漂移分支 |
| `tests/compute_unit/test_runner.py` | **新建** | `_eval_one`/`_eval_batch` 等价红线 + failed/degenerate/empty |
| `tests/compute_unit/test_summary.py` | **新建** | top-N 选取 + 中文格式 + 诊断计数 |
| `tests/compute_unit/test_task_export.py` | **新建** | mock freeze/git,字段完整 + trial_id 一致 |
| `tests/compute_unit/test_e2e.py` | **新建** | `_eval_batch` → `summarize` 全链路等价(C6) |
| `.gitignore` | **改** | 第 86 行 `data_lake/` 加取反行放行 `a_shares_daily.parquet` |

> **依赖顺序**:Task 1(protocol)→ Task 2(env_check)→ Task 3(runner,含 conftest)→ Task 4(summary)→ Task 5(__main__)→ Task 6(task_export)→ Task 7(.gitignore + parquet 入 git)→ Task 8(e2e)。

---

## Task 1: protocol.py —— Task/Result dataclass + JSON 序列化

**Files:**
- Create: `compute_unit/__init__.py`、`compute_unit/protocol.py`
- Test: `tests/compute_unit/__init__.py`、`tests/compute_unit/test_protocol.py`

**Interfaces:**
- Consumes: 无(纯数据载体)
- Produces: `Task`/`Result`/`TrialSpec`/`TrialResult`/`SegmentSpec`/`SplitSpec`/`SnapshotMetaSpec` dataclass;`Task.from_json(path)`/`to_json(path)`、`Result.from_json(path)`/`to_json(path)`。Task 2/3/4/5/6 全依赖。

- [ ] **Step 1: 建 `compute_unit/__init__.py` + `tests/compute_unit/__init__.py`**

`compute_unit/__init__.py`:
```python
# -*- coding: utf-8 -*-
"""Mac 远程计算单元:discovery 参数回测的跨机离线执行包。

定位(spec §3.1):Mac(封闭工作机·只 git pull)作 discovery 探索试验台,结果不回库。
只借 discovery.objective.evaluate / snapshot.freeze / split.holdout_split 三纯函数(C2)。
"""
```

`tests/compute_unit/__init__.py`:空文件。

- [ ] **Step 2: 写 `test_protocol.py` 失败测试**

`tests/compute_unit/test_protocol.py`:
```python
# -*- coding: utf-8 -*-
"""protocol.py 序列化往返测试:date↔str、嵌套 split/snapshot/trials、三种 status。"""
from datetime import date

from compute_unit.protocol import (
    Task, Result, TrialSpec, TrialResult, SegmentSpec, SplitSpec, SnapshotMetaSpec,
)


def _sample_task():
    """构造一个完整 Task 样本(覆盖所有字段)。"""
    return Task(
        protocol_version=1, task_id="a1b2c3d4", created_at="2026-07-26T12:00:00",
        git_commit="a" * 40, engine_hash="abc123def456", parquet_sha256="b" * 64,
        lake_start="2025-01-01", embargo_days=5,
        snapshot_meta=SnapshotMetaSpec(
            snapshot_hash="snap1234abcd5678", universe_def="创板科创",
            universe_count=1334, date_range="2025-01-01~2026-07-25", lake_start="2025-01-01",
        ),
        split=SplitSpec(
            inner=SegmentSpec("inner_2025", date(2025, 1, 1), date(2025, 12, 31)),
            outer=SegmentSpec("outer_2026", date(2026, 1, 1), date(2026, 12, 31)),
            embargo_days=5,
        ),
        trials=[TrialSpec(trial_id="tid000000001", params={"window": 80},
                          source="discovery_search", seed=42)],
    )


def test_task_roundtrip(tmp_path):
    """Task → json → Task 字段全等(date 还原为 date 对象,非 str)。"""
    t = _sample_task()
    out = tmp_path / "task.json"
    t.to_json(out)
    t2 = Task.from_json(out)
    assert t2.task_id == t.task_id
    assert t2.git_commit == t.git_commit
    assert t2.snapshot_meta.universe_count == 1334
    assert t2.snapshot_meta.universe_def == "创板科创"          # 中文 ensure_ascii=False 保真
    assert t2.split.inner.start == date(2025, 1, 1)             # date 对象,非 str
    assert isinstance(t2.split.inner.start, date)
    assert t2.split.embargo_days == 5
    assert t2.trials[0].trial_id == "tid000000001"
    assert t2.trials[0].params == {"window": 80}
    assert t2.trials[0].seed == 42


def test_result_roundtrip(tmp_path):
    """Result → json → Result,含 failed/degenerate/ok 三种 status。"""
    r = Result(
        task_id="a1b2c3d4", git_commit="a" * 40, parquet_sha256="b" * 64,
        ran_at="2026-07-26T20:00:00",
        results=[
            TrialResult(trial_id="ok1", status="ok",
                        inner={"n": 10, "calmar": 5.0}, outer={"n": 8}, n_total=18),
            TrialResult(trial_id="fail1", status="failed", error="KeyError"),
            TrialResult(trial_id="deg1", status="degenerate", n_total=0),
        ],
    )
    out = tmp_path / "result.json"
    r.to_json(out)
    r2 = Result.from_json(out)
    assert len(r2.results) == 3
    assert r2.results[0].inner["calmar"] == 5.0
    assert r2.results[1].status == "failed" and r2.results[1].error == "KeyError"
    assert r2.results[2].status == "degenerate" and r2.results[2].n_total == 0
```

- [ ] **Step 3: 跑测试,确认 ImportError 失败**

Run: `F:/quanter/.venv310/Scripts/python.exe -m pytest tests/compute_unit/test_protocol.py -v`
Expected: FAIL(`ModuleNotFoundError: No module named 'compute_unit.protocol'`)。

- [ ] **Step 4: 实现 `compute_unit/protocol.py`**

```python
# -*- coding: utf-8 -*-
"""task.json / result.json 协议:dataclass + JSON 序列化。

物理定位(C3):纯数据载体,处理 date↔str、确保中文 ensure_ascii=False 保真。
compute_unit(Mac)与 task_export(Win)共用同一协议,保证跨机字节级一致。

设计:Task/Result 是 dataclass;SegmentSpec 的 date 字段在 JSON 里序列化为
"YYYY-MM-DD" str(isoformat),反序列化 date.fromisoformat 还原为 datetime.date。
metrics dict(inner/outer)原样透传(数值已是原生 float,evaluate 直出)。
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path


# ── task.json 内嵌的 split 段(对应 discovery.split.Segment)──
@dataclass
class SegmentSpec:
    """一段日期区间(inner test / outer holdout)的序列化形态。"""
    name: str
    start: date
    end: date

    def to_dict(self) -> dict:
        return {"name": self.name, "start": self.start.isoformat(), "end": self.end.isoformat()}

    @classmethod
    def from_dict(cls, d: dict) -> "SegmentSpec":
        return cls(name=d["name"], start=date.fromisoformat(d["start"]),
                   end=date.fromisoformat(d["end"]))


@dataclass
class SplitSpec:
    """HoldoutSplit 的序列化形态(inner + outer + embargo_days)。"""
    inner: SegmentSpec
    outer: SegmentSpec
    embargo_days: int

    def to_dict(self) -> dict:
        return {"inner": self.inner.to_dict(), "outer": self.outer.to_dict(),
                "embargo_days": self.embargo_days}

    @classmethod
    def from_dict(cls, d: dict) -> "SplitSpec":
        return cls(inner=SegmentSpec.from_dict(d["inner"]),
                   outer=SegmentSpec.from_dict(d["outer"]),
                   embargo_days=d["embargo_days"])


@dataclass
class SnapshotMetaSpec:
    """discovery.snapshot.SnapshotMeta 的序列化形态(Win 权威,Mac 不重算)。"""
    snapshot_hash: str
    universe_def: str
    universe_count: int
    date_range: str
    lake_start: str

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "SnapshotMetaSpec":
        return cls(**d)


@dataclass
class TrialSpec:
    """单条 trial:Win 端预算的 trial_id + params + source + seed(C8)。"""
    trial_id: str
    params: dict
    source: str
    seed: int

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "TrialSpec":
        return cls(**d)


@dataclass
class Task:
    """task.json 的反序列化形态(Mac 端读取)。

    Win 端 task_export 写入,Mac 端 compute_unit 读取。三件哈希(git_commit/engine_hash/
    parquet_sha256)+ snapshot_meta 是 env_check 校验输入(C4)。
    """
    protocol_version: int
    task_id: str
    created_at: str
    git_commit: str
    engine_hash: str
    parquet_sha256: str
    lake_start: str
    embargo_days: int
    snapshot_meta: SnapshotMetaSpec
    split: SplitSpec
    trials: list

    def to_dict(self) -> dict:
        return {
            "protocol_version": self.protocol_version, "task_id": self.task_id,
            "created_at": self.created_at, "git_commit": self.git_commit,
            "engine_hash": self.engine_hash, "parquet_sha256": self.parquet_sha256,
            "lake_start": self.lake_start, "embargo_days": self.embargo_days,
            "snapshot_meta": self.snapshot_meta.to_dict(), "split": self.split.to_dict(),
            "trials": [t.to_dict() for t in self.trials],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Task":
        return cls(
            protocol_version=d["protocol_version"], task_id=d["task_id"],
            created_at=d["created_at"], git_commit=d["git_commit"],
            engine_hash=d["engine_hash"], parquet_sha256=d["parquet_sha256"],
            lake_start=d["lake_start"], embargo_days=d["embargo_days"],
            snapshot_meta=SnapshotMetaSpec.from_dict(d["snapshot_meta"]),
            split=SplitSpec.from_dict(d["split"]),
            trials=[TrialSpec.from_dict(t) for t in d["trials"]],
        )

    @classmethod
    def from_json(cls, path) -> "Task":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    def to_json(self, path) -> None:
        # ensure_ascii=False 保中文(universe_def 等);indent=2 pretty 便于人读/git diff
        Path(path).write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")


# ── result.json ──
@dataclass
class TrialResult:
    """单条 trial 评估结果。status: ok | failed | degenerate。"""
    trial_id: str
    status: str
    inner: dict = field(default_factory=dict)
    outer: dict = field(default_factory=dict)
    n_total: int = 0
    error: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "TrialResult":
        return cls(**d)


@dataclass
class Result:
    """result.json 的序列化形态(Mac 本地,不回传)。"""
    task_id: str
    git_commit: str
    parquet_sha256: str
    ran_at: str
    results: list

    def to_dict(self) -> dict:
        return {"task_id": self.task_id, "git_commit": self.git_commit,
                "parquet_sha256": self.parquet_sha256, "ran_at": self.ran_at,
                "results": [r.to_dict() for r in self.results]}

    @classmethod
    def from_dict(cls, d: dict) -> "Result":
        return cls(task_id=d["task_id"], git_commit=d["git_commit"],
                   parquet_sha256=d["parquet_sha256"], ran_at=d["ran_at"],
                   results=[TrialResult.from_dict(r) for r in d["results"]])

    def to_json(self, path) -> None:
        Path(path).write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def from_json(cls, path) -> "Result":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
```

- [ ] **Step 5: 跑测试,确认通过**

Run: `F:/quanter/.venv310/Scripts/python.exe -m pytest tests/compute_unit/test_protocol.py -v`
Expected: 2 PASS。

- [ ] **Step 6: 提交**

```bash
git add compute_unit/__init__.py compute_unit/protocol.py tests/compute_unit/__init__.py tests/compute_unit/test_protocol.py
git commit -m "feat(compute_unit): protocol.py — Task/Result dataclass + JSON 序列化(date↔str)

- SegmentSpec/SplitSpec/SnapshotMetaSpec/TrialSpec/Task/TrialResult/Result 七 dataclass
- date 字段 isoformat/fromisoformat 往返;ensure_ascii=False 保中文
- Task/Result 的 from_json/to_json 文件 IO
- 序列化往返单测 2 例(task 含嵌套 split/snapshot/trials;result 含 ok/failed/degenerate)

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 2: env_check.py —— 三件哈希 + snapshot 双校验

**Files:**
- Create: `compute_unit/env_check.py`
- Test: `tests/compute_unit/test_env_check.py`

**Interfaces:**
- Consumes: Task 1 的 `Task`/`SnapshotMetaSpec`;`discovery.snapshot.freeze`(延迟 import,只读)。
- Produces: `EnvDriftError`、`_check_hashes(task)`、`_check_snapshot(task, meta)`、`verify_and_freeze(task) -> (universe, meta)`、`verify(task)`、`_git_head_sha()`、`_file_sha256(path)`、`_engine_hash()`、`_parquet_path()`。Task 3 的 `runner.run` 用 `verify_and_freeze`;Task 6 的 `task_export` 复用同款哈希算法。

- [ ] **Step 1: 写 `test_env_check.py` 失败测试**

`tests/compute_unit/test_env_check.py`:
```python
# -*- coding: utf-8 -*-
"""env_check 校验测试:三件哈希漂移分支 + snapshot 双校验分支 + verify_and_freeze 串联。

mock git/文件 sha(不依赖真实 parquet/git),各漂移分支断言抛 EnvDriftError。
"""
from datetime import date
from types import SimpleNamespace

import pytest

from compute_unit import env_check
from compute_unit.env_check import EnvDriftError
from compute_unit.protocol import Task, SnapshotMetaSpec, SplitSpec, SegmentSpec


def _task_with(git_commit="a"*40, engine_hash="abc123def456", parquet_sha256="b"*64,
               universe_count=1334, date_range="2025-01-01~2026-07-25"):
    """构造 Task,允许覆盖各哈希/snapshot 字段(测漂移分支)。"""
    return Task(
        protocol_version=1, task_id="t1", created_at="x", git_commit=git_commit,
        engine_hash=engine_hash, parquet_sha256=parquet_sha256,
        lake_start="2025-01-01", embargo_days=5,
        snapshot_meta=SnapshotMetaSpec("snap1234abcd5678", "创板科创", universe_count,
                                       date_range, "2025-01-01"),
        split=SplitSpec(SegmentSpec("i", date(2025, 1, 1), date(2025, 12, 31)),
                        SegmentSpec("o", date(2026, 1, 1), date(2026, 12, 31)), 5),
        trials=[],
    )


def test_check_hashes_git_drift(monkeypatch):
    """git_commit 不符 → EnvDriftError。"""
    monkeypatch.setattr(env_check, "_git_head_sha", lambda: "z"*40)
    monkeypatch.setattr(env_check, "_engine_hash", lambda: "abc123def456")
    monkeypatch.setattr(env_check, "_file_sha256", lambda p: "b"*64)
    with pytest.raises(EnvDriftError, match="git_commit"):
        env_check._check_hashes(_task_with(git_commit="a"*40))


def test_check_hashes_engine_drift(monkeypatch):
    """engine_hash 不符 → EnvDriftError。"""
    monkeypatch.setattr(env_check, "_git_head_sha", lambda: "a"*40)
    monkeypatch.setattr(env_check, "_engine_hash", lambda: "XXXXXXXXXXXX")
    monkeypatch.setattr(env_check, "_file_sha256", lambda p: "b"*64)
    with pytest.raises(EnvDriftError, match="engine_hash"):
        env_check._check_hashes(_task_with(engine_hash="abc123def456"))


def test_check_hashes_parquet_drift(monkeypatch):
    """parquet_sha256 不符 → EnvDriftError。"""
    monkeypatch.setattr(env_check, "_git_head_sha", lambda: "a"*40)
    monkeypatch.setattr(env_check, "_engine_hash", lambda: "abc123def456")
    monkeypatch.setattr(env_check, "_file_sha256", lambda p: "Z"*64)
    with pytest.raises(EnvDriftError, match="parquet"):
        env_check._check_hashes(_task_with(parquet_sha256="b"*64))


def test_check_hashes_all_match(monkeypatch):
    """三件全匹配 → 不抛(None)。"""
    monkeypatch.setattr(env_check, "_git_head_sha", lambda: "a"*40)
    monkeypatch.setattr(env_check, "_engine_hash", lambda: "abc123def456")
    monkeypatch.setattr(env_check, "_file_sha256", lambda p: "b"*64)
    env_check._check_hashes(_task_with())   # 不抛即通过


def test_check_snapshot_count_drift():
    """universe_count 不符 → EnvDriftError。"""
    meta = SimpleNamespace(universe_count=999, date_range="2025-01-01~2026-07-25")
    with pytest.raises(EnvDriftError, match="universe_count"):
        env_check._check_snapshot(_task_with(universe_count=1334), meta)


def test_check_snapshot_date_range_drift():
    """date_range 不符 → EnvDriftError。"""
    meta = SimpleNamespace(universe_count=1334, date_range="2025-01-01~2026-01-01")
    with pytest.raises(EnvDriftError, match="date_range"):
        env_check._check_snapshot(_task_with(date_range="2025-01-01~2026-07-25"), meta)


def test_verify_and_freeze_chains(monkeypatch):
    """verify_and_freeze = _check_hashes + freeze + _check_snapshot,返回 (universe, meta)。"""
    calls = []
    monkeypatch.setattr(env_check, "_check_hashes", lambda t: calls.append("hashes"))
    monkeypatch.setattr(env_check, "_check_snapshot", lambda t, m: calls.append("snapshot"))
    fake_meta = SimpleNamespace(universe_count=1334, date_range="2025-01-01~2026-07-25")
    monkeypatch.setattr("discovery.snapshot.freeze", lambda lake_start="x": ({"u": 1}, fake_meta))
    universe, meta = env_check.verify_and_freeze(_task_with())
    assert calls == ["hashes", "snapshot"]   # 串联顺序
    assert universe == {"u": 1}               # 返回 freeze 的 universe 供 runner 复用
```

- [ ] **Step 2: 跑测试,确认 ImportError 失败**

Run: `F:/quanter/.venv310/Scripts/python.exe -m pytest tests/compute_unit/test_env_check.py -v`
Expected: FAIL(`ModuleNotFoundError: No module named 'compute_unit.env_check'`)。

- [ ] **Step 3: 实现 `compute_unit/env_check.py`**

```python
# -*- coding: utf-8 -*-
"""跨机一致性校验:三件哈希 + snapshot 双校验(C4)。

基石物理意图:discovery 的可比性靠 snapshot_hash + engine_hash + trial_id 三件套。Mac
计算单元必须保证这三件与 Win 字节级一致,否则 Mac 跑的是脏数据,摘要里的 calmar 和 Win
补跑的对不上,整套筛选失去意义。任一漂移 → EnvDriftError,compute_unit 拒跑(退出码 3)。

两层校验:
- _check_hashes:git_commit / engine_hash / parquet_sha256 三件(纯文件 + git,不依赖 freeze)
- _check_snapshot:Mac 本地 freeze 重算 universe_count / date_range vs task.snapshot_meta
  (捕获哈希漏掉的逻辑层漂移:is_target_board / load_universe 流动性阈值改了)
"""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from compute_unit.protocol import Task


class EnvDriftError(RuntimeError):
    """环境漂移(代码/数据/快照不一致),compute_unit 拒跑(退出码 3)。"""


# 项目根(compute_unit/ 的上级 = quanter/):算 parquet/engine 哈希的基准
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _git_head_sha() -> str:
    """本地当前 HEAD commit sha(git rev-parse HEAD)。失败返空串(让校验报漂移)。"""
    try:
        r = subprocess.run(["git", "rev-parse", "HEAD"],
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""


def _file_sha256(path: Path) -> str:
    """文件全内容 sha256(64 hex),64KB 块流式读(省内存,parquet 435MB)。不存在返空串。"""
    path = Path(path)
    if not path.exists():
        return ""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _engine_hash() -> str:
    """回测内核指纹:backtest.py + method_v0.py 内容 sha256[:12]。

    与 discovery/runner.py:_engine_hash 同款算法(本地重声明避免循环 import)。
    内核一动(engine_hash 变),Mac 与 Win 不可比。
    """
    from strategies.neckline import backtest, method_v0
    h = hashlib.sha256()
    for f in (backtest.__file__, method_v0.__file__):
        with open(f, "rb") as fh:
            h.update(fh.read())
    return h.hexdigest()[:12]


def _parquet_path() -> Path:
    """a_shares_daily.parquet 路径(与 discovery.snapshot.LAKE_PATH 同源)。"""
    return PROJECT_ROOT / "data_lake" / "a_shares_daily.parquet"


def _check_hashes(task: Task) -> None:
    """三件哈希校验:git_commit / engine_hash / parquet_sha256。任一不符抛 EnvDriftError。"""
    # ① git commit(代码版本)
    head = _git_head_sha()
    if head != task.git_commit:
        raise EnvDriftError(
            f"git_commit 漂移:Mac 本地 {head[:12]} ≠ task {task.git_commit[:12]},请 git pull")
    # ② engine hash(回测内核代码指纹)
    eng = _engine_hash()
    if eng != task.engine_hash:
        raise EnvDriftError(
            f"engine_hash 漂移:Mac 本地 {eng} ≠ task {task.engine_hash}"
            "(backtest.py/method_v0.py 不一致,请 git pull)")
    # ③ parquet sha256(数据指纹)
    pq = _file_sha256(_parquet_path())
    if pq != task.parquet_sha256:
        raise EnvDriftError(
            f"parquet_sha256 漂移:Mac 本地 {pq[:12]}… ≠ task {task.parquet_sha256[:12]}…"
            "(数据滞后或损坏,请 git pull 最新 parquet)")


def _check_snapshot(task: Task, meta) -> None:
    """snapshot 双校验:Mac freeze 出的 universe_count/date_range vs task.snapshot_meta。

    meta: discovery.snapshot.SnapshotMeta(freeze 返回)。捕获哈希漏掉的逻辑层漂移。
    """
    if meta.universe_count != task.snapshot_meta.universe_count:
        raise EnvDriftError(
            f"universe_count 漂移:Mac freeze {meta.universe_count} "
            f"≠ task {task.snapshot_meta.universe_count}")
    if meta.date_range != task.snapshot_meta.date_range:
        raise EnvDriftError(
            f"date_range 漂移:Mac {meta.date_range} ≠ task {task.snapshot_meta.date_range}")


def verify_and_freeze(task: Task):
    """校验 + freeze,返回 (universe, meta)。runner.run 复用,避免重复 freeze(~5s/次)。

    流程:_check_hashes(纯文件/git)→ freeze(lake_start)重算 meta → _check_snapshot(meta)。
    漂移任一环节抛 EnvDriftError。通过则返回 universe 供后续跑批复用(不重读 parquet)。
    """
    _check_hashes(task)
    from discovery.snapshot import freeze
    universe, meta = freeze(lake_start=task.lake_start)
    _check_snapshot(task, meta)
    return universe, meta


def verify(task: Task) -> None:
    """完整校验(CLI verify 子命令用,只校验不跑批)。"""
    verify_and_freeze(task)   # 丢弃返回的 universe
```

- [ ] **Step 4: 跑测试,确认通过**

Run: `F:/quanter/.venv310/Scripts/python.exe -m pytest tests/compute_unit/test_env_check.py -v`
Expected: 7 PASS。

- [ ] **Step 5: 提交**

```bash
git add compute_unit/env_check.py tests/compute_unit/test_env_check.py
git commit -m "feat(compute_unit): env_check — 三件哈希 + snapshot 双校验(C4 跨机一致性)

- EnvDriftError + _check_hashes(git_commit/engine_hash/parquet_sha256)
- _check_snapshot(Mac freeze 重算 universe_count/date_range vs task)
- verify_and_freeze 串联两层 + 返回 universe 供 runner 复用(避免重复 freeze)
- _file_sha256 64KB 块流式读(435MB parquet 省内存)
- 7 例单测:三件各漂移分支 + snapshot 双漂移 + 全匹配 + verify 串联

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 3: runner.py —— 跑批核心(spawn Pool + evaluate)+ conftest fixture

**Files:**
- Create: `compute_unit/runner.py`、`tests/compute_unit/conftest.py`、`tests/compute_unit/test_runner.py`

**Interfaces:**
- Consumes: Task 1 的 `Task`/`TrialSpec`/`SplitSpec`/`Result`/`TrialResult`;Task 2 的 `verify_and_freeze`;`discovery.objective.evaluate`、`discovery.split.HoldoutSplit`/`Segment`。
- Produces: `_eval_one(trial, universe, split) -> TrialResult`(纯函数)、`_eval_worker(trial)`(spawn 包装)、`_init_worker(universe, split_spec)`、`_eval_batch(task, universe, split_spec, n_proc) -> list[TrialResult]`、`run(task, n_proc) -> Result`。Task 5 的 `__main__` 用 `run`;Task 8 的 e2e 用 `_eval_batch`。

> **关键技术点(C7)**:`mp.spawn` 子进程**不继承父进程 monkeypatch**。故可 mock 的纯函数 `_eval_one(trial, universe, split)`(同进程,monkeypatch evaluate 生效)与 spawn 包装 `_eval_worker(trial)`(读 `_WORKER_STATE`,子进程跑真实 evaluate)拆分。测试 mock 走 `_eval_one`;Pool 集成测试跑真实 evaluate(子进程直跑,不 mock,与父进程直跑等价)。

- [ ] **Step 1: 建 `tests/compute_unit/conftest.py`(合成 fixture,与 tests/discovery/conftest.py 同口径)**

```python
# -*- coding: utf-8 -*-
"""compute_unit 测试共享 fixture。合成数据,不依赖真实 data_lake(快)。

与 tests/discovery/conftest.py 同口径(synth_sym_df 复制同款),保证 runner 等价红线测试
(C6:compute_unit 结果 == discovery evaluate 直跑)用同一份合成 universe,口径可信。
"""
import numpy as np
import pandas as pd
import pytest


@pytest.fixture
def synth_sym_df():
    """合成单标的 OHLCV(250 根日线 2025 全年),与 tests/discovery/conftest.py 同款。

    价格平稳上行 + 噪声,让颈线法有信号又不退化。列对齐 scan_symbol 期望。
    """
    idx = pd.bdate_range("2025-01-01", periods=250)
    rng = np.random.default_rng(42)
    close = 10.0 + np.cumsum(rng.normal(0.02, 0.3, 250))
    high = close * (1 + rng.uniform(0, 0.03, 250))
    low = close * (1 - rng.uniform(0, 0.03, 250))
    opn = close + rng.normal(0, 0.1, 250)
    return pd.DataFrame({
        "open": opn, "high": high, "low": low, "close": close,
        "volume": rng.integers(1e6, 1e7, 250),
        "amount": rng.integers(1e7, 1e8, 250),   # 千元单位,≥1e5=1亿
    }, index=idx)


@pytest.fixture
def fixed_params():
    """固定 21 维 params(确定性,不读 logs/param_iter_state.json)。

    值对齐 discovery 冠军 fallback(tests/discovery/conftest.py:champion_params fallback)。
    runner 等价测试用此,保证跨实现口径一致。
    """
    return {
        "window": 80, "min_touches": 2, "min_suppression": 0.5,
        "local_extrema_window": 3, "min_bottoms": 3, "breakout_vol_mult": 1.0,
        "min_rr": 2.0, "max_h_atr": 4.0, "stop_atr_mult": 1.5, "tp_h_mult": 1.5,
        "decay_tau": 30,
        "max_holding": 15, "max_wait": 8, "cooldown": 5, "buy_limit_atr_mult": 1.5,
        "tp1_h_mult": 1.5, "tp1_portion": 0.3, "cancel_thresh_mult": 2.0,
        "trailing_grace": 0, "trailing_step": 0.15, "trailing_floor": 0.0,
    }


@pytest.fixture
def synth_universe(synth_sym_df):
    """合成 universe(1 只标的),供 _eval_batch / _eval_one 注入。"""
    return {"300001.SZ": synth_sym_df}
```

- [ ] **Step 2: 写 `test_runner.py` 失败测试**

`tests/compute_unit/test_runner.py`:
```python
# -*- coding: utf-8 -*-
"""runner 测试:_eval_one/_eval_batch 与 discovery.objective.evaluate 逐字段等价(C6 红线)。

_eval_one 同进程(mock 可生效);_eval_batch 走 spawn Pool(子进程跑真实 evaluate,与父进程
直跑等价)。注入合成 universe,不依赖真实 parquet,快。
"""
from datetime import date

from compute_unit.protocol import Task, TrialSpec, SnapshotMetaSpec, SplitSpec, SegmentSpec


def _task_for(trial_id, params, seed=42):
    """构造含 1 trial 的 Task(供 _eval_batch / _eval_one)。"""
    return Task(
        protocol_version=1, task_id="t1", created_at="x", git_commit="a"*40,
        engine_hash="x", parquet_sha256="x",
        lake_start="2025-01-01", embargo_days=5,
        snapshot_meta=SnapshotMetaSpec("s", "u", 1, "dr", "2025-01-01"),
        split=SplitSpec(SegmentSpec("inner_2025", date(2025, 1, 1), date(2025, 12, 31)),
                        SegmentSpec("outer_2026", date(2026, 1, 1), date(2026, 12, 31)), 5),
        trials=[TrialSpec(trial_id=trial_id, params=params, source="discovery_search", seed=seed)],
    )


def test_eval_one_equivalent(fixed_params, synth_universe):
    """红线 C6:_eval_one(trial, universe, split) inner/outer/n_total == evaluate 直跑。"""
    from compute_unit.runner import _eval_one
    from discovery.objective import evaluate
    from discovery.split import holdout_split
    split = holdout_split()
    trial = TrialSpec("tid_eq", fixed_params, "discovery_search", 42)
    r = _eval_one(trial, synth_universe, split)
    direct = evaluate(fixed_params, synth_universe, split)
    assert r.status == "ok"
    assert r.inner == direct["inner"]       # 逐字段相等(红线)
    assert r.outer == direct["outer"]
    assert r.n_total == direct["n_total"]


def test_eval_one_failed(monkeypatch, fixed_params, synth_universe):
    """单组 evaluate 异常 → status=failed(同进程 mock 生效)。"""
    from compute_unit import runner
    from compute_unit.protocol import TrialSpec
    from discovery.split import holdout_split
    monkeypatch.setattr("discovery.objective.evaluate",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    trial = TrialSpec("tid_fail", fixed_params, "discovery_search", 42)
    r = runner._eval_one(trial, synth_universe, holdout_split())
    assert r.status == "failed"
    assert "boom" in r.error


def test_eval_one_degenerate(monkeypatch, fixed_params, synth_universe):
    """n_total==0(退化)→ status=degenerate。"""
    from compute_unit import runner
    from compute_unit.protocol import TrialSpec
    from discovery.split import holdout_split
    monkeypatch.setattr("discovery.objective.evaluate",
                        lambda p, u, s: {"inner": {}, "outer": {}, "n_total": 0})
    trial = TrialSpec("tid_deg", fixed_params, "discovery_search", 42)
    r = runner._eval_one(trial, synth_universe, holdout_split())
    assert r.status == "degenerate"
    assert r.n_total == 0


def test_eval_batch_equivalent(fixed_params, synth_universe):
    """_eval_batch(spawn Pool,真实 evaluate)inner == 父进程 evaluate 直跑(红线 C6)。

    子进程跑真实 evaluate(不 mock),与父进程同 universe 同 split,结果必然一致。
    """
    from compute_unit.runner import _eval_batch
    from discovery.objective import evaluate
    from discovery.split import holdout_split
    task = _task_for("tid_batch", fixed_params)
    results = _eval_batch(task, synth_universe, task.split, n_proc=1)
    direct = evaluate(fixed_params, synth_universe, holdout_split())
    assert len(results) == 1
    assert results[0].status == "ok"
    assert results[0].inner == direct["inner"]


def test_eval_batch_empty_trials():
    """空 trials → [](不起 Pool,省 spawn 开销)。"""
    from compute_unit.runner import _eval_batch
    task = _task_for("x", {})
    task.trials = []
    assert _eval_batch(task, {}, task.split, n_proc=1) == []
```

- [ ] **Step 3: 跑测试,确认 ImportError 失败**

Run: `F:/quanter/.venv310/Scripts/python.exe -m pytest tests/compute_unit/test_runner.py -v`
Expected: FAIL(`ModuleNotFoundError: No module named 'compute_unit.runner'`)。

- [ ] **Step 4: 实现 `compute_unit/runner.py`**

```python
# -*- coding: utf-8 -*-
"""跑批核心:verify_and_freeze → spawn Pool 并行 evaluate → 拼 Result。

物理意图(spec §9.2 拷问②):一组 params × 全 universe ~720s 串行;一批 N 组用 mp.spawn
Pool 分到 (核数-2) 子进程并发,吞吐 ×(核数-2)。完全沿用 discovery.worker 的 spawn 四铁律
(C7,Win/Linux/Mac 跨平台一致):
1. _init_worker/_eval_worker/_eval_one 顶层定义(spawn pickle 函数引用,嵌套/lambda 不可 pickle)
2. 子进程重新 import 本模块——顶层无副作用(_WORKER_STATE 初始 ready=False 占位)
3. universe 经 initializer 注入子进程模块全局,不随每 params pickle(否则 455MB 每次 map 爆掉)
4. _eval_one 捕获单组异常 → failed;n_total==0 退化 → degenerate(对应 _eval_worker 返 None 语义)

可测试拆分:_eval_one 是纯函数(注入 universe+split,同进程 mock evaluate 生效);_eval_worker
是 spawn 包装(读 _WORKER_STATE);_eval_batch 注入 universe(单测不依赖 freeze/parquet)。
"""
from __future__ import annotations

import multiprocessing as mp
import os
from datetime import date

from compute_unit.protocol import Task, Result, TrialResult, SplitSpec


# 子进程模块全局:initializer 一次注入后填充(主进程也 import 本模块但读 ready=False 占位,
# 主进程不调 _eval_worker)。
_WORKER_STATE = {"universe": None, "split": None, "ready": False}


def _split_to_discovery(split_spec: SplitSpec):
    """protocol.SplitSpec → discovery.split.HoldoutSplit(evaluate 需要的类型)。

    date 字段原样透传(SplitSpec.start/end 已是 datetime.date)。
    """
    from discovery.split import HoldoutSplit, Segment
    return HoldoutSplit(
        inner=Segment(split_spec.inner.name, split_spec.inner.start, split_spec.inner.end),
        outer=Segment(split_spec.outer.name, split_spec.outer.start, split_spec.outer.end),
        embargo_days=split_spec.embargo_days,
    )


def _eval_one(trial, universe, split) -> TrialResult:
    """评估单 trial(纯函数,无全局状态)。_eval_worker 是它的 spawn 包装。

    两类降级(C7):
    - 异常 → status=failed(单 trial 崩溃不阻断批,对应 discovery._eval_worker 返 None)
    - n_total==0 → status=degenerate(params 退化,全 universe 挂单区间空)
    """
    from discovery.objective import evaluate
    try:
        res = evaluate(trial.params, universe, split)
    except Exception as e:
        return TrialResult(trial_id=trial.trial_id, status="failed", error=repr(e)[:200])
    if res.get("n_total", 0) == 0:
        return TrialResult(trial_id=trial.trial_id, status="degenerate", n_total=0)
    return TrialResult(trial_id=trial.trial_id, status="ok",
                       inner=res["inner"], outer=res["outer"], n_total=res["n_total"])


def _init_worker(universe, split_spec: SplitSpec):
    """Pool initializer:子进程启动时一次注入 universe + split 到 _WORKER_STATE。

    顶层定义(spawn 可 pickle)。universe 是 dict(主进程 freeze 好传入),不重读 parquet。
    split 经 _split_to_discovery 转 HoldoutSplit(evaluate 需要的类型)。
    """
    _WORKER_STATE["universe"] = universe
    _WORKER_STATE["split"] = _split_to_discovery(split_spec)
    _WORKER_STATE["ready"] = True


def _eval_worker(trial) -> TrialResult:
    """Pool.map 调用:读 _WORKER_STATE → _eval_one。顶层定义(spawn 可 pickle)。"""
    if not _WORKER_STATE["ready"]:
        return TrialResult(trial_id=trial.trial_id, status="failed", error="worker 未就绪")
    return _eval_one(trial, _WORKER_STATE["universe"], _WORKER_STATE["split"])


def _default_n_proc() -> int:
    """默认进程数 = 核数 - 2(留 2 核给系统/主进程,与 discovery.worker._default_n_proc 同)。"""
    return max(1, (os.cpu_count() or 4) - 2)


def _eval_batch(task: Task, universe: dict, split_spec: SplitSpec,
                n_proc: int | None = None) -> list:
    """纯计算:spawn Pool 并行评估 task.trials → list[TrialResult]。

    可测试入口(注入 universe,不依赖 freeze/parquet)。生产入口 run() 调它。
    n_proc=None → 核数-2;clamp 到不超过任务数(开 8 进程跑 2 任务无意义)。
    空 trials 返 [](不起 Pool,省 spawn 开销)。
    """
    if not task.trials:
        return []
    if n_proc is None:
        n_proc = _default_n_proc()
    n_proc = min(n_proc, len(task.trials))
    ctx = mp.get_context("spawn")   # 跨平台一致(Win 默认 spawn,Linux/Mac 显式更安全,不踩 fork 坑)
    with ctx.Pool(processes=n_proc, initializer=_init_worker,
                  initargs=(universe, split_spec)) as pool:
        results = pool.map(_eval_worker, task.trials)
    return results


def run(task: Task, n_proc: int | None = None) -> Result:
    """生产入口:verify_and_freeze(校验+freeze 复用)→ _eval_batch → 拼 Result。

    verify_and_freeze 漂移抛 EnvDriftError(__main__ 捕获返退出码 3)。返回的 universe 直接
    喂 _eval_batch(不重复 freeze,省 ~5s)。
    """
    from compute_unit.env_check import verify_and_freeze
    from datetime import datetime
    universe, _meta = verify_and_freeze(task)
    trial_results = _eval_batch(task, universe, task.split, n_proc=n_proc)
    return Result(
        task_id=task.task_id,
        git_commit=task.git_commit,
        parquet_sha256=task.parquet_sha256,
        ran_at=datetime.utcnow().isoformat(),
        results=trial_results,
    )
```

- [ ] **Step 5: 跑测试,确认通过**

Run: `F:/quanter/.venv310/Scripts/python.exe -m pytest tests/compute_unit/test_runner.py -v`
Expected: 5 PASS。

- [ ] **Step 6: 提交**

```bash
git add compute_unit/runner.py tests/compute_unit/conftest.py tests/compute_unit/test_runner.py
git commit -m "feat(compute_unit): runner — spawn Pool 并行 evaluate + 等价红线 C6

- _eval_one 纯函数(注入 universe+split,可同进程 mock)+ _eval_worker spawn 包装
- _eval_batch mp.spawn Pool,universe 经 initializer 注入模块全局(不随 params pickle,C7)
- run = verify_and_freeze + _eval_batch + 拼 Result(复用 universe 不重复 freeze)
- failed/degenerate/empty 三类降级单测 + _eval_one/_eval_batch 与 evaluate 逐字段等价(C6)
- conftest 合成 fixture 与 tests/discovery 同口径(synth_sym_df/fixed_params/synth_universe)

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 4: summary.py —— top-N 人可读摘要

**Files:**
- Create: `compute_unit/summary.py`
- Test: `tests/compute_unit/test_summary.py`

**Interfaces:**
- Consumes: Task 1 的 `Result`。
- Produces: `summarize(result, top_n=3) -> str`、`_fmt_metrics(m, prefix) -> str`。Task 5 的 `__main__` 用 `summarize`。

- [ ] **Step 1: 写 `test_summary.py` 失败测试**

`tests/compute_unit/test_summary.py`:
```python
# -*- coding: utf-8 -*-
"""summary 测试:top-N 按 inner calmar 降序 + 中文格式 + 诊断计数 + 无 ok 兜底。"""
from compute_unit.protocol import Result, TrialResult
from compute_unit.summary import summarize


def _result_with(*trials):
    return Result(task_id="t1", git_commit="a"*40, parquet_sha256="b"*64,
                  ran_at="x", results=list(trials))


def test_summarize_top_n_by_inner_calmar():
    """top-N 按 inner calmar 降序,仅 ok 的 trial。"""
    r = _result_with(
        TrialResult("low", "ok",
                    inner={"n": 10, "ann": 0.1, "calmar": 1.0, "max_dd": 0.1},
                    outer={"n": 8}, n_total=18),
        TrialResult("high", "ok",
                    inner={"n": 20, "ann": 0.3, "calmar": 5.0, "max_dd": 0.06},
                    outer={"n": 15}, n_total=35),
        TrialResult("mid", "ok",
                    inner={"n": 15, "ann": 0.2, "calmar": 3.0, "max_dd": 0.07},
                    outer={"n": 10}, n_total=25),
    )
    out = summarize(r, top_n=2)
    assert out.index("high") < out.index("mid")   # calmar5 在 calmar3 前
    assert "low" not in out                        # top-2 不含 low(calmar1)


def test_summarize_skips_failed_degenerate():
    """failed/degenerate 不进 top,末尾诊断计数展示。"""
    r = _result_with(
        TrialResult("ok1", "ok",
                    inner={"n": 5, "ann": 0.1, "calmar": 2.0, "max_dd": 0.05},
                    outer={"n": 4}, n_total=9),
        TrialResult("fail1", "failed", error="boom"),
        TrialResult("deg1", "degenerate", n_total=0),
    )
    out = summarize(r, top_n=3)
    assert "ok1" in out
    assert "fail1" not in out                       # 不进 top
    assert "failed" in out.lower() and "degenerate" in out.lower()   # 诊断计数


def test_summarize_no_ok():
    """无 ok 结果 → 提示无结果。"""
    r = _result_with(TrialResult("f", "failed", error="x"))
    out = summarize(r)
    assert "无 ok" in out
```

- [ ] **Step 2: 跑测试,确认 ImportError 失败**

Run: `F:/quanter/.venv310/Scripts/python.exe -m pytest tests/compute_unit/test_summary.py -v`
Expected: FAIL(`ModuleNotFoundError: No module named 'compute_unit.summary'`)。

- [ ] **Step 3: 实现 `compute_unit/summary.py`**

```python
# -*- coding: utf-8 -*-
"""人可读摘要:从 Result 按 inner calmar 选 top-N → 钉钉友好中文文本。

物理意图(spec §3.3 路径 2):Mac 跑完不回库,只生成几百字摘要供人 AirDrop + 手机钉钉转发。
出站信息量从「全批 result.json ~50KB」压到「top-N 摘要几百字」,绕开钉钉消息大小限制。

排序键:inner calmar 降序(与 discovery 冠军排序同口径,spec §15 开放问题②)。
仅展示 status=ok 的 trial(failed/degenerate 跳过)。含 inner+outer calmar/max_dd/ann/n,
人按「outer calmar 高 + max_dd 小 + n 足够」综合判断(与 discovery judging 口径一致,
信息隔离:outer 不反馈搜索但供人参考)。
"""
from __future__ import annotations

from compute_unit.protocol import Result


def _fmt_metrics(m: dict, prefix: str = "") -> str:
    """metrics dict → 单行简短中文(n/年化/calmar/回撤)。空 dict 安全。"""
    if not m:
        return f"{prefix}无数据"
    return (
        f"{prefix}n={m.get('n', 0)} "
        f"年化{m.get('ann', 0)*100:+.1f}% "
        f"calmar={m.get('calmar', 0):.2f} "
        f"回撤{m.get('max_dd', 0)*100:.1f}%"
    )


def summarize(result: Result, top_n: int = 3) -> str:
    """Result → top-N 中文摘要文本(钉钉友好,纯文本无 markdown 特殊字符)。

    top-N 按 inner calmar 降序,仅 ok 的 trial。末尾附 failed/degenerate 计数(供诊断)。
    """
    ok = [r for r in result.results if r.status == "ok"]
    ok.sort(key=lambda r: r.inner.get("calmar", 0.0), reverse=True)
    top = ok[:top_n]

    lines = [
        f"【Mac 计算单元回测摘要】task={result.task_id} 共{len(result.results)}组",
        f"git={result.git_commit[:8]} parquet={result.parquet_sha256[:8]}…",
        f"跑批时间 {result.ran_at}",
        "",
    ]
    if not top:
        lines.append("无 ok 结果(全 failed/degenerate,请查 result.json 的 error 字段)")
    else:
        lines.append(f"▼ top-{len(top)}(按 inner calmar 降序)")
        for i, r in enumerate(top, 1):
            lines.append(f"{i}. trial={r.trial_id}")
            lines.append(f"   {_fmt_metrics(r.inner, 'inner ')}")
            lines.append(f"   {_fmt_metrics(r.outer, 'outer ')}")
            lines.append(f"   总笔数 n_total={r.n_total}")
            lines.append("")
    # 诊断计数(failed/degenerate 数量,供判断批质量)
    n_failed = sum(1 for r in result.results if r.status == "failed")
    n_degen = sum(1 for r in result.results if r.status == "degenerate")
    if n_failed or n_degen:
        lines.append(f"(诊断:failed {n_failed} / degenerate {n_degen})")
    return "\n".join(lines)
```

- [ ] **Step 4: 跑测试,确认通过**

Run: `F:/quanter/.venv310/Scripts/python.exe -m pytest tests/compute_unit/test_summary.py -v`
Expected: 3 PASS。

- [ ] **Step 5: 提交**

```bash
git add compute_unit/summary.py tests/compute_unit/test_summary.py
git commit -m "feat(compute_unit): summary — top-N 按 inner calmar 降序中文摘要

- summarize 按 inner calmar 降序选 top-N(与 discovery 冠军排序同口径)
- 仅展示 ok 的 trial;failed/degenerate 末尾诊断计数
- inner+outer calmar/max_dd/ann/n 全展示,供人「outer calmar 高+max_dd 小+n 足够」综合判断
- 纯文本无 markdown 特殊字符(钉钉友好),3 例单测覆盖

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 5: __main__.py —— CLI(verify / run / summary)

**Files:**
- Create: `compute_unit/__main__.py`
- Test: `tests/compute_unit/test_cli.py`

**Interfaces:**
- Consumes: Task 1-4 的 `Task.from_json`/`Result.from_json`/`verify`/`run`/`summarize`/`EnvDriftError`。
- Produces: `main(argv) -> int`(`python -m compute_unit {verify|run|summary} ...`)。

- [ ] **Step 1: 写 `test_cli.py` 失败测试**

`tests/compute_unit/test_cli.py`:
```python
# -*- coding: utf-8 -*-
"""__main__ CLI 测试:verify/run/summary 三子命令路由 + 退出码 + EnvDriftError 兜底。

mock run/verify/summarize,不真跑回测/不真读 parquet。
"""
from compute_unit import __main__ as cu


def _write_task(tmp_path):
    """写一个最小 task.json 供 CLI 读(mock 真实 freeze)。"""
    import json
    task = {
        "protocol_version": 1, "task_id": "t1", "created_at": "x",
        "git_commit": "a"*40, "engine_hash": "x", "parquet_sha256": "x",
        "lake_start": "2025-01-01", "embargo_days": 5,
        "snapshot_meta": {"snapshot_hash": "s", "universe_def": "u", "universe_count": 1,
                          "date_range": "dr", "lake_start": "2025-01-01"},
        "split": {"inner": {"name": "i", "start": "2025-01-01", "end": "2025-12-31"},
                  "outer": {"name": "o", "start": "2026-01-01", "end": "2026-12-31"},
                  "embargo_days": 5},
        "trials": [],
    }
    p = tmp_path / "task.json"
    p.write_text(json.dumps(task), encoding="utf-8")
    return p


def test_verify_ok(monkeypatch, tmp_path, capsys):
    """verify 子命令:verify 通过 → 退出码 0 + 打印可跑批。"""
    monkeypatch.setattr("compute_unit.env_check.verify", lambda t: None)
    p = _write_task(tmp_path)
    rc = cu.main(["verify", str(p)])
    assert rc == 0
    assert "可跑批" in capsys.readouterr().out


def test_verify_drift_returns_3(monkeypatch, tmp_path, capsys):
    """verify 漂移 → 退出码 3 + stderr 打印漂移信息。"""
    from compute_unit.env_check import EnvDriftError
    monkeypatch.setattr("compute_unit.env_check.verify",
                        lambda t: (_ for _ in ()).throw(EnvDriftError("git_commit 漂移")))
    p = _write_task(tmp_path)
    rc = cu.main(["verify", str(p)])
    assert rc == 3
    assert "环境漂移" in capsys.readouterr().err


def test_run_writes_result(monkeypatch, tmp_path):
    """run 子命令:run 通过 → 写 result.json + 退出码 0。"""
    from compute_unit.protocol import Result
    fake = Result(task_id="t1", git_commit="a"*40, parquet_sha256="x", ran_at="x", results=[])
    monkeypatch.setattr("compute_unit.runner.run", lambda t, n_proc=None: fake)
    p = _write_task(tmp_path)
    out = tmp_path / "result.json"
    rc = cu.main(["run", str(p), "-o", str(out)])
    assert rc == 0
    assert out.exists()


def test_run_drift_returns_3(monkeypatch, tmp_path):
    """run 漂移 → 退出码 3(不写 result)。"""
    from compute_unit.env_check import EnvDriftError
    monkeypatch.setattr("compute_unit.runner.run",
                        lambda t, n_proc=None: (_ for _ in ()).throw(EnvDriftError("engine 漂移")))
    p = _write_task(tmp_path)
    out = tmp_path / "result.json"
    rc = cu.main(["run", str(p), "-o", str(out)])
    assert rc == 3
    assert not out.exists()


def test_summary_prints(monkeypatch, tmp_path, capsys):
    """summary 子命令:读 result.json → 打印摘要。"""
    import json
    rfile = tmp_path / "result.json"
    rfile.write_text(json.dumps({
        "task_id": "t1", "git_commit": "a"*40, "parquet_sha256": "b"*64, "ran_at": "x",
        "results": [{"trial_id": "ok1", "status": "ok",
                     "inner": {"n": 5, "ann": 0.2, "calmar": 3.0, "max_dd": 0.07},
                     "outer": {"n": 4}, "n_total": 9}],
    }), encoding="utf-8")
    rc = cu.main(["summary", str(rfile), "--top", "3"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Mac 计算单元" in out and "ok1" in out
```

- [ ] **Step 2: 跑测试,确认 ImportError 失败**

Run: `F:/quanter/.venv310/Scripts/python.exe -m pytest tests/compute_unit/test_cli.py -v`
Expected: FAIL(`ModuleNotFoundError: No module named 'compute_unit.__main__'` 或 main 未定义)。

- [ ] **Step 3: 实现 `compute_unit/__main__.py`**

```python
# -*- coding: utf-8 -*-
"""compute_unit CLI:verify / run / summary 三子命令。

物理定位:Mac 端唯一入口。verify=只校验不跑;run=校验+跑批+写 result.json;
summary=读 result.json 生成摘要文本(spec §6 数据流出站前最后一步,人 AirDrop 前生成)。

退出码:0=成功;3=环境漂移(EnvDriftError);1=其他错误。
"""
from __future__ import annotations

import argparse
import sys

from compute_unit.env_check import EnvDriftError, verify
from compute_unit.protocol import Result, Task
from compute_unit.runner import run
from compute_unit.summary import summarize


def main(argv: list[str] | None = None) -> int:
    """CLI 总入口。返 0=成功 / 3=环境漂移 / 1=其他错误。"""
    p = argparse.ArgumentParser(prog="python -m compute_unit", description="Mac 远程计算单元(回测)")
    sub = p.add_subparsers(dest="cmd", required=True)

    pv = sub.add_parser("verify", help="校验 task.json 环境(三件哈希+snapshot,不跑批)")
    pv.add_argument("task_json", help="task.json 路径")

    pr = sub.add_parser("run", help="校验 + 跑批 → 写 result.json")
    pr.add_argument("task_json", help="task.json 路径")
    pr.add_argument("-o", "--out", default="result.json", help="result.json 输出路径(默认 ./result.json)")
    pr.add_argument("--n-proc", type=int, default=None, help="并发进程数(默认核数-2)")

    ps = sub.add_parser("summary", help="读 result.json → top-N 中文摘要")
    ps.add_argument("result_json", help="result.json 路径")
    ps.add_argument("--top", type=int, default=3, help="展示 top-N(默认 3)")

    args = p.parse_args(argv)

    if args.cmd == "verify":
        task = Task.from_json(args.task_json)
        try:
            verify(task)
        except EnvDriftError as e:
            print(f"❌ 环境漂移:{e}", file=sys.stderr)
            return 3
        print(f"✅ 环境一致,可跑批(task={task.task_id})")
        return 0

    if args.cmd == "run":
        task = Task.from_json(args.task_json)
        try:
            result = run(task, n_proc=args.n_proc)
        except EnvDriftError as e:
            print(f"❌ 环境漂移:{e}", file=sys.stderr)
            return 3
        result.to_json(args.out)
        print(f"✅ 跑批完成 task={result.task_id} → {args.out}({len(result.results)}组)")
        return 0

    if args.cmd == "summary":
        result = Result.from_json(args.result_json)
        print(summarize(result, top_n=args.top))
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: 跑测试,确认通过**

Run: `F:/quanter/.venv310/Scripts/python.exe -m pytest tests/compute_unit/test_cli.py -v`
Expected: 5 PASS。

- [ ] **Step 5: 冒烟(CLI 帮助正常)**

Run: `F:/quanter/.venv310/Scripts/python.exe -m compute_unit --help`
Expected: 打印三子命令帮助,无报错。

- [ ] **Step 6: 提交**

```bash
git add compute_unit/__main__.py tests/compute_unit/test_cli.py
git commit -m "feat(compute_unit): __main__ CLI — verify/run/summary 三子命令

- verify:只校验不跑;run:校验+跑批+写 result.json;summary:读 result→中文摘要
- 退出码 0=成功 / 3=EnvDriftError 漂移 / 1=其他
- argparse 子命令;5 例单测覆盖三命令 + 漂移退出码 + summary 打印

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 6: task_export.py —— Win 端导出 task.json

**Files:**
- Create: `compute_unit/task_export.py`
- Test: `tests/compute_unit/test_task_export.py`

**Interfaces:**
- Consumes: Task 1 的 `Task`/`TrialSpec`/`SnapshotMetaSpec`/`SplitSpec`/`SegmentSpec`;`discovery.snapshot.freeze`、`discovery.store.trial_id_of`、`discovery.split.holdout_split`(延迟 import)。
- Produces: `export_task(params_list, lake_start, seed, source, out_path) -> Task`、`main(argv)`(CLI)、`_git_head_sha()`、`_file_sha256()`、`_engine_hash()`、`_holdout_to_spec()`、`_snapshot_to_spec()`(后三个供测试)。

> **复用同款哈希算法**:task_export 的 `_git_head_sha`/`_file_sha256`/`_engine_hash` 与 env_check 同款(本地重声明,避免 task_export→env_check→discovery.snapshot 的 import 链耦合)。Win 端导出时算的哈希 = Mac 端 env_check 校验时重算的哈希,算法一致才能比对。

- [ ] **Step 1: 写 `test_task_export.py` 失败测试**

`tests/compute_unit/test_task_export.py`:
```python
# -*- coding: utf-8 -*-
"""task_export 测试:mock freeze/holdout_split/git/哈希,断言 task.json 字段 + trial_id 一致。"""
import json
from datetime import date
from types import SimpleNamespace

from compute_unit import task_export


def _fake_meta():
    return SimpleNamespace(
        snapshot_hash="snap1234abcd5678", universe_def="创板科创",
        universe_count=1334, date_range="2025-01-01~2026-07-25", lake_start="2025-01-01",
    )


def test_export_task_fields(monkeypatch, tmp_path):
    """export_task 写 task.json:字段完整 + trial_id == trial_id_of 算(C8 权威)。"""
    from discovery.store import trial_id_of
    from discovery.split import Segment, HoldoutSplit

    # mock freeze(返合成 meta,不读真实 parquet)
    monkeypatch.setattr("discovery.snapshot.freeze", lambda lake_start="x": ({}, _fake_meta()))
    # mock holdout_split(返固定 split)
    monkeypatch.setattr("discovery.split.holdout_split", lambda embargo_days=5: HoldoutSplit(
        inner=Segment("inner_2025", date(2025, 1, 1), date(2025, 12, 31)),
        outer=Segment("outer_2026", date(2026, 1, 1), date(2026, 12, 31)), embargo_days=5))
    # mock 三件哈希
    monkeypatch.setattr(task_export, "_git_head_sha", lambda: "a"*40)
    monkeypatch.setattr(task_export, "_engine_hash", lambda: "abc123def456")
    monkeypatch.setattr(task_export, "_file_sha256", lambda p: "b"*64)

    params = [{"window": 80, "min_touches": 2}]
    out = tmp_path / "task.json"
    task = task_export.export_task(params, out_path=out)

    # 三件哈希字段
    assert task.git_commit == "a"*40
    assert task.engine_hash == "abc123def456"
    assert task.parquet_sha256 == "b"*64
    # snapshot meta 透传
    assert task.snapshot_meta.universe_count == 1334
    assert task.snapshot_meta.snapshot_hash == "snap1234abcd5678"
    # trial_id == trial_id_of 算(C8 权威来源,Mac 不算)
    assert task.trials[0].trial_id == trial_id_of(params[0], "snap1234abcd5678", 42)
    # json 文件写出且可读回(ensure_ascii=False 中文保真)
    raw = json.loads(out.read_text(encoding="utf-8"))
    assert raw["task_id"] == task.task_id
    assert raw["trials"][0]["params"] == params[0]
    assert raw["snapshot_meta"]["universe_def"] == "创板科创"
```

- [ ] **Step 2: 跑测试,确认 ImportError 失败**

Run: `F:/quanter/.venv310/Scripts/python.exe -m pytest tests/compute_unit/test_task_export.py -v`
Expected: FAIL(`ModuleNotFoundError: No module named 'compute_unit.task_export'`)。

- [ ] **Step 3: 实现 `compute_unit/task_export.py`**

```python
# -*- coding: utf-8 -*-
"""Win 端 task.json 导出:params 批 + 当前 freeze meta + 三件哈希 → tasks/<id>.json。

物理定位:Win 主机专用(Mac 不用)。把 discovery Sobol 采样的一批 params 打包成 Mac 可拉的
task.json。trial_id 在此预算(discovery.store.trial_id_of,C8),Mac 不算——跨机一致性的
权威来源。

用法:
  python -m compute_unit.task_export --params-file sobol_batch.json --out tasks/<id>.json
  (sobol_batch.json 内容为 params dict 的 list,如 [{"window":80,...}, {...}] )
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import uuid
from datetime import datetime
from pathlib import Path

from compute_unit.protocol import (
    Task, TrialSpec, SnapshotMetaSpec, SplitSpec, SegmentSpec,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _git_head_sha() -> str:
    """当前 HEAD sha(与 env_check._git_head_sha 同款;本地重声明避免跨模块耦合)。"""
    try:
        r = subprocess.run(["git", "rev-parse", "HEAD"],
                           capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            raise RuntimeError(f"git rev-parse HEAD 失败(不在 git 仓库?):{r.stderr}")
        return r.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(f"git rev-parse HEAD 异常:{e}")


def _file_sha256(path: Path) -> str:
    """文件全内容 sha256(与 env_check._file_sha256 同款,64KB 块流式)。"""
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _engine_hash() -> str:
    """回测内核指纹(与 discovery.runner._engine_hash / env_check._engine_hash 同款)。"""
    from strategies.neckline import backtest, method_v0
    h = hashlib.sha256()
    for f in (backtest.__file__, method_v0.__file__):
        with open(f, "rb") as fh:
            h.update(fh.read())
    return h.hexdigest()[:12]


def _holdout_to_spec() -> tuple:
    """discovery.split.holdout_split() → (SplitSpec, embargo_days) 序列化形态。"""
    from discovery.split import holdout_split
    sp = holdout_split()   # 默认 embargo_days=5,inner 2025 / outer 2026
    spec = SplitSpec(
        inner=SegmentSpec(sp.inner.name, sp.inner.start, sp.inner.end),
        outer=SegmentSpec(sp.outer.name, sp.outer.start, sp.outer.end),
        embargo_days=sp.embargo_days,
    )
    return spec, sp.embargo_days


def _snapshot_to_spec(meta) -> SnapshotMetaSpec:
    """discovery.snapshot.SnapshotMeta → SnapshotMetaSpec 序列化形态。"""
    return SnapshotMetaSpec(
        snapshot_hash=meta.snapshot_hash, universe_def=meta.universe_def,
        universe_count=meta.universe_count, date_range=meta.date_range,
        lake_start=meta.lake_start,
    )


def export_task(params_list: list, lake_start: str = "2025-01-01",
                seed: int = 42, source: str = "discovery_search",
                out_path="tasks/task.json") -> Task:
    """params 批 → Task → 写 out_path。

    流程:freeze 取 SnapshotMeta → 算 trial_id(trial_id_of)→ 算三件哈希(git/engine/parquet)
    → 拼 Task → to_json。trial_id 在此预算(Win 权威 C8),Mac 不算。
    """
    from discovery.snapshot import freeze
    from discovery.store import trial_id_of

    _universe, meta = freeze(lake_start=lake_start)
    split_spec, embargo_days = _holdout_to_spec()

    trials = [
        TrialSpec(trial_id=trial_id_of(p, meta.snapshot_hash, seed),
                  params=p, source=source, seed=seed)
        for p in params_list
    ]
    task = Task(
        protocol_version=1,
        task_id=uuid.uuid4().hex[:8],
        created_at=datetime.utcnow().isoformat(),
        git_commit=_git_head_sha(),
        engine_hash=_engine_hash(),
        parquet_sha256=_file_sha256(PROJECT_ROOT / "data_lake" / "a_shares_daily.parquet"),
        lake_start=lake_start,
        embargo_days=embargo_days,
        snapshot_meta=_snapshot_to_spec(meta),
        split=split_spec,
        trials=trials,
    )
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    task.to_json(out_path)
    return task


def main(argv: list[str] | None = None) -> int:
    """CLI:python -m compute_unit.task_export --params-file <json> --out <path>。"""
    p = argparse.ArgumentParser(prog="python -m compute_unit.task_export",
                                description="Win 端导出 task.json(Mac 拉取跑批)")
    p.add_argument("--params-file", required=True, help="JSON 文件,内容为 params dict 的 list")
    p.add_argument("--out", default="tasks/task.json", help="task.json 输出路径")
    p.add_argument("--lake-start", default="2025-01-01", help="freeze 加载起始日")
    p.add_argument("--seed", type=int, default=42, help="trial_id seed")
    args = p.parse_args(argv)

    params_list = json.loads(Path(args.params_file).read_text(encoding="utf-8"))
    task = export_task(params_list, lake_start=args.lake_start, seed=args.seed, out_path=args.out)
    print(f"✅ 导出 task={task.task_id} → {args.out}({len(task.trials)}组)")
    print(f"   git={task.git_commit[:8]} engine={task.engine_hash} "
          f"parquet={task.parquet_sha256[:8]}…")
    print(f"   snapshot={task.snapshot_meta.snapshot_hash[:8]} "
          f"universe={task.snapshot_meta.universe_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: 跑测试,确认通过**

Run: `F:/quanter/.venv310/Scripts/python.exe -m pytest tests/compute_unit/test_task_export.py -v`
Expected: 1 PASS。

- [ ] **Step 5: 提交**

```bash
git add compute_unit/task_export.py tests/compute_unit/test_task_export.py
git commit -m "feat(compute_unit): task_export — Win 端导出 task.json(预算 trial_id + 三件哈希)

- export_task:freeze 取 meta → trial_id_of 预算 trial_id(C8 Win 权威)→ 三件哈希 → Task
- _git_head_sha/_file_sha256/_engine_hash 与 env_check 同款(本地重声明避免 import 耦合)
- CLI:--params-file/--out/--lake-start/--seed
- mock freeze/holdout/git 单测:字段完整 + trial_id == trial_id_of + 中文保真

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 7: .gitignore 放行 parquet + 首次纳入 git

**Files:**
- Modify: `.gitignore:86`(`data_lake/` 加取反放行 a_shares_daily.parquet)
- 无单测(git 操作;验收靠 `git status` + Mac 端 clone 含 parquet)。

> **物理意图(spec §4)**:Mac 只能 git pull,parquet 必须进 git 才能到 Mac。当前 `.gitignore:86` 是 `data_lake/`(整个目录忽略),要精确放行 `a_shares_daily.parquet` 一个文件,其余 data_lake 继续忽略(4.4GB 湖与 Mac 计算单元无关)。

- [ ] **Step 1: 改 `.gitignore` 第 86 行**

把:
```gitignore
# 数据湖生成产物（parquet/shards，本地产物不入库）
data_lake/
```
改为:
```gitignore
# 数据湖生成产物（parquet/shards，本地产物不入库；a_shares_daily.parquet 例外，见下）
data_lake/*
# Mac 计算单元需 git pull 取 a_shares_daily.parquet（discovery 回测唯一依赖，spec §4）
!data_lake/a_shares_daily.parquet
```

> **取反规则踩坑(关键)**:`data_lake/`(忽略目录本身)会让 `!data_lake/xxx` 取反**无效**——git 不进入被忽略目录,内部文件的 `!` 取反不生效。必须用 `data_lake/*`(忽略目录内容、不忽略目录本身),`!data_lake/a_shares_daily.parquet` 才能重新包含。这是 gitignore 的反直觉坑,务必用 `/*` 形态。

- [ ] **Step 2: 验证 gitignore 规则生效**

Run: `F:/quanter/.venv310/Scripts/python.exe -c "import subprocess; print(subprocess.run(['git','check-ignore','--no-index','data_lake/a_shares_daily.parquet'],capture_output=True,text=True).returncode)"` 然后人工确认。

或更直接:`git check-ignore -v data_lake/a_shares_daily.parquet` 应**无输出**(不被忽略);`git check-ignore -v data_lake/index_daily.parquet` 应输出被 `.gitignore:data_lake/*` 匹配(仍忽略)。

Expected:
- `data_lake/a_shares_daily.parquet`:不被忽略(check-ignore 退出码 1)
- `data_lake/index_daily.parquet`:被忽略(check-ignore 退出码 0)

- [ ] **Step 3: 首次纳入 parquet(git add 435MB,单独 commit)**

Run:
```bash
git add .gitignore
git add data_lake/a_shares_daily.parquet
git status --short   # 确认 a_shares_daily.parquet 已暂存(435MB)
```

- [ ] **Step 4: 提交(parquet 单独 commit,commit message 标注 435MB 大文件)**

```bash
git commit -m "chore(data): 纳入 a_shares_daily.parquet 入 git（Mac 计算单元需 git pull 取数）

- .gitignore: data_lake/ → data_lake/* + !data_lake/a_shares_daily.parquet（精确放行）
- 其余 4.4GB data_lake 继续忽略（与 Mac 计算单元无关）
- discovery 回测唯一依赖此 parquet（snapshot.LAKE_PATH），435MB 进 git 非 LFS
  （spec §4：首次 clone 慢几分钟，后续周更 diff 小，parquet 列存增量友好）

Co-Authored-By: Claude <noreply@anthropic.com>"
```

> **注意**:435MB 大文件提交会让仓库 `.git` 增长。提交前与研究员确认网络允许 push 大 commit。若研究员偏好,可改 git LFS(spec §4 备选),但需 Mac 装 git-lfs(又一依赖),MVP 阶段非 LFS 最简。

---

## Task 8: 端到端等价测试 + 全量回归

**Files:**
- Create: `tests/compute_unit/test_e2e.py`
- 无新组件(整合 Task 1-6)。

**Interfaces:**
- Consumes: Task 1/3/4 的 `_eval_batch`/`summarize`/`Result`/`Task`;`discovery.objective.evaluate`、`discovery.split.holdout_split`。

- [ ] **Step 1: 写 `test_e2e.py`(C6 红线全链路)**

`tests/compute_unit/test_e2e.py`:
```python
# -*- coding: utf-8 -*-
"""端到端等价测试(C6 红线):_eval_batch → summarize 全链路与 discovery 直跑等价。

注入合成 universe(不依赖真实 parquet,快)。task_export 的 freeze 集成在 test_task_export
已测,本测试聚焦「跑批 + 摘要」链路与 discovery 的等价——compute_unit 不能偷偷改口径。
"""
from datetime import date

from compute_unit.protocol import (
    Task, TrialSpec, SnapshotMetaSpec, SplitSpec, SegmentSpec, Result,
)
from compute_unit.runner import _eval_batch
from compute_unit.summary import summarize
from discovery.objective import evaluate
from discovery.split import holdout_split


def _task(trial_id, params):
    """构造含 1 trial 的 Task(手工填字段,跳过 task_export 的 freeze)。"""
    return Task(
        protocol_version=1, task_id="e2e", created_at="x", git_commit="a"*40,
        engine_hash="x", parquet_sha256="x",
        lake_start="2025-01-01", embargo_days=5,
        snapshot_meta=SnapshotMetaSpec("s", "u", 1, "dr", "2025-01-01"),
        split=SplitSpec(SegmentSpec("i", date(2025, 1, 1), date(2025, 12, 31)),
                        SegmentSpec("o", date(2026, 1, 1), date(2026, 12, 31)), 5),
        trials=[TrialSpec(trial_id, params, "discovery_search", 42)],
    )


def test_e2e_eval_equivalent(fixed_params, synth_universe):
    """_eval_batch(synth_universe) inner/outer/n_total == evaluate 直跑(逐字段,红线 C6)。"""
    task = _task("e2e_tid", fixed_params)
    results = _eval_batch(task, synth_universe, task.split, n_proc=1)
    direct = evaluate(fixed_params, synth_universe, holdout_split())
    assert results[0].status == "ok"
    assert results[0].inner == direct["inner"]
    assert results[0].outer == direct["outer"]
    assert results[0].n_total == direct["n_total"]


def test_e2e_summary_runs(fixed_params, synth_universe):
    """跑批 → 拼 Result → summarize 跑通(不抛,含标题 + trial_id)。"""
    task = _task("e2e2_tid", fixed_params)
    results = _eval_batch(task, synth_universe, task.split, n_proc=1)
    result = Result(task_id="e2e2", git_commit="a"*40, parquet_sha256="x",
                    ran_at="x", results=results)
    out = summarize(result, top_n=3)
    assert "Mac 计算单元" in out
    assert "e2e2_tid" in out
```

- [ ] **Step 2: 跑 e2e,确认通过**

Run: `F:/quanter/.venv310/Scripts/python.exe -m pytest tests/compute_unit/test_e2e.py -v`
Expected: 2 PASS。

- [ ] **Step 3: 全量回归(compute_unit 包 + discovery 回归不破)**

Run: `F:/quanter/.venv310/Scripts/python.exe -m pytest tests/compute_unit/ tests/discovery/ -v`
Expected: compute_unit 全绿(test_protocol 2 + test_env_check 7 + test_runner 5 + test_summary 3 + test_cli 5 + test_task_export 1 + test_e2e 2 = 25)+ discovery 全绿(内核零改动,无回归)。

> **若 discovery 测试有失败**:说明 C1 被破坏(compute_unit 误改了 discovery 内核或 import 有副作用)。检查 compute_unit 模块顶层是否无意触发 discovery 副作用。

- [ ] **Step 4: 提交**

```bash
git add tests/compute_unit/test_e2e.py
git commit -m "test(compute_unit): e2e 等价红线 C6 — _eval_batch→summarize 与 discovery 逐字段相等

- test_e2e_eval_equivalent:_eval_batch(synth_universe) inner/outer/n_total == evaluate 直跑
- test_e2e_summary_runs:跑批→Result→summarize 全链路跑通(含标题+trial_id)
- 全量回归:compute_unit 25 例全绿 + discovery 零回归(C1 内核零改动守护)

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## 验收对照(spec §12 Phase 1)

- [ ] `compute_unit verify <task.json>` 校验三件哈希+snapshot,漂移退出码 3 → Task 2 + Task 5 `test_verify_drift_returns_3`
- [ ] `compute_unit run <task.json> -o result.json` 跑批写 result → Task 3 + Task 5 `test_run_writes_result`
- [ ] `compute_unit summary <result.json> --top 3` 打印中文摘要 → Task 4 + Task 5 `test_summary_prints`
- [ ] compute_unit 跑出的 inner/outer metrics 与 Win 本机 `discovery.objective.evaluate` 直跑**逐字段相等** → Task 3 `test_eval_one_equivalent`/`test_eval_batch_equivalent` + Task 8 `test_e2e_eval_equivalent`(C6 红线)
- [ ] spawn 四铁律:顶层函数 + initializer 注入 universe + 不随 params pickle + failed/degenerate → Task 3 四测试
- [ ] `task_export` 预算 trial_id == `trial_id_of`(C8 权威)→ Task 6 `test_export_task_fields`
- [ ] 三件哈希漂移各分支单测 → Task 2 三测试
- [ ] discovery 内核零改动,回归不破 → Task 8 Step 3 全量回归
- [ ] `.gitignore` 精确放行 `a_shares_daily.parquet`,其余 data_lake 继续忽略 → Task 7 Step 2 check-ignore 双验

---

## 用户侧外向动作(C6 · AI 不替按 · Phase 1 完成后研究员执行)

```bash
# 1. push 到远端(让 Mac 能 pull)
cd F:/quanter
git push -u origin feat/compute-unit

# 2. Mac 端环境准备(Phase 2 真机部署用,Phase 1 Win 本机模拟不必)
#    git clone <quanter>  # 含 parquet 435MB,首次 clone 慢几分钟
#    cd quanter && python3 -m venv .venv && source .venv/bin/activate
#    pip install pandas numpy pyarrow
```

> **Phase 1 不含**:Mac 真机部署、discovery Sobol 自动导出钩子、补跑 SOP 文档(均 Phase 2,spec §12)。
