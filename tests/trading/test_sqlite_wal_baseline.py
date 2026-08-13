# -*- coding: utf-8 -*-
"""Task G6：SQLite WAL + timeout 基线测试（DG-G6 · g-wave-p0-guards）。

物理意图：
    job_ledger / state_store 的 _connect 防护落后于 backtest/tasks_db.py +
    discovery/store.py 正范式——缺 timeout=30（默认 5s）/缺 busy_timeout，且
    job_ledger 连 WAL 都没开。并发写场景（cron + 启动补跑 + API 查询）下极易抛
    SQLITE_BUSY 中断台账/状态库写入，污染交易关键路径。

    本测试钉死三件套基线：
      ① PRAGMA journal_mode=WAL（持久化到 db 文件头，独立连接可读到 wal）；
      ② PRAGMA busy_timeout=30000（连接级配置，独立连接查 PRAGMA busy_timeout）；
      ③ sqlite3.connect timeout=30（无法直接 SQL 查询——通过并发场景不抛 BUSY 间接验证）。

    并发场景：10 线程 Barrier 同时开始覆盖同一 (job, date)——无 WAL/timeout 时最易
    触发 SQLITE_BUSY（rollback journal 模式 + 5s 默认 timeout 在锁竞争下秒抛）；
    WAL + timeout=30 + busy_timeout=30000 让并发写排队串行化，不抛 BUSY。
"""
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from trading import job_ledger, state_store


def _journal_mode_persisted(db_path: str) -> str:
    """读 SQLite 持久化的 journal_mode（独立连接，返 delete/truncate/wal 等）。

    WAL 是持久化属性（写入 db 文件头），即使开了 WAL 的连接全部关闭，新开的独立连接
    仍能读到 wal——故用独立连接验证即可证 _connect 落盘 PRAGMA 成功。
    """
    con = sqlite3.connect(db_path)
    try:
        row = con.execute("PRAGMA journal_mode").fetchone()
        return row[0] if row else None
    finally:
        con.close()


# ============================================================================
# ① WAL 模式基线（PRAGMA journal_mode 持久化 = wal）
# ============================================================================

def test_job_ledger_wal_mode(tmp_path):
    """job_ledger._connect 必须开 WAL（DG-G6 基线：对齐 tasks_db/store 正范式）。

    FAIL 场景（当前代码）：job_ledger._connect 用裸 sqlite3.connect(db)，无 PRAGMA
    journal_mode=WAL → 持久化模式 = delete（rollback journal 默认）→ assert 失败。
    """
    db = str(tmp_path / "job_run.db")
    job_ledger.init_db(db)
    mode = _journal_mode_persisted(db)
    assert mode == "wal", f"job_ledger DB journal_mode 应 wal，实得 {mode}"


def test_state_store_wal_mode(tmp_path):
    """state_store._connect 必须开 WAL（基线钉死，防回归）。

    注：state_store._connect 当前已开 WAL（G5 前），本测试钉死基线——任何「去 WAL」
    的改动（如误删 PRAGMA）都会立即被此测试抓住。
    """
    db = str(tmp_path / "trading_state.db")
    state_store.init_store(db)
    mode = _journal_mode_persisted(db)
    assert mode == "wal", f"state_store DB journal_mode 应 wal，实得 {mode}"


# ============================================================================
# ② busy_timeout 基线（连接级 PRAGMA，_connect 内必须显式开）
# ============================================================================
# Why 不用独立 sqlite3.connect 查：busy_timeout 是 per-connection 设置（非持久化），
# 独立连接查到的是默认 0，必须经模块 _connect 路径开连接才能验证配置生效。


def test_job_ledger_connect_busy_timeout(tmp_path):
    """job_ledger._connect 开的连接 PRAGMA busy_timeout=30000（30s 兜底）。

    FAIL 场景（当前代码）：_connect 无 PRAGMA busy_timeout → 默认 0 → assert 失败。
    """
    db = str(tmp_path / "job_run.db")
    con = job_ledger._connect(db)
    try:
        row = con.execute("PRAGMA busy_timeout").fetchone()
        bt = row[0] if row else 0
    finally:
        con.close()
    assert bt == 30000, f"job_ledger busy_timeout 应 30000ms，实得 {bt}"


def test_state_store_connect_busy_timeout(tmp_path):
    """state_store._connect 开的连接 PRAGMA busy_timeout=30000（基线钉死）。"""
    db = str(tmp_path / "trading_state.db")
    with state_store._connect(db) as con:
        row = con.execute("PRAGMA busy_timeout").fetchone()
        bt = row[0] if row else 0
    assert bt == 30000, f"state_store busy_timeout 应 30000ms，实得 {bt}"


# ============================================================================
# ③ 并发写不抛 BUSY（WAL + timeout + busy_timeout 三件套综合验收）
# ============================================================================

def test_job_ledger_concurrent_writes_no_busy(tmp_path):
    """10 线程 Barrier 同时并发 begin_run/finish_run 同一 (job, date)——无 BUSY 抛错。

    物理意图：cron + 启动补跑 + API 查询可能并发触发 job_ledger 写。无 WAL + 默认 5s
    timeout 时，rollback journal 模式的 EXCLUSIVE 锁竞争极易抛 SQLITE_BUSY。WAL 模式
    + timeout=30 + busy_timeout=30000 让并发写排队串行化（不并发执行，但也不 BUSY 中断）。

    幂等语义（begin_run INSERT OR REPLACE 覆盖为 running）：10 线程同时覆盖同一行，
    顺序无关——最终状态取决于最后一个 finish_run，但所有写操作必须全部成功不抛 BUSY。

    FAIL 场景（当前代码）：无 WAL/timeout（默认 5s）→ 10 线程 Barrier 同时写同一行，
    EXCLUSIVE 锁竞争 → 部分线程 SQLITE_BUSY 抛错（偶发，慢盘/高并发更稳定触发）。
    """
    db = str(tmp_path / "job_run.db")
    job_ledger.init_db(db)

    errors: list[Exception] = []
    barrier = threading.Barrier(10)  # 10 线程同时就绪后一起放行，最大化锁竞争窗口

    def worker(i: int):
        try:
            barrier.wait()  # 全员就绪后同时触发，最大化并发锁竞争
            # 同一 (job, date) 并发覆盖——无 WAL/timeout 时最易 BUSY
            job_ledger.begin_run("pipeline", "2026-08-13", f"t{i}", path=db)
            job_ledger.finish_run("pipeline", "2026-08-13", "done", f"m{i}", path=db)
        except Exception as e:
            errors.append(e)

    with ThreadPoolExecutor(max_workers=10) as ex:
        futures = [ex.submit(worker, i) for i in range(10)]
        for f in as_completed(futures):
            f.result()  # 主线程重新观测（让断言外的异常也可见）

    busy_errors = [
        e for e in errors
        if isinstance(e, sqlite3.OperationalError)
        and ("busy" in str(e).lower() or "locked" in str(e).lower())
    ]
    assert not busy_errors, (
        f"并发 begin_run/finish_run 抛 SQLITE_BUSY/LOCKED：{[str(e) for e in busy_errors]}"
    )
    # 全员完成后行仍在 + 状态 = done（最后一个 finish_run 落盘）
    assert job_ledger.latest_status("pipeline", "2026-08-13", path=db) == "done"
