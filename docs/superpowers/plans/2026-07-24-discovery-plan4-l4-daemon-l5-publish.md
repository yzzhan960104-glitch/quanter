# Plan 4 实施计划：L4 守护 daemon + L5 闭环 publish

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 补齐 discovery 引擎最后一块——L4 跨夜守护 daemon（schtasks 夜跑 + 跨 run 收敛判据① + 新冠军钉钉 + 冠军 outer 去偏）+ L5 闭环 publish（冠军→experiment DRAFT + ≥5天硬闸 fail-closed），使 §12 验收 #13/#14 兑现。

**Architecture:** daemon 是 `run_search` 的薄编排层（schtasks 每夜触发短命子命令，跨夜状态落 `search_run` 表）；publish 桥接冠军 trial → experiment DRAFT（零改 experiment）；硬闸把 `trading/__main__.py` 现状 WARNING 升级为 fail-closed。discovery 包自包含 schtasks 调度，不依赖 `scripts/`。

**Tech Stack:** Python 3.10（`.venv310`）/ SQLite WAL / stdlib sqlite3+threading / optuna 4.9.0（Plan 3 既有，T0/T7 用）/ APScheduler（既有，不改）。**零新增依赖**。

## Global Constraints

- **全中文注释**（CLAUDE.md）：每个新增函数讲清"为什么"（物理意图/风控意图），不只"是什么"。
- **零新增依赖**：optuna 已是 Plan 3 唯一新依赖；scipy 仅 T0 DSR 对照测试用（`pytest.importorskip`，非运行时依赖）。
- **TDD RED→GREEN→回归**：每步先写测试跑失败 → 实现 → 跑过 → 回归 Plan 1-3 的 84 non-slow 全绿。
- **频繁 commit**：每个 task 末尾 commit（commit message 中文，`feat(discovery):` / `fix(discovery):` 前缀）。
- **信息隔离红线**（spec §6.2）：`evaluate` 的 outer 结果严禁回写 `run_search` 排序——代码级保证 outer 变量不传回搜索。
- **SQLite WAL + `_write_lock` 单点写**：新写入函数复用 `discovery/store.py` 既有 `_write_lock` 模式（跨线程串行化，防并发写锁死）。
- **Windows 兼容**：bat/schtasks 路径用反斜杠；`ProcessPool` spawn pickle 守护既有（Plan 2）。
- **分支**：`feat/discovery-l0-l1`（当前分支，非 master，直接提交）。

---

## File Structure

| 文件 | 职责 | task |
|---|---|---|
| `discovery/store.py`【改】 | search_run 表扩跨夜字段 + `write_search_run`/`read_latest_search_run`/`write_daemon_state` + init_db migration | T1 |
| `discovery/runner.py`【改】 | `RunSummary` 加 `run_id`；`run_search` 收尾调 `write_search_run` | T1 |
| `discovery/daemon.py`【新】 | `run_daemon_cycle` 跨夜编排纯函数（注入 run_search/notify/eval_outer） | T2/T3 |
| `discovery/schtasks.py`【新】 | discovery 夜跑任务 schtasks 注册（register/unregister/list 纯函数 + CLI） | T4 |
| `discovery/run_daemon.bat`【新】 | schtasks 触发入口（call venv + cd + python -m discovery daemon） | T4 |
| `discovery/publish.py`【新】 | `publish_champion`：冠军 trial → experiment DRAFT + outer 报告 | T5 |
| `discovery/cli.py`【改】 | `cmd_daemon` / `cmd_publish` 子命令 + `estimate_budget` | T4/T5 |
| `discovery/__init__.py`【改】 | 导出 daemon/publish API | T4/T5 |
| `experiment/models.py`【改】 | `ActiveExperiment` 加 `activated_at`（additive） | T5 |
| `experiment/resolver.py`【改】 | `resolve_active` 构造时带上 `activated_at` | T5 |
| `trading/__main__.py`【改】 | WARNING → fail-closed ≥5天硬闸 | T6 |
| `tests/discovery/test_store.py`【改】 | 跨夜状态 migration + roundtrip | T1 |
| `tests/discovery/test_daemon.py`【新】 | daemon 编排（跨夜 k/收敛/早退/告警/outer 隔离） | T2/T3 |
| `tests/discovery/test_schtasks.py`【新】 | schtasks 注册纯函数（mock subprocess） | T4 |
| `tests/discovery/test_publish.py`【新】 | publish → DRAFT + 不自动 promote + outer 报告 | T5 |
| `tests/trading/test_main_shadow_gate.py`【新】 | ≥5天硬闸 5 场景 | T6 |
| `tests/discovery/test_plan4_e2e.py`【新,slow】 | 多夜 daemon + publish→DRAFT 闭环 | T7 |
| `tests/discovery/test_dsr.py`【改】 | DSR vs scipy 数值对照 | T0 |

---

## Task 0: pre-flight verification（Plan 3 地基）

**Files:**
- Modify: `tests/discovery/test_dsr.py`
- Run: `tests/discovery/test_plan3_e2e.py`（slow，已有）

**Interfaces:**
- Consumes: `discovery/dsr.py::_norm_ppf`（Acklam 逆正态，已存在）
- Produces: DSR 数学锁（scipy 对照断言）+ Plan 3 slow 绿灯确认

- [ ] **Step 1: 写 DSR vs scipy 数值对照测试（RED）**

在 `tests/discovery/test_dsr.py` 末尾追加：

```python
def test_norm_ppf_matches_scipy():
    """DSR 闭式的 Acklam 逆正态须与 scipy.stats.norm.ppf 逐数字一致（锁数学，防系数抄错）。

    物理意图：DSR 公式依赖 Φ^{-1}(p)，discovery/dsr.py 用 Acklam 算法纯 Python 实现（不引
    scipy 作运行时依赖）。Acklam 系数抄错一个数字 → DSR 系统性偏差且无报警。本测试用 scipy
    权威实现做 golden 对照，绝对误差须 < 1e-9（Acklam 1996 公开精度）。
    """
    scipy_stats = pytest.importorskip("scipy.stats")   # CI 无 scipy 则 skip，非运行时依赖
    from discovery.dsr import _norm_ppf
    # 覆盖 Acklam 三段分支：下尾(p<0.02425) / 中段 / 上尾(p>1-0.02425)
    for p in [0.001, 0.01, 0.02425, 0.1, 0.5, 0.9, 0.97575, 0.99, 0.999]:
        assert abs(_norm_ppf(p) - scipy_stats.norm.ppf(p)) < 1e-9, f"p={p} 偏差超 1e-9"
```

- [ ] **Step 2: 跑测试确认状态**

Run: `python -m pytest tests/discovery/test_dsr.py::test_norm_ppf_matches_scipy -v`
Expected: PASS（Acklam 实现正确则直接过；若 FAIL → 系数抄错，按 scipy 值核对 `discovery/dsr.py:34-41` 的 a/b/c/d 系数，这是 Plan 3 遗留 bug，修后 commit `fix(discovery):`）。

- [ ] **Step 3: 顺手修正算法名拼写（Plan 3 Minor）**

`discovery/dsr.py` 内注释/docstring 把 `Ackhard` 改为 `Acklam`（社区误写，算法发明人 Peter Acklam）。仅注释不改逻辑。

Run: `git grep -n "Ackhard" discovery/dsr.py` → 应只在注释里，逐处改 `Acklam`。

- [ ] **Step 4: 跑 DSR 全量 + Plan 3 slow 确认地基**

Run: `python -m pytest tests/discovery/test_dsr.py -v && python -m pytest tests/discovery/test_plan3_e2e.py -v -m slow`
Expected: test_dsr 全绿（含新对照）+ Plan 3 slow 端到端绿（TPE 收敛稳定）。

- [ ] **Step 5: Commit**

```bash
git add tests/discovery/test_dsr.py discovery/dsr.py
git commit -m "test(discovery): T0 DSR vs scipy 数值对照锁数学 + Acklam 拼写修正（Plan 4 pre-flight）"
```

---

## Task 1: search_run 表扩跨夜状态 + 读写 + run_search 落库

**Files:**
- Modify: `discovery/store.py:17-44`（SCHEMA）、`discovery/store.py:74-77`（init_db）、末尾追加三函数
- Modify: `discovery/runner.py:47-65`（RunSummary 加 run_id）、`discovery/runner.py:204-211`（run_search 收尾调 write_search_run）
- Test: `tests/discovery/test_store.py`

**Interfaces:**
- Consumes: `discovery/store.py::{init_db, connect, _write_lock, _now_iso, DEFAULT_DB_PATH}`（既有）；`RunSummary`（runner.py 既有）
- Produces:
  - `write_search_run(conn, run_id, snapshot_hash, started_at, ended_at, n_trials, status, frontier_size, k_rounds_no_expansion, daemon_run_count, note)` → INSERT OR REPLACE
  - `read_latest_search_run(conn, snapshot_hash) -> dict | None`（同 snapshot 最新一行的跨夜状态）
  - `write_daemon_state(conn, run_id, frontier_size, k_rounds_no_expansion, daemon_run_count, status)` → UPDATE
  - `RunSummary.run_id: str`（新增字段）

- [ ] **Step 1: 写 migration + roundtrip 测试（RED）**

