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