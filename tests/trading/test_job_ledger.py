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


# ---- Task 8: snapshot_for_date 只读查询（GET /trading/jobs 数据源）----

def test_snapshot_returns_all_jobs_for_date(tmp_path):
    """同日多 job 全返 + 字段映射 job_name→name + 按 job_name 升序。"""
    db = str(tmp_path / "job_run.db")
    job_ledger.begin_run("pipeline", "2026-08-02", "t1", path=db)
    job_ledger.finish_run("pipeline", "2026-08-02", "done", "ok", path=db)
    job_ledger.begin_run("pre_open", "2026-08-02", "t2", path=db)
    job_ledger.finish_run("pre_open", "2026-08-02", "skipped", "gate3 reject", path=db)
    snap = job_ledger.snapshot_for_date("2026-08-02", path=db)
    assert [j["name"] for j in snap] == ["pipeline", "pre_open"]
    assert snap[0]["status"] == "done" and snap[0]["message"] == "ok"
    assert snap[1]["status"] == "skipped" and snap[1]["started_at"] == "t2"


def test_snapshot_empty_when_no_rows(tmp_path):
    """init_db 后查空表，无任何记录时返 []（前端驾驶舱空态）。"""
    db = str(tmp_path / "job_run.db")
    job_ledger.init_db(db)
    assert job_ledger.snapshot_for_date("2026-08-02", path=db) == []


def test_snapshot_isolates_by_date(tmp_path):
    """按业务日隔离——两日各一条，查 2026-08-02 只返 1 条。"""
    db = str(tmp_path / "job_run.db")
    job_ledger.begin_run("pipeline", "2026-08-01", "t1", path=db)
    job_ledger.finish_run("pipeline", "2026-08-01", "done", path=db)
    job_ledger.begin_run("pipeline", "2026-08-02", "t2", path=db)
    job_ledger.finish_run("pipeline", "2026-08-02", "done", path=db)
    snap = job_ledger.snapshot_for_date("2026-08-02", path=db)
    assert len(snap) == 1
    assert snap[0]["name"] == "pipeline"
    assert snap[0]["started_at"] == "t2"


def test_snapshot_coerces_null_message_to_empty(tmp_path):
    """message 列为 NULL（绕过 finish_run 直接 SQL 写入）→ snapshot 强制 ''（防 None 透传前端）。"""
    db = str(tmp_path / "job_run.db")
    job_ledger.init_db(db)
    import sqlite3
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO job_run (job_name, business_date, status, started_at, finished_at, message) "
        "VALUES (?, ?, ?, ?, NULL, NULL)",
        ("pipeline", "2026-08-02", "done", "t1"),
    )
    conn.commit()
    conn.close()
    snap = job_ledger.snapshot_for_date("2026-08-02", path=db)
    assert len(snap) == 1
    assert snap[0]["message"] == ""   # NULL → "" 强制，非 None
    assert snap[0]["finished_at"] is None or snap[0]["finished_at"] == ""  # NULL 透传 finished_at（仅 message 走守卫）