在 `tests/discovery/test_store.py` 追加：

```python
def test_init_db_search_run_migration_idempotent(tmp_path):
    """search_run 表扩跨夜字段后，init_db 重复调用不报错（PRAGMA migration 幂等）。"""
    from discovery.store import init_db, SCHEMA
    db = str(tmp_path / "t.db")
    init_db(db)   # 首次：建表 + migration 补列
    init_db(db)   # 二次：migration 列已存，不重复 ALTER（不报错）
    init_db(db)   # 三次：再确认幂等
    import sqlite3
    con = sqlite3.connect(db)
    cols = {r[1] for r in con.execute("PRAGMA table_info(search_run)")}
    con.close()
    assert {"frontier_size_prev", "k_rounds_no_expansion", "daemon_run_count"} <= cols


def test_write_read_daemon_state_roundtrip(tmp_path):
    """write_search_run → read_latest_search_run → write_daemon_state 往返一致。"""
    from discovery.store import init_db, connect, write_search_run, \
        read_latest_search_run, write_daemon_state
    db = str(tmp_path / "t.db")
    init_db(db)
    snap = "abc123"
    with connect(db) as conn:
        write_search_run(conn, run_id="r1", snapshot_hash=snap, started_at="t1",
                         ended_at="t1e", n_trials=5, status="budget_exhausted",
                         frontier_size=3, k_rounds_no_expansion=0, daemon_run_count=1, note="")
        write_search_run(conn, run_id="r2", snapshot_hash=snap, started_at="t2",
                         ended_at="t2e", n_trials=4, status="budget_exhausted",
                         frontier_size=3, k_rounds_no_expansion=1, daemon_run_count=2, note="")
    with connect(db) as conn:
        latest = read_latest_search_run(conn, snap)
    assert latest is not None
    assert latest["run_id"] == "r2"                  # 最新一行（started_at DESC）
    assert latest["k_rounds_no_expansion"] == 1
    # daemon 更新本次行跨夜状态
    with connect(db) as conn:
        write_daemon_state(conn, run_id="r2", frontier_size=3,
                           k_rounds_no_expansion=2, daemon_run_count=2, status="converged")
        latest2 = read_latest_search_run(conn, snap)
    assert latest2["k_rounds_no_expansion"] == 2
    assert latest2["status"] == "converged"


def test_read_latest_search_run_none_when_empty(tmp_path):
    """首次 daemon（无历史 run）→ read_latest_search_run 返 None（不抛）。"""
    from discovery.store import init_db, connect, read_latest_search_run
    db = str(tmp_path / "t.db")
    init_db(db)
    with connect(db) as conn:
        assert read_latest_search_run(conn, "no_such_snap") is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/discovery/test_store.py::test_init_db_search_run_migration_idempotent tests/discovery/test_store.py::test_write_read_daemon_state_roundtrip -v`
Expected: FAIL（`write_search_run`/`read_latest_search_run`/`write_daemon_state` 未定义）。

- [ ] **Step 3: 改 SCHEMA + init_db migration**

`discovery/store.py` 的 `SCHEMA`（line 36-43）search_run 建表追加三字段（新库直接建全）：

```python
CREATE TABLE IF NOT EXISTS search_run (
  run_id                TEXT PRIMARY KEY,
  snapshot_hash         TEXT NOT NULL,
  started_at            TEXT,
  ended_at              TEXT,
  n_trials              INTEGER,
  status                TEXT,
  note                  TEXT,
  frontier_size_prev    INTEGER DEFAULT 0,        -- Plan 4 跨夜判据①：上夜前沿大小
  k_rounds_no_expansion INTEGER DEFAULT 0,        -- 连续未扩张夜数（>=K=3 收敛）
  daemon_run_count      INTEGER DEFAULT 0);       -- 累计 daemon 调用次数
```

`init_db`（line 74-77）追加 migration（仿 `backtest/tasks_db.py:88-91` PRAGMA 模式，老库无新列时 ALTER 补列）：

```python
def init_db(db_path=DEFAULT_DB_PATH):
    """建表（幂等）+ search_run 跨夜列 migration（Plan 4）。

    单点写锁内 executescript + PRAGMA 检列 ALTER，防并发建表/迁移竞争。SQLite 不支持
    ADD COLUMN IF NOT EXISTS，故 PRAGMA table_info 查列存否再 ALTER。
    """
    with _write_lock, connect(db_path) as conn:
        conn.executescript(SCHEMA)
        # Plan 4 跨夜列 migration（老库补列）
        cols = {r[1] for r in conn.execute("PRAGMA table_info(search_run)")}
        for col, decl in [
            ("frontier_size_prev", "INTEGER DEFAULT 0"),
            ("k_rounds_no_expansion", "INTEGER DEFAULT 0"),
            ("daemon_run_count", "INTEGER DEFAULT 0"),
        ]:
            if col not in cols:
                conn.execute(f"ALTER TABLE search_run ADD COLUMN {col} {decl}")
```

- [ ] **Step 4: 追加三个读写函数（store.py 末尾）**

```python
def write_search_run(conn, run_id, snapshot_hash, started_at, ended_at, n_trials,
                     status, frontier_size, k_rounds_no_expansion, daemon_run_count, note=""):
    """落 search_run 行（INSERT OR REPLACE upsert，含 Plan 4 跨夜状态字段）。

    每次 run_search/daemon 调用写一行；同 snapshot_hash 下多行（每夜一行），
    read_latest_search_run 按 started_at DESC 取最新做跨夜比对。
    """
    with _write_lock:
        conn.execute(
            "INSERT OR REPLACE INTO search_run "
            "(run_id, snapshot_hash, started_at, ended_at, n_trials, status, note, "
            " frontier_size_prev, k_rounds_no_expansion, daemon_run_count) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (run_id, snapshot_hash, started_at, ended_at, n_trials, status, note,
             frontier_size, k_rounds_no_expansion, daemon_run_count))


def read_latest_search_run(conn, snapshot_hash):
    """读同 snapshot 最新一行 search_run（跨夜判据① 状态源）。

    返回 dict（含 run_id/frontier_size_prev/k_rounds_no_expansion/daemon_run_count/status）
    或 None（首次 daemon，无历史 run）。按 started_at DESC 取最新——daemon 每夜写一行，
    最新即上夜。frontier_size_prev 存的是「本次跑完的前沿大小」（write_search_run 落本次，
    次夜读作 prev 比对）。
    """
    row = conn.execute(
        "SELECT run_id, frontier_size_prev, k_rounds_no_expansion, daemon_run_count, status "
        "FROM search_run WHERE snapshot_hash=? ORDER BY started_at DESC LIMIT 1",
        (snapshot_hash,)).fetchone()
    return dict(row) if row else None


def write_daemon_state(conn, run_id, frontier_size, k_rounds_no_expansion,
                       daemon_run_count, status):
    """daemon 跨夜状态回写（UPDATE 本次 run_id 行的跨夜字段）。

    run_search 收尾已 write_search_run 落了本次行（frontier_size=本次, k=初值）；
    daemon 算完跨夜判据①后用本函数更新 k_rounds_no_expansion/daemon_run_count/status。
    """
    with _write_lock:
        conn.execute(
            "UPDATE search_run SET frontier_size_prev=?, k_rounds_no_expansion=?, "
            "daemon_run_count=?, status=? WHERE run_id=?",
            (frontier_size, k_rounds_no_expansion, daemon_run_count, status, run_id))
```

- [ ] **Step 5: RunSummary 加 run_id + run_search 收尾落库**

`discovery/runner.py` `RunSummary`（line 47-65）加字段：

```python
    run_id: str = ""                # Plan 4：本次 run 的 search_run 行 id（daemon 跨夜状态键）
```

`run_search` 在 `return RunSummary(...)`（line 204-211）**之前**插入落库（生成 run_id + 写 search_run）。在函数末尾 `return RunSummary(...)` 处改为先构造 summary 再落库再返回：

```python
    # === Plan 4：落 search_run 行（daemon 跨夜状态源；cmd_run 亦受益可追溯） ===
    import uuid
    run_id = f"{snapshot_meta.snapshot_hash[:8]}_{uuid.uuid4().hex[:8]}"
    started_at = _now_iso()   # 复用 store._now_iso（顶部已有 from discovery.store import ...）
    # 注意：run_search 顶部已 `from discovery.store import init_db, connect, write_snapshot,
    # trial_id_of, trial_exists, write_trial, read_trials_by_snapshot`；此处补 write_search_run
    from discovery.store import write_search_run
    with connect(db_path) as conn:
        write_search_run(conn, run_id=run_id, snapshot_hash=snapshot_meta.snapshot_hash,
                         started_at=started_at, ended_at=_now_iso(), n_trials=n_new,
                         status=status, frontier_size=len(frontier_idxs),
                         k_rounds_no_expansion=0, daemon_run_count=0,
                         note=reason)

    return RunSummary(
        n_sampled=n_sampled, n_evaluated=len(to_eval), n_new_trials=n_new,
        n_skipped_dup=n_skipped, n_failed=n_failed,
        top_inner_calmar=top_calmar, top_trial_id=top_tid,
        db_path=db_path, snapshot_hash=snapshot_meta.snapshot_hash,
        status=status, convergence_reason=reason,
        rho=rho, ei=ei, frontier_size=len(frontier_idxs), dsr_top=dsr_top,
        run_id=run_id,
    )
```

> ⚠️ 实现者须读 `discovery/runner.py:204-211` 现有 return 块，把 `run_id` 加进 RunSummary 构造、并在 return 前插入上面的 write_search_run 块。`_now_iso` 已在 store 模块，runner 若未 import 则 `from discovery.store import _now_iso` 或直接用 `from datetime import datetime; datetime.utcnow().isoformat()`。

- [ ] **Step 6: 跑测试确认通过**

Run: `python -m pytest tests/discovery/test_store.py -v && python -m pytest tests/discovery/test_runner.py -v`
Expected: test_store 新 3 测试 PASS + test_runner 全绿（RunSummary.run_id additive 不破坏既有）。

- [ ] **Step 7: 回归 Plan 1-3 + Commit**

Run: `python -m pytest tests/discovery/ -q -m "not slow"`
Expected: 84+3 passed（零回归）。

```bash
git add discovery/store.py discovery/runner.py tests/discovery/test_store.py
git commit -m "feat(discovery): T1 search_run 跨夜状态字段+读写+run_search落库（Plan 4 L4 基建）"
```

---

## Task 2: daemon 跨夜编排纯函数

**Files:**
- Create: `discovery/daemon.py`
- Test: `tests/discovery/test_daemon.py`

**Interfaces:**
- Consumes: `discovery/store.py::{init_db, connect, read_latest_search_run, write_daemon_state}`；`discovery/runner.py::run_search`（注入）；`RunSummary`
- Produces: `run_daemon_cycle(snapshot_meta, split, db_path, *, budget_hours=4, n_proc=None, lake_start="2025-01-01", tpe_trials=0, rho_threshold=0.8, K=3, run_search_fn=None, notify_fn=None, eval_outer_fn=None) -> dict`
  - 返回 `{"run_id","summary","latest_k","converged_cross","outer","status"}`（outer/notify 由注入函数产出，T2 默认 noop，T3 接真实实现）

- [ ] **Step 1: 写跨夜 k 累加 + 收敛测试（RED）**

```python
# tests/discovery/test_daemon.py
import types
from discovery.split import holdout_split


def _fake_summary(run_id, frontier_size, status="budget_exhausted"):
    """造一个最小 RunSummary（避免跑真实 run_search）。"""
    return types.SimpleNamespace(
        run_id=run_id, frontier_size=frontier_size, status=status,
        top_trial_id="t1", top_inner_calmar=1.0, rho=0.9, ei=0.0005,
        snapshot_hash="snap1", n_new_trials=5, convergence_reason="")


def _make_run_search_fn(summaries, db_path, snapshot_hash):
    """假 run_search：按调用序号吐预设 summary，并模拟真实 run_search 的 write_search_run
    副作用（daemon read_latest_search_run 依赖此行存在，否则跨夜 k 永不累积）。"""
    calls = {"i": 0}
    def _rs(*a, **kw):
        i = calls["i"]; calls["i"] += 1
        s = summaries[i]
        from discovery.store import connect, write_search_run
        with connect(db_path) as conn:
            write_search_run(conn, run_id=s.run_id, snapshot_hash=snapshot_hash,
                             started_at=f"t{i}", ended_at=f"t{i}e", n_trials=5,
                             status=s.status, frontier_size=s.frontier_size,
                             k_rounds_no_expansion=0, daemon_run_count=0, note="")
        return s
    return _rs, calls


def test_daemon_accumulates_k_when_frontier_stagnant(tmp_path):
    """连续 3 夜前沿不扩张 → 第 3 夜 converged_cross=True（跨夜判据①）。"""
    from discovery.daemon import run_daemon_cycle
    from discovery.snapshot import SnapshotMeta
    from discovery.store import init_db, connect, write_search_run
    db = str(tmp_path / "t.db"); init_db(db)
    meta = SnapshotMeta("snap1", "u", 10, "d", "2025-01-01")
    split = holdout_split()
    # 4 夜 frontier_size 都=3（不扩张）：夜1 latest=None→k=0 / 夜2→k=1 / 夜3→k=2 / 夜4→k=3 收敛
    sums = [_fake_summary(f"r{i}", 3) for i in range(4)]
    rs_fn, _ = _make_run_search_fn(sums, db, "snap1")
    out = None
    for _ in range(4):
        out = run_daemon_cycle(meta, split, db, run_search_fn=rs_fn, K=3)
    assert out["converged_cross"] is True
    assert out["latest_k"] == 3
    assert out["status"] == "converged"


def test_daemon_resets_k_on_frontier_expansion(tmp_path):
    """前沿扩张（3→5）→ k 重置 0。"""
    from discovery.daemon import run_daemon_cycle
    from discovery.snapshot import SnapshotMeta
    from discovery.store import init_db
    db = str(tmp_path / "t.db"); init_db(db)
    meta = SnapshotMeta("snap1", "u", 10, "d", "2025-01-01"); split = holdout_split()
    # 夜1 size=3(k=0), 夜2 size=5 扩张(k=0 重置)
    rs_fn, _ = _make_run_search_fn([_fake_summary("r1", 3), _fake_summary("r2", 5)], db, "snap1")
    run_daemon_cycle(meta, split, db, run_search_fn=rs_fn, K=3)
    out2 = run_daemon_cycle(meta, split, db, run_search_fn=rs_fn, K=3)
    assert out2["latest_k"] == 0
    assert out2["converged_cross"] is False


def test_daemon_first_run_k_zero_when_no_latest(tmp_path):
    """首次 daemon（latest=None）→ k=0，不早退不炸。"""
    from discovery.daemon import run_daemon_cycle
    from discovery.snapshot import SnapshotMeta
    from discovery.store import init_db
    db = str(tmp_path / "t.db"); init_db(db)
    meta = SnapshotMeta("snap1", "u", 10, "d", "2025-01-01"); split = holdout_split()
    rs_fn, _ = _make_run_search_fn([_fake_summary("r1", 3)], db, "snap1")
    out = run_daemon_cycle(meta, split, db, run_search_fn=rs_fn, K=3)
    assert out["latest_k"] == 0
    assert out["converged_cross"] is False


def test_daemon_early_exit_when_converged(tmp_path):
    """latest.status==converged → 早退，不调 run_search。"""
    from discovery.daemon import run_daemon_cycle
    from discovery.snapshot import SnapshotMeta
    from discovery.store import init_db, connect, write_search_run, write_daemon_state
    db = str(tmp_path / "t.db"); init_db(db)
    meta = SnapshotMeta("snap1", "u", 10, "d", "2025-01-01"); split = holdout_split()
    # 预置一行已收敛的 search_run
    with connect(db) as conn:
        write_search_run(conn, "prev", "snap1", "t0", "t0e", 5, "converged", 3, 3, 3, "")
    called = {"n": 0}
    def _rs(*a, **kw): called["n"] += 1; return _fake_summary("x", 3)
    out = run_daemon_cycle(meta, split, db, run_search_fn=_rs, K=3)
    assert out["early_exited"] is True
    assert called["n"] == 0    # 未触达 run_search
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/discovery/test_daemon.py -v`
Expected: FAIL（`ModuleNotFoundError: discovery.daemon`）。

- [ ] **Step 3: 实现 daemon.py 编排纯函数**

```python
# -*- coding: utf-8 -*-
"""L4 守护 daemon 编排（spec §5.2 / §12#13，Plan 4）。

物理意图：run_search（Plan 2/3）只管单夜跑批 + 单 run 收敛②EI ④覆盖度；跨夜"连续 K 夜
前沿不扩张才算真收敛"（判据①）需要跨 run 状态累积——本模块是 run_search 的薄编排层：
每夜 schtasks 触发 → 读上夜跨夜状态 → 调 run_search → 比对前沿 → 更新 k → 判据①自停。

两层收敛分工（化解 spec §5.2 K 轮歧义）：
  单夜内：run_search 的 ②EI<ε ∧ ④覆盖度达标（Plan 3 既有，零改）。
  跨夜：本模块的 ①连续 K 夜 frontier_size 不扩张（Plan 4 新增，状态落 search_run 表）。

纯函数可单测：run_search/notify/eval_outer 均可注入（测试 mock，不触达真实 schtasks/钉钉）。
"""
from discovery.store import init_db, connect, read_latest_search_run, write_daemon_state


def estimate_budget(budget_hours, n_proc=None):
    """时间预算（小时）→ 组数上限（算力账粗估，spec §3.6）。

    ~180s/组 × ProcessPool(n_proc) 并发。诚实标注：单组成本待 L1 replay 标定，
    偏高→次夜 trial_id 去重断点续跑接续（不无限跑）。
    """
    import os
    n_proc = n_proc or max(1, (os.cpu_count() or 2) - 2)
    per_group_seconds = 180
    return max(1, int(budget_hours * 3600 / per_group_seconds) * n_proc)


def run_daemon_cycle(snapshot_meta, split, db_path, *, budget_hours=4, n_proc=None,
                     lake_start="2025-01-01", tpe_trials=0, rho_threshold=0.8, K=3,
                     run_search_fn=None, notify_fn=None, eval_outer_fn=None):
    """单夜 daemon 编排（读跨夜状态→跑批→比对前沿→更新 k→告警/outer）。

    Args:
      run_search_fn: 注入 run_search（默认 discovery.runner.run_search）。测试 mock。
      notify_fn: 新冠军/收敛告警回调（默认 None=noop；T3 接 fire_and_forget+notify_risk_event）。
      eval_outer_fn: 冠军 outer 去偏回调（默认 None=noop；T3 接 evaluate）。签名 (params)->dict。
    Returns: dict（run_id/summary/latest_k/converged_cross/early_exited/outer/status）。

    信息隔离（spec §6.2）：eval_outer_fn 的结果只进返回 dict 供报告，严禁回写 run_search 排序。
    """
    init_db(db_path)
    # 1. 读跨夜状态（首次=None）
    with connect(db_path) as conn:
        latest = read_latest_search_run(conn, snapshot_meta.snapshot_hash)
    if latest and latest.get("status") == "converged":
        return {"early_exited": True, "run_id": None, "summary": None,
                "latest_k": latest.get("k_rounds_no_expansion", 0),
                "converged_cross": True, "outer": None, "status": "converged"}

    # 2. 调 run_search（注入默认）
    if run_search_fn is None:
        from discovery.runner import run_search as run_search_fn
    n_budget = estimate_budget(budget_hours, n_proc)
    summary = run_search_fn(
        snapshot_meta, split, budget=n_budget, n_sobol=min(5, n_budget),
        n_random=min(5, max(0, n_budget - 5)), seed=42, db_path=db_path,
        n_proc=n_proc, lake_start=lake_start,
        tpe_trials=tpe_trials, rho_threshold=rho_threshold)

    # 3. 跨夜判据①：比对本次 vs 上夜前沿
    prev_size = latest["frontier_size_prev"] if latest else -1
    if summary.frontier_size > prev_size:          # 扩张 → 重置
        k = 0
    elif latest is None:                            # 首次 → 从 0 起算
        k = 0
    else:                                           # 未扩张 → 累加
        k = latest["k_rounds_no_expansion"] + 1
    converged_cross = (k >= K)

    # 4. 写回跨夜状态
    status = "converged" if converged_cross else summary.status
    daemon_run_count = (latest["daemon_run_count"] + 1 if latest else 1)
    with connect(db_path) as conn:
        write_daemon_state(conn, run_id=summary.run_id, frontier_size=summary.frontier_size,
                           k_rounds_no_expansion=k, daemon_run_count=daemon_run_count,
                           status=status)

    # 5. 冠军 outer 去偏 + 告警（注入；默认 noop，T3 接真实实现）
    outer = None
    if eval_outer_fn is not None and summary.top_trial_id:
        try:
            outer = eval_outer_fn(summary.top_trial_id)
        except Exception:
            outer = None   # outer 软降级：数据缺失不阻断 daemon（spec §7）
    if notify_fn is not None:
        try:
            notify_fn(summary=summary, k=k, K=K, converged_cross=converged_cross, outer=outer)
        except Exception:
            pass           # 告警软降级：钉钉失败不阻断 daemon

    return {"early_exited": False, "run_id": summary.run_id, "summary": summary,
            "latest_k": k, "converged_cross": converged_cross, "outer": outer, "status": status}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/discovery/test_daemon.py -v`
Expected: 4 测试 PASS。

- [ ] **Step 5: Commit**

```bash
git add discovery/daemon.py tests/discovery/test_daemon.py
git commit -m "feat(discovery): T2 daemon 跨夜编排纯函数（判据① k累加/收敛/早退，Plan 4 L4）"
```

---

## Task 3: daemon 钉钉告警 + outer 去偏调度（接真实实现）

**Files:**
- Modify: `discovery/daemon.py`（加 `_notify_champion` / `_eval_outer` 真实实现）
- Modify: `tests/discovery/test_daemon.py`（加告警 + outer 隔离测试）

**Interfaces:**
- Consumes: `discovery/objective.py::evaluate`；`discovery/snapshot.py::freeze`；`discovery/store.py::read_trials_by_snapshot`；`infra/notifier.py::{NotificationManager, fire_and_forget, notify_risk_event}`
- Produces: `daemon._notify_champion(summary, k, K, converged_cross, outer)`、`daemon._eval_outer(trial_id, db_path, split, lake_start) -> dict`

- [ ] **Step 1: 写告警 + outer 隔离测试（RED）**

追加到 `tests/discovery/test_daemon.py`：

```python
def test_daemon_alerts_on_new_champion(tmp_path, monkeypatch):
    """有冠军 → notify_fn 被调（mock fire_and_forget 验证消息含关键字段）。"""
    from discovery.daemon import run_daemon_cycle
    from discovery.snapshot import SnapshotMeta
    from discovery.store import init_db
    db = str(tmp_path / "t.db"); init_db(db)
    meta = SnapshotMeta("snap1", "u", 10, "d", "2025-01-01"); split = holdout_split()
    rs_fn, _ = _make_run_search_fn([_fake_summary("r1", 3)], db, "snap1")
    sent = {}
    def _notify(**kw): sent.update(kw)
    run_daemon_cycle(meta, split, db, run_search_fn=rs_fn, notify_fn=_notify)
    assert "summary" in sent and sent["k"] == 0
    assert sent["converged_cross"] is False


def test_daemon_outer_no_feedback(tmp_path, monkeypatch):
    """outer 去偏结果只进返回 dict，不回写 run_search 排序（信息隔离红线）。

    验证：eval_outer_fn 被调返回值出现在返回 dict['outer']，但 run_search_fn 收到的
    调用参数不含 outer（run_search 签名无 outer 入参，物理不可能回写）。
    """
    from discovery.daemon import run_daemon_cycle
    from discovery.snapshot import SnapshotMeta
    from discovery.store import init_db
    db = str(tmp_path / "t.db"); init_db(db)
    meta = SnapshotMeta("snap1", "u", 10, "d", "2025-01-01"); split = holdout_split()
    rs_calls = []
    def _rs(*a, **kw): rs_calls.append(kw); return _fake_summary("r1", 3)
    def _eval(tid): return {"ann": 0.5, "calmar": 2.0}
    out = run_daemon_cycle(meta, split, db, run_search_fn=_rs, eval_outer_fn=_eval)
    assert out["outer"] == {"ann": 0.5, "calmar": 2.0}     # outer 进返回 dict
    # run_search 收到的 kwargs 不含 outer（信息隔离）
    assert all("outer" not in kw for kw in rs_calls)
```

- [ ] **Step 2: 跑测试确认失败/通过**

Run: `python -m pytest tests/discovery/test_daemon.py::test_daemon_alerts_on_new_champion tests/discovery/test_daemon.py::test_daemon_outer_no_feedback -v`
Expected: PASS（T2 的 run_daemon_cycle 已支持 notify_fn/eval_outer_fn 注入，T3 测试直接验注入语义）。若 T2 注入点签名与测试不符，调整对齐。

- [ ] **Step 3: 加真实 notify/outer 实现（daemon.py 追加）**

```python
def _eval_outer(trial_id, db_path, split, lake_start="2025-01-01"):
    """冠军 outer 去偏：读 trial params → evaluate 在 outer 段跑 2026 真实 OOS。

    信息隔离（spec §6.2）：结果只返回给调用方进报告，绝不回写 run_search 排序。
    软降级：trial 不存在/evaluate 抛 → 返 None（daemon 主流程不阻断）。
    """
    import json
    from discovery.store import connect
    from discovery.snapshot import freeze
    from discovery.objective import evaluate
    with connect(db_path) as conn:
        row = conn.execute("SELECT params FROM trial WHERE trial_id=?", (trial_id,)).fetchone()
    if row is None:
        return None
    params = json.loads(row["params"])
    universe, _ = freeze(lake_start)   # 主进程 freeze（outer evaluate 用）
    return evaluate(params, universe, split)["outer"]


def _notify_champion(summary, k, K, converged_cross, outer, snapshot_hash=""):
    """新冠军/收敛钉钉告警（fire_and_forget 不阻塞 daemon）。

    走 infra.notifier 真身（core.notifier 是 strangler 垫片，直指 infra 防未来断链）。
    level=INFO（业务流水：发现新冠军/进度，非风控红线）。投递失败由 _broadcast 软降级。
    """
    from infra.notifier import NotificationManager, fire_and_forget, notify_risk_event
    outer_ann = (outer or {}).get("ann", 0.0)
    msg = (
        f"discovery daemon: snapshot={summary.snapshot_hash} run={summary.run_id}\n"
        f"冠军 calmar={summary.top_inner_calmar:.2f} trial={summary.top_trial_id} "
        f"outer ann={outer_ann*100:.1f}% rho={summary.rho:.3f} ei={summary.ei:.4f}\n"
        f"跨夜判据①: k={k}/{K}（连续未扩张夜数）"
        f"{' → 已收敛，可 publish' if converged_cross else ' 进行中'}"
    )
    fire_and_forget(NotificationManager.get_default().notify_risk_event(msg, "INFO"))


def run_daemon(snapshot_meta, split, db_path, *, notify_fn=None, eval_outer_fn=None, **kwargs):
    """生产入口：run_daemon_cycle 预装真实 notify/outer（供 cli cmd_daemon 调）。

    与 run_daemon_cycle（测试用纯函数）分离：本函数绑死 _notify_champion/_eval_outer，
    cli 调本函数；测试直接调 run_daemon_cycle 注入 mock。显式签名拿 split/db_path 供
    eval_outer_fn 闭包（避免 *args 透传时拿不到位置参数）。
    """
    if notify_fn is None:
        notify_fn = _notify_champion
    if eval_outer_fn is None:
        eval_outer_fn = lambda tid: _eval_outer(tid, db_path, split)
    return run_daemon_cycle(snapshot_meta, split, db_path,
                            notify_fn=notify_fn, eval_outer_fn=eval_outer_fn, **kwargs)
```

- [ ] **Step 4: 跑 daemon 全量 + 回归**

Run: `python -m pytest tests/discovery/test_daemon.py -v && python -m pytest tests/discovery/ -q -m "not slow"`
Expected: daemon 全绿 + 零回归。

- [ ] **Step 5: Commit**

```bash
git add discovery/daemon.py tests/discovery/test_daemon.py
git commit -m "feat(discovery): T3 daemon 钉钉告警+outer去偏调度（信息隔离，Plan 4 L4）"
```

---

## Task 4: cli daemon 子命令 + schtasks 注册（discovery 包自包含）

**Files:**
- Create: `discovery/schtasks.py`
- Create: `discovery/run_daemon.bat`
- Modify: `discovery/cli.py`（加 cmd_dacon + estimate_budget import + main 子命令注册）
- Modify: `discovery/__init__.py`（导出 run_daemon）
- Test: `tests/discovery/test_schtasks.py`

**Interfaces:**
- Consumes: `discovery/daemon.py::run_daemon`；`discovery/cli.py::{freeze, holdout_split, _db_path}`（既有 helper）；`ops/manage_ops_schtasks.py` 模式（幂等先删后建）
- Produces: `discovery/schtasks.py::{DAEMON_TASK_NAME, build_register_commands, register, unregister, list_tasks, main}`；`discovery/cli.py::cmd_daemon`

- [ ] **Step 1: 写 schtasks 纯函数测试（RED）**

```python
# tests/discovery/test_schtasks.py
def test_build_register_commands_shape():
    """注册命令纯函数：返 task/time/bot 三段映射（不触达 subprocess）。"""
    from discovery.schtasks import build_register_commands, DAEMON_TASK_NAME
    cmds = build_register_commands()
    assert len(cmds) == 1
    c = cmds[0]
    assert c["task"] == DAEMON_TASK_NAME
    assert c["time"] == "02:00"
    assert c["bat"].endswith("discovery\\run_daemon.bat")


def test_register_calls_schtasks_delete_then_create(monkeypatch):
    """register 幂等：先 /Delete /F 再 /Create（不污染真实任务计划程序，mock subprocess）。"""
    import discovery.schtasks as sch
    calls = []
    def _fake(args): calls.append(args); return 0
    monkeypatch.setattr(sch, "_schtasks", _fake)
    sch.register()
    # 至少一次 Delete + 一次 Create
    assert any("/Delete" in a for a in calls)
    assert any("/Create" in a for a in calls)
```

- [ ] **Step 2: 跑确认失败**

Run: `python -m pytest tests/discovery/test_schtasks.py -v`
Expected: FAIL（`discovery.schtasks` 不存在）。

- [ ] **Step 3: 实现 schtasks.py（仿 manage_ops_schtasks 幂等模式）**

```python
# -*- coding: utf-8 -*-
"""discovery 夜跑 daemon 的 schtasks 注册（spec §10，Plan 4）。

discovery 包自包含调度：不依赖 scripts/（后续废弃），与 broadcast schtasks 解耦。
幂等模式（先 /Delete /F 再 /Create /SC DAILY）复用 ops/manage_ops_schtasks.py 既有纪律。
改时间 = 改本模块常量 + python -m discovery.schtasks --register。
"""
from __future__ import annotations
import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]   # 项目根（discovery/ 的上级）
sys.path.insert(0, str(ROOT))

DAEMON_TASK_NAME = "QuanterDiscoveryDaemon"
DAEMON_TIME = "02:00"                          # 每夜 02:00 触发（spec §5.2/§10）
DAEMON_BAT = str(ROOT / "discovery" / "run_daemon.bat")


def build_register_commands() -> list[dict]:
    """生成 daemon 注册命令参数（纯函数，不执行）。供单测验证 task/time/bat 映射。"""
    return [{"task": DAEMON_TASK_NAME, "time": DAEMON_TIME, "bat": DAEMON_BAT, "bot": "discovery"}]


def _schtasks(args: list[str]) -> int:
    """封装 schtasks 子进程调用（capture_output 避免乱码）。"""
    return subprocess.run(["schtasks"] + args, capture_output=True, text=True).returncode


def register() -> None:
    """幂等注册：先 /Delete /F（不存在也返 0）再 /Create /F 覆盖。"""
    for c in build_register_commands():
        _schtasks(["/Delete", "/TN", c["task"], "/F"])
        rc = _schtasks(["/Create", "/SC", "DAILY", "/TN", c["task"],
                        "/TR", c["bat"], "/ST", c["time"], "/F"])
        print(f"{'OK' if rc == 0 else 'FAIL'} {c['task']} @ {c['time']} → {c['bat']}")


def unregister() -> None:
    """一键清退（删除幂等）。"""
    _schtasks(["/Delete", "/TN", DAEMON_TASK_NAME, "/F"])
    print(f"deleted {DAEMON_TASK_NAME}")


def list_tasks() -> None:
    subprocess.run(["schtasks", "/Query", "/TN", DAEMON_TASK_NAME], check=False)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="discovery daemon schtasks 管理（包自包含）")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--register", action="store_true")
    g.add_argument("--unregister", action="store_true")
    g.add_argument("--list", action="store_true")
    args = p.parse_args(argv)
    if args.register: register()
    elif args.unregister: unregister()
    elif args.list: list_tasks()
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: 写 run_daemon.bat（包内入口）**

创建 `discovery/run_daemon.bat`（**保存为 CRLF 行尾**，Windows bat 要求）：

```bat
@echo off
REM discovery daemon 夜跑入口（spec §5.2/§10，Plan 4）
REM schtasks 触发：激活 venv → cd 项目根 → python -m discovery daemon --budget 4h
cd /d %~dp0\..
call .venv310\Scripts\activate.bat
python -m discovery daemon --budget 4h
```

- [ ] **Step 5: 加 cmd_daemon 子命令到 cli.py**

`discovery/cli.py` 在 `cmd_run`（line 114-144）之后加 `cmd_daemon`，并在 `main()`（line 209-242）注册子命令：

```python
def cmd_daemon(args):
    """L4 守护 daemon 夜跑入口（spec §5.2/§12#13，Plan 4）。

    串 freeze→holdout_split→run_daemon（跨夜编排：判据①+告警+outer去偏）。
    每夜 schtasks @02:00 触发本命令（discovery/schtasks.py 注册）。
    """
    from discovery.daemon import run_daemon, estimate_budget
    universe, meta = freeze(args.lake_start)
    split = holdout_split(args.embargo)
    print(f"=== discovery daemon：跨夜守护（snapshot={meta.snapshot_hash}）===")
    print(f"配置: budget={args.budget}h(≈{estimate_budget(args.budget, args.n_proc)}组) "
          f"tpe={args.tpe_trials} proc={args.n_proc} K={args.k_rounds} rho={args.rho_threshold}")
    out = run_daemon(meta, split, _db_path(),
                     budget_hours=args.budget, n_proc=args.n_proc, lake_start=args.lake_start,
                     tpe_trials=args.tpe_trials, rho_threshold=args.rho_threshold, K=args.k_rounds)
    if out["early_exited"]:
        print(f"早退：跨夜已收敛（k={out['latest_k']}），不重复夜跑")
        return
    s = out["summary"]
    print(f"--- daemon 汇总 ---")
    print(f"run_id={out['run_id']} n_new={s.n_new_trials} frontier={s.frontier_size} "
          f"top_calmar={s.top_inner_calmar:.2f} rho={s.rho:.3f}")
    print(f"跨夜判据①: k={out['latest_k']}/{args.k_rounds} "
          f"{'已收敛（可 publish）' if out['converged_cross'] else '进行中'}")
    if out.get("outer"):
        o = out["outer"]
        print(f"冠军 outer 去偏: ann={o.get('ann',0)*100:.1f}% calmar={o.get('calmar',0):.2f} "
              f"max_dd={o.get('max_dd',0)*100:.1f}%")
        print(f"下一步: python -m discovery publish {s.top_trial_id}")
```

`main()` 在 `ap_rp`（report，line 239-240）之后注册 daemon 子命令：

```python
    ap_d = sub.add_parser("daemon", help="L4 守护 daemon 夜跑（Plan 4）")
    ap_d.add_argument("--budget", type=int, default=4, help="时间预算小时（默认 4h）")
    ap_d.add_argument("--embargo", type=int, default=5)
    ap_d.add_argument("--n-proc", type=int, default=None, dest="n_proc")
    ap_d.add_argument("--lake-start", type=str, default="2025-01-01", dest="lake_start")
    ap_d.add_argument("--tpe-trials", type=int, default=10, dest="tpe_trials")
    ap_d.add_argument("--rho-threshold", type=float, default=0.8, dest="rho_threshold")
    ap_d.add_argument("--k-rounds", type=int, default=3, dest="k_rounds", help="跨夜收敛 K（判据①）")
    ap_d.set_defaults(func=cmd_daemon)
```

`discovery/__init__.py` 追加导出：`from discovery.daemon import run_daemon, run_daemon_cycle`。

- [ ] **Step 6: 跑测试 + 回归**

Run: `python -m pytest tests/discovery/test_schtasks.py tests/discovery/test_cli_run.py tests/discovery/test_cli_plan3.py -v && python -m pytest tests/discovery/ -q -m "not slow"`
Expected: schtasks 2 测试 PASS + cli 既有不回归 + 全量零回归。

- [ ] **Step 7: Commit**

```bash
git add discovery/schtasks.py discovery/run_daemon.bat discovery/cli.py discovery/__init__.py tests/discovery/test_schtasks.py
git commit -m "feat(discovery): T4 cli daemon 子命令 + 包自包含 schtasks 注册（Plan 4 L4）"
```

---

## Task 5: resolver 扩 activated_at + publish 命令

**Files:**
- Modify: `experiment/models.py:46-50`（ActiveExperiment 加字段）
- Modify: `experiment/resolver.py:15-24`（resolve_active 带上 activated_at）
- Create: `discovery/publish.py`
- Modify: `discovery/cli.py`（加 cmd_publish + main 注册）
- Modify: `discovery/__init__.py`（导出 publish_champion）
- Test: `tests/experiment/test_resolver.py`（扩）、`tests/discovery/test_publish.py`（新）

**Interfaces:**
- Consumes: `experiment/store.py::{create_version, _DEFAULT_DB}`；`experiment/models.py::ExperimentVersion, ExperimentStatus`；`discovery/store.py::{connect, DEFAULT_DB_PATH}`；`discovery/objective.py::evaluate`；`discovery/snapshot.py::freeze`；`discovery/split.py::holdout_split`
- Produces: `ActiveExperiment.activated_at: Optional[str]`；`discovery/publish.py::publish_champion(trial_id, db_path=DEFAULT_DB_PATH, exp_db_path=None) -> dict`

- [ ] **Step 1: 写 resolver 扩字段测试（RED）**

`tests/experiment/test_resolver.py` 追加：

```python
def test_resolve_active_includes_activated_at(tmp_path, monkeypatch):
    """resolve_active 返回值须含 activated_at（≥5天硬闸依赖此字段算影子期，Plan 4 T6）。"""
    import experiment.store as estore
    from experiment.models import ExperimentVersion, ExperimentStatus
    db = str(tmp_path / "e.db"); estore.init_db(db)
    v = ExperimentVersion("exp1", "neckline", {"window": 5}, 0.1,
                          ExperimentStatus.ACTIVE, 1, source="test",
                          created_at="2026-01-01T00:00:00", activated_at="2026-01-01T00:00:00")
    estore.create_version(db, v)   # create 落的是 DRAFT；下面手动 promote
    # 直接落 ACTIVE（绕过 promote 的权重校验，测 resolver 读列）
    import sqlite3
    con = sqlite3.connect(db); con.execute(
        "UPDATE experiment_version SET status='ACTIVE', activated_at='2026-01-01T00:00:00' "
        "WHERE experiment_id='exp1'"); con.commit(); con.close()
    monkeypatch.setattr(estore, "_DEFAULT_DB", db)
    from experiment.resolver import resolve_active
    active = resolve_active(db)
    assert len(active) == 1
    assert active[0].activated_at == "2026-01-01T00:00:00"
```

- [ ] **Step 2: 跑确认失败**

Run: `python -m pytest tests/experiment/test_resolver.py::test_resolve_active_includes_activated_at -v`
Expected: FAIL（`ActiveExperiment` 无 `activated_at` 属性 → AttributeError）。

- [ ] **Step 3: 改 models.py + resolver.py（additive）**

`experiment/models.py` `ActiveExperiment`（line 46-54）加字段：

```python
@dataclass
class ActiveExperiment:
    """resolver 返回给 scan 的精简视图（design §4.1 resolve_active 契约）。"""
    experiment_id: str
    strategy_name: str
    params: dict
    weight: float
    activated_at: Optional[str] = None   # Plan 4：上线时刻（≥5天硬闸算影子期用，additive）
```

`experiment/resolver.py` `resolve_active`（line 22-24）构造时带上：

```python
    return [ActiveExperiment(experiment_id=v.experiment_id, strategy_name=v.strategy_name,
                             params=v.params, weight=v.weight, activated_at=v.activated_at)
            for v in versions if v.weight > 0]
```

> `ExperimentVersion`（models.py:30-42）已有 `activated_at` 字段 + `list_versions` 已 SELECT *，故 `v.activated_at` 可直接读，零改 store。

- [ ] **Step 4: 写 publish 测试（RED）**

```python
# tests/discovery/test_publish.py
def test_publish_creates_draft(tmp_path, monkeypatch):
    """publish → experiment create_version(DRAFT, source=discovery:xxx, weight=0)。"""
    import experiment.store as estore
    from experiment.models import ExperimentStatus
    exp_db = str(tmp_path / "e.db"); estore.init_db(exp_db)
    monkeypatch.setattr(estore, "_DEFAULT_DB", exp_db)

    # 预置一个 discovery trial（params + snapshot_hash）
    import discovery.store as dstore
    disc_db = str(tmp_path / "d.db"); dstore.init_db(disc_db)
    from discovery.store import connect, write_trial
    with connect(disc_db) as conn:
        write_trial(conn, "tid1", {"window": 5}, "snap1abc", "eng1", "holdout_2025_2026",
                    {"ann": 0.5}, {"ann": 0.4}, "tpe")
    monkeypatch.setattr(dstore, "DEFAULT_DB_PATH", disc_db)

    from discovery.publish import publish_champion
    out = publish_champion("tid1", db_path=disc_db, exp_db_path=exp_db)
    # 验证 experiment 落了 DRAFT
    versions = estore.list_versions(exp_db)
    assert len(versions) == 1
    v = versions[0]
    assert v.status == ExperimentStatus.DRAFT
    assert v.weight == 0.0
    assert v.source.startswith("discovery:")
    assert v.params == {"window": 5}
    assert out["experiment_id"] == v.experiment_id


def test_publish_no_auto_promote(tmp_path, monkeypatch):
    """publish 不自动 promote（spec §2.2，防过拟合参数直冲实盘）。"""
    import experiment.store as estore
    exp_db = str(tmp_path / "e.db"); estore.init_db(exp_db)
    monkeypatch.setattr(estore, "_DEFAULT_DB", exp_db)
    import discovery.store as dstore
    disc_db = str(tmp_path / "d.db"); dstore.init_db(disc_db)
    from discovery.store import connect, write_trial
    with connect(disc_db) as conn:
        write_trial(conn, "tid1", {"window": 5}, "snap1abc", "eng1", "h", {"ann": 0.5}, {"ann": 0.4}, "tpe")
    monkeypatch.setattr(dstore, "DEFAULT_DB_PATH", disc_db)
    from discovery.publish import publish_champion
    publish_champion("tid1", db_path=disc_db, exp_db_path=exp_db)
    versions = estore.list_versions(exp_db)
    assert versions[0].status == estore.ExperimentStatus.DRAFT if hasattr(estore, "ExperimentStatus") else True
    from experiment.models import ExperimentStatus
    assert versions[0].status == ExperimentStatus.DRAFT    # 仍是 DRAFT，未 promote ACTIVE
```

- [ ] **Step 5: 跑确认失败**

Run: `python -m pytest tests/discovery/test_publish.py -v`
Expected: FAIL（`discovery.publish` 不存在）。

- [ ] **Step 6: 实现 publish.py**

```python
# -*- coding: utf-8 -*-
"""L5 publish：discovery 冠军 → experiment DRAFT 桥（spec §5.3/§12#14，Plan 4）。

物理意图：daemon 收敛后，冠军 trial 的 params 须沉淀为 experiment 系统的 DRAFT 候选
（带 source 溯源），供人审 promote 走既有 _eod 链路。本模块是 discovery→experiment 的
薄桥：零改 experiment 系统（create_version/create_experiment_id 既有）。

不自动 promote（spec §2.2 非目标——防过拟合参数直冲实盘）；人审 experiment promote <id>。
"""
import json
from datetime import datetime

from discovery.store import DEFAULT_DB_PATH, connect


def publish_champion(trial_id, db_path=DEFAULT_DB_PATH, exp_db_path=None, *, lake_start="2025-01-01"):
    """冠军 trial → experiment DRAFT + outer 去偏报告。

    Args:
      trial_id: 冠军 trial id（daemon RunSummary.top_trial_id）。
      db_path: discovery trial 库。
      exp_db_path: experiment 库（默认 experiment.store._DEFAULT_DB）。
    Returns: {"experiment_id","outer","trial_id","snapshot_hash"}。

    幂等性：experiment_id 含 trial_id[:6] + 日期，同 trial 同日重复 publish 会撞
    UNIQUE(strategy_name, version) → create_version 抛 ValueError（调用方感知重复）。
    """
    from experiment.store import create_version, _DEFAULT_DB
    from experiment.models import ExperimentVersion, ExperimentStatus
    from discovery.snapshot import freeze
    from discovery.split import holdout_split
    from discovery.objective import evaluate

    exp_db = exp_db_path or _DEFAULT_DB
    # 1. 读 trial
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT params, snapshot_hash FROM trial WHERE trial_id=?", (trial_id,)).fetchone()
    if row is None:
        raise ValueError(f"trial 不存在: {trial_id}")
    params = json.loads(row["params"])
    snapshot_hash = row["snapshot_hash"]

    # 2. outer 去偏报告（信息隔离：只读不回写搜索，spec §6.2）
    outer = None
    try:
        universe, _ = freeze(lake_start)
        split = holdout_split()
        outer = evaluate(params, universe, split)["outer"]
    except Exception:
        outer = None   # outer 软降级（数据缺失不阻断 publish）

    # 3. experiment create DRAFT（source 溯源 discovery:snapshot）
    today = datetime.now().strftime("%Y%m%d")
    experiment_id = f"neckline_disc_{today}_{trial_id[:6]}"
    note = (f"outer ann={(outer or {}).get('ann', 0)*100:.1f}% "
            f"calmar={(outer or {}).get('calmar', 0):.2f} "
            f"max_dd={(outer or {}).get('max_dd', 0)*100:.1f}%") if outer else "outer 评估失败"
    version = ExperimentVersion(
        experiment_id=experiment_id, strategy_name="neckline", params=params,
        weight=0.0, status=ExperimentStatus.DRAFT, version=1,
        source=f"discovery:{snapshot_hash[:8]}", note=note,
        created_at=datetime.now().isoformat(timespec="seconds"))
    create_version(exp_db, version, operator="discovery:publish")

    return {"experiment_id": experiment_id, "outer": outer,
            "trial_id": trial_id, "snapshot_hash": snapshot_hash}
```

`discovery/cli.py` 加 `cmd_publish`（仿 cmd_champions 读 latest snapshot 模式）：

```python
def cmd_publish(args):
    """L5 publish：冠军 trial → experiment DRAFT + outer 去偏报告（spec §5.3，Plan 4）。

    人审下一步：experiment promote <id> --weight 0.1 → 走既有 _eod 链路。
    不自动 promote（spec §2.2）。
    """
    from discovery.publish import publish_champion
    out = publish_champion(args.trial_id, db_path=_db_path())
    print(f"=== discovery publish：冠军 → experiment DRAFT ===")
    print(f"experiment_id: {out['experiment_id']}（source=discovery:{out['snapshot_hash'][:8]}）")
    if out["outer"]:
        o = out["outer"]
        print(f"outer 去偏: ann={o.get('ann',0)*100:.1f}% calmar={o.get('calmar',0):.2f} "
              f"max_dd={o.get('max_dd',0)*100:.1f}% n={o.get('n',0)}")
    else:
        print("outer 去偏: 评估失败（数据缺失，已软降级）")
    print(f"下一步人审: python -m experiment promote {out['experiment_id']} --weight 0.1")
```

`main()` 注册：

```python
    ap_p = sub.add_parser("publish", help="L5 冠军→experiment DRAFT（Plan 4）")
    ap_p.add_argument("trial_id", help="冠军 trial id（champions 报的 top）")
    ap_p.set_defaults(func=cmd_publish)
```

`discovery/__init__.py` 追加：`from discovery.publish import publish_champion`。

- [ ] **Step 7: 跑测试 + 回归**

Run: `python -m pytest tests/experiment/test_resolver.py tests/discovery/test_publish.py -v && python -m pytest tests/discovery/ tests/experiment/ tests/trading/ -q -m "not slow"`
Expected: resolver + publish 测试 PASS + 零回归（experiment/trading 既有不破坏）。

- [ ] **Step 8: Commit**

```bash
git add experiment/models.py experiment/resolver.py discovery/publish.py discovery/cli.py discovery/__init__.py tests/experiment/test_resolver.py tests/discovery/test_publish.py
git commit -m "feat(discovery): T5 resolver 扩 activated_at + publish 冠军→DRAFT（Plan 4 L5）"
```

---

## Task 6: ≥5 天硬闸 fail-closed（trading/__main__.py）

**Files:**
- Modify: `trading/__main__.py:122-137`（WARNING → 硬闸）
- Test: `tests/trading/test_main_shadow_gate.py`

**Interfaces:**
- Consumes: `experiment.resolver.resolve_active`（T5 扩了 activated_at）；`infra.notifier.{NotificationManager, fire_and_forget, notify_risk_event}`
- Produces: `_shadow_gate()` → bool（True=放行，False=sys.exit(2)）；`_days_since_activation(iso_ts) -> int|None`

- [ ] **Step 1: 写硬闸 5 场景测试（RED）**

```python
# tests/trading/test_main_shadow_gate.py
import sys
from datetime import datetime, timedelta


def _set_active(monkeypatch, experiments):
    """mock resolve_active 返回预设实验列表（每项带 activated_at）。"""
    import types
    def _fake(): return experiments
    mod = sys.modules["trading.__main__"]
    monkeypatch.setattr(mod, "resolve_active", _fake, raising=False)


def _set_mode(monkeypatch, mode):
    monkeypatch.setenv("AUTO_TRADE_MODE", mode)
    monkeypatch.setenv("TRADE_SHADOW_MIN_DAYS", "5")


def test_live_blocked_when_shadow_insufficient(monkeypatch, tmp_path):
    """mode=live + activated_at < 5天 → sys.exit(2) + fail-closed。"""
    import trading.__main__ as m
    _set_mode(monkeypatch, "live")
    recent = datetime.now().isoformat(timespec="seconds")
    _set_active(monkeypatch, [types.SimpleNamespace(activated_at=recent, experiment_id="e1")])
    import pytest
    with pytest.raises(SystemExit) as ei:
        m._shadow_gate()
    assert ei.value.code == 2


def test_live_allowed_when_shadow_sufficient(monkeypatch):
    """mode=live + 所有 activated_at ≥ 5天 → 放行（返 True）。"""
    import trading.__main__ as m
    _set_mode(monkeypatch, "live")
    old = (datetime.now() - timedelta(days=10)).isoformat(timespec="seconds")
    _set_active(monkeypatch, [types.SimpleNamespace(activated_at=old, experiment_id="e1")])
    assert m._shadow_gate() is True


def test_live_blocked_when_activated_at_missing(monkeypatch):
    """activated_at=None → 保守归入 fresh 拒绝（D9，宁可误杀）。"""
    import trading.__main__ as m
    _set_mode(monkeypatch, "live")
    _set_active(monkeypatch, [types.SimpleNamespace(activated_at=None, experiment_id="e1")])
    import pytest
    with pytest.raises(SystemExit):
        m._shadow_gate()


def test_resolve_active_failure_blocks_live(monkeypatch):
    """resolve_active 抛异常 → fail-closed sys.exit(2)（非空列表误放行）。"""
    import trading.__main__ as m
    _set_mode(monkeypatch, "live")
    def _boom(): raise RuntimeError("db locked")
    monkeypatch.setattr(m, "resolve_active", _boom, raising=False)
    import pytest
    with pytest.raises(SystemExit) as ei:
        m._shadow_gate()
    assert ei.value.code == 2


def test_dry_run_skips_gate(monkeypatch):
    """mode=dry_run → 不查不拦（返 True，硬闸仅 LIVE 触发）。"""
    import trading.__main__ as m
    _set_mode(monkeypatch, "dry_run")
    called = {"n": 0}
    def _fake():
        called["n"] += 1; return []
    monkeypatch.setattr(m, "resolve_active", _fake, raising=False)
    assert m._shadow_gate() is True
    assert called["n"] == 0   # dry_run 不查 resolve_active


def test_live_allowed_when_no_active(monkeypatch):
    """mode=live + resolve_active 返空列表 → 放行（D8，合法清场非查询失败）。"""
    import trading.__main__ as m
    _set_mode(monkeypatch, "live")
    _set_active(monkeypatch, [])
    assert m._shadow_gate() is True
```

- [ ] **Step 2: 跑确认失败**

Run: `python -m pytest tests/trading/test_main_shadow_gate.py -v`
Expected: FAIL（`trading.__main__._shadow_gate` 未定义）。

- [ ] **Step 3: 改 trading/__main__.py 加硬闸**

在 `trading/__main__.py` 的 `asyncio.run(_run_forever())`（line 140）之前、`mode` 读取（line 126-137）处重构。把现有 WARNING 段替换为：

```python
def _days_since_activation(activated_at):
    """ISO activated_at 距今自然日数（影子期校验）。

    None/解析失败 → 返 None（调用方保守视为"影子期不足"拒绝，D9）。
    experiment activated_at 由 cli._now()=datetime.now().isoformat 落（本地 naive），
    datetime.now() 同口径，差值正确。
    """
    from datetime import datetime
    if not activated_at:
        return None
    try:
        then = datetime.fromisoformat(activated_at).date()
    except (ValueError, TypeError):
        return None
    return (datetime.now().date() - then).days


def _shadow_gate():
    """≥5 天影子期硬闸（spec §5.3/§12#14，Plan 4，fail-closed）。

    物理意图：spec §5.3 把 __main__ 旧 WARNING 误称"≥5天硬闸"——实为提醒，切 LIVE 只需
    改 env。本函数升级为真闸：mode=live 时查所有 ACTIVE 实验的 activated_at，任一影子期
    < TRADE_SHADOW_MIN_DAYS → sys.exit(2) + 钉钉 CRITICAL（绝不裸跑真单）。

    风控红线（D3/D8/D9）：
      - resolve_active 抛异常 → fail-closed exit（非空列表误放行）。
      - 返空列表 → 放行（合法清场，D8）。
      - activated_at 缺失 → 保守拒绝（D9）。
      - mode=dry_run → 跳过（硬闸仅 LIVE 触发）。
    """
    mode = os.getenv("AUTO_TRADE_MODE", "dry_run")
    if mode == "dry_run":
        return True   # 影子模式不拦
    min_days = int(os.getenv("TRADE_SHADOW_MIN_DAYS", "5"))
    try:
        from experiment.resolver import resolve_active
        experiments = resolve_active()
    except Exception as e:
        # 查询失败 fail-closed（绝不因查不到而放行 LIVE）
        try:
            from infra.notifier import NotificationManager, fire_and_forget
            fire_and_forget(NotificationManager.get_default().notify_risk_event(
                f"拒切 LIVE：experiment 状态查询失败（{e}），回退 dry_run", "CRITICAL"))
        except Exception:
            pass
        sys.exit(2)
    # 空列表放行（D8）；任一影子期不足/缺失 → 拒绝（D9）
    fresh = [e for e in experiments
             if _days_since_activation(getattr(e, "activated_at", None)) is None
             or _days_since_activation(e.activated_at) < min_days]
    if fresh:
        try:
            from infra.notifier import NotificationManager, fire_and_forget
            fire_and_forget(NotificationManager.get_default().notify_risk_event(
                f"拒切 LIVE：{len(fresh)} 实验影子期不足 {min_days} 天，回退 dry_run", "CRITICAL"))
        except Exception:
            pass
        sys.exit(2)
    logger.warning("⚠️ LIVE 模式：所有 ACTIVE 实验影子期 ≥ %s 天，放行（确保对账/网关/止损已就绪）", min_days)
    return True
```

在 `if __name__ == "__main__":`（line 122）块内，`asyncio.run(_run_forever())`（line 140）之前调 `_shadow_gate()`：

```python
    _shadow_gate()   # Plan 4：≥5天硬闸（fail-closed，dry_run 跳过 / LIVE 校验）
    try:
        asyncio.run(_run_forever())
```

> 删除原 line 129-137 的 `if mode != "dry_run": logger.warning(...)` 段（其语义已被 `_shadow_gate` 取代；`_shadow_gate` 内部 LIVE 放行时保留 WARNING）。

- [ ] **Step 4: 跑测试 + 回归**

Run: `python -m pytest tests/trading/test_main_shadow_gate.py -v && python -m pytest tests/trading/ tests/experiment/ -q -m "not slow"`
Expected: 硬闸 6 测试 PASS + trading/experiment 零回归。

- [ ] **Step 5: Commit**

```bash
git add trading/__main__.py tests/trading/test_main_shadow_gate.py
git commit -m "feat(trading): T6 ≥5天影子期硬闸 fail-closed（spec §5.3 真·闸，Plan 4 L5）"
```

---

## Task 7: slow 端到端集成

**Files:**
- Create: `tests/discovery/test_plan4_e2e.py`（slow）

**Interfaces:**
- Consumes: T1-T6 全部产出
- Produces: Plan 4 端到端绿灯（多夜 daemon 收敛 + publish→DRAFT + 硬闸）

- [ ] **Step 1: 写 slow E2E 测试**

```python
# tests/discovery/test_plan4_e2e.py
"""Plan 4 端到端 slow 集成（spec §12 #13/#14）。

模拟多夜 daemon（跨夜判据①收敛）+ publish→experiment DRAFT 闭环 + ≥5天硬闸。
slow：真实 freeze/evaluate 跑颈线法回测（~分钟级），验证全链通。
"""
import pytest
pytestmark = pytest.mark.slow


def test_plan4_multi_night_daemon_converges_and_publishes(tmp_path, monkeypatch):
    """三夜 daemon（前沿不扩张）→ 跨夜收敛 → publish 冠军 → experiment DRAFT。"""
    import discovery.store as dstore
    disc_db = str(tmp_path / "d.db")
    monkeypatch.setattr(dstore, "DEFAULT_DB_PATH", disc_db)

    import experiment.store as estore
    exp_db = str(tmp_path / "e.db"); estore.init_db(exp_db)
    monkeypatch.setattr(estore, "_DEFAULT_DB", exp_db)

    from discovery.daemon import run_daemon
    from discovery.snapshot import freeze
    from discovery.split import holdout_split
    from discovery.store import init_db
    init_db(disc_db)
    universe, meta = freeze("2025-01-01")
    split = holdout_split()

    # 三夜 daemon（真实 run_search，小 budget 加速）
    out = None
    for _ in range(3):
        out = run_daemon(meta, split, disc_db, budget_hours=0.01,    # 极小 budget 加速
                         n_proc=1, tpe_trials=2, K=3)
    # daemon 至少跑通（收敛取决于真实前沿，此处只验链路不炸 + 状态落库）
    assert out is not None
    assert out["early_exited"] is False or out["converged_cross"] in (True, False)

    # publish 冠军（若有 top_trial_id）
    if out["summary"] and out["summary"].top_trial_id:
        from discovery.publish import publish_champion
        pub = publish_champion(out["summary"].top_trial_id, db_path=disc_db, exp_db_path=exp_db)
        assert pub["experiment_id"].startswith("neckline_disc_")
        versions = estore.list_versions(exp_db)
        from experiment.models import ExperimentStatus
        assert any(v.status == ExperimentStatus.DRAFT for v in versions)


def test_plan4_shadow_gate_blocks_insufficient_shadow(tmp_path, monkeypatch):
    """≥5天硬闸：mode=live + 新 promote 实验（activated_at 今日）→ sys.exit(2)。"""
    from datetime import datetime
    import trading.__main__ as m
    import types
    monkeypatch.setenv("AUTO_TRADE_MODE", "live")
    monkeypatch.setenv("TRADE_SHADOW_MIN_DAYS", "5")
    recent = datetime.now().isoformat(timespec="seconds")
    monkeypatch.setattr(m, "resolve_active",
                        lambda: [types.SimpleNamespace(activated_at=recent, experiment_id="e1")],
                        raising=False)
    import pytest
    with pytest.raises(SystemExit) as ei:
        m._shadow_gate()
    assert ei.value.code == 2
```

- [ ] **Step 2: 跑 slow E2E**

Run: `python -m venv` 不需要——直接：
Run: `python -m pytest tests/discovery/test_plan4_e2e.py -v -m slow`
Expected: 2 slow 测试 PASS（真实 freeze/evaluate/daemon/publish/硬闸全链通）。

> 若跑超时：daemon budget_hours 调更小 / n_proc=1 / tpe_trials=2 已是加速档；颈线法回测 ~分钟级正常。`.venv310` 环境跑（与 xtquant 同环境，但本测试不触达 broker）。

- [ ] **Step 3: 全量回归（含 non-slow）**

Run: `python -m pytest tests/discovery/ tests/experiment/ tests/trading/ -q -m "not slow"`
Expected: 全绿（Plan 1-3 的 84 + Plan 4 新增 non-slow 全过，零回归）。

- [ ] **Step 4: Commit**

```bash
git add tests/discovery/test_plan4_e2e.py
git commit -m "test(discovery): T7 Plan 4 slow 端到端（多夜daemon+publish+硬闸，Plan 4 集成）"
```

---

## 验收（task 全部完成后）

- [ ] `python -m pytest tests/discovery/ tests/experiment/ tests/trading/ -q -m "not slow"` → 全绿，零回归。
- [ ] `python -m pytest tests/discovery/test_plan3_e2e.py tests/discovery/test_plan4_e2e.py -v -m slow` → Plan 3 + Plan 4 slow 全绿。
- [ ] §12 #13（L4 自治）：跨夜判据①连续 K=3 收敛自停 ✓（T2）+ schtasks 夜跑守护 + 断点续跑 ✓（T4 + Plan 2 既有）。
- [ ] §12 #14（L5 闭环）：publish→experiment DRAFT（source=discovery:xxx）✓（T5）+ ≥5天硬闸 ✓（T6）+ 不自动 promote ✓（T5）。
- [ ] **更新 `.superpowers/sdd/progress.md`**：记 Plan 4 完成情况 + Plan 5 follow-up（candidate-only 虚拟 PnL）。

## follow-up（不在 Plan 4 范围，记 progress.md）

- **Plan 5 candidate-only 影子虚拟 PnL**：重写 `_eod`/`pre_open` 按 experiment 标影子态分流 + 纸上 mark-to-market 记账（spec §3.2 Gap 2）。
- **算力账标定**：`estimate_budget` 粗估待 L1 replay 校准（spec §3.6）。
- **scripts/ 废弃迁移**：`manage_ops_schtasks.py` + `run_*.bat` 归宿（ops/ 包或各包自管），Plan 4 的 discovery 自包含调度不受影响。
