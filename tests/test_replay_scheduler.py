# -*- coding: utf-8 -*-
"""replay_scheduler 单测：领取/派发/cancel/heartbeat 超时/重启恢复（Spec 1 · Task 4）。

物理意图：调度器是 uvicorn 进程内的 daemon 线程，串行调度（concurrency=1）。用假 pool
（_FakePool 直接记录 submit，不真起子进程）+ 注入 no-op run_replay_worker，隔离进程边界，
专注调度器状态机逻辑（claim→submit→abort_flag 注册、cancel、sweep 超时、重启恢复）。
"""
from datetime import datetime, timedelta

import pytest

from caisen import replay_tasks_db, replay_scheduler


class _FakePool:
    """假 ProcessPoolExecutor：submit 只记录 callable + 参数，不真跑（测试控制）。"""

    def __init__(self):
        self.submitted = []

    def submit(self, fn, *a, **kw):
        self.submitted.append((fn, a, kw))


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = str(tmp_path / "t.db")
    monkeypatch.setattr(replay_tasks_db, "_DEFAULT_DB_PATH", path)
    replay_tasks_db.init_db()
    return path


def _new_sched(db, pool=None, **kw):
    """构造 scheduler（注入 no-op run_replay_worker，避免真起子进程）。"""
    return replay_scheduler.ReplayScheduler(
        pool or _FakePool(), {}, db,
        run_replay_worker=kw.pop("run_replay_worker", lambda *a, **k: None),
        **kw,
    )


def _mk_task(db, **over):
    return replay_tasks_db.create_task({
        "start": "s", "end": "e", "universe": None, "cfg_override": {}, **over,
    })


def test_reset_on_startup_marks_stale_running_failed(db):
    """启动恢复：残留 RUNNING 标 FAILED（spec §3.3，不自动重跑）。"""
    tid = _mk_task(db)
    replay_tasks_db.claim_next_pending()         # → RUNNING
    sched = _new_sched(db)
    sched._reset_on_startup()
    assert replay_tasks_db.get_task(tid)["status"] == "FAILED"


def test_poll_dispatches_pending(db):
    """有 PENDING → claim（标 RUNNING）+ submit worker + 注册 abort_flag。"""
    tid = _mk_task(db)
    pool = _FakePool()
    sched = _new_sched(db, pool=pool)
    sched._poll_once()
    sched.stop()
    assert len(pool.submitted) == 1              # submit 了一次 worker
    assert tid in sched.abort_flags              # abort_flag 已注册
    assert replay_tasks_db.get_task(tid)["status"] == "RUNNING"


def test_poll_no_pending_is_noop(db):
    """无 PENDING → _poll_once 不 submit（空转）。"""
    pool = _FakePool()
    sched = _new_sched(db, pool=pool)
    sched._poll_once()
    sched.stop()
    assert pool.submitted == []


def test_request_cancel_sets_abort_flag(db):
    """request_cancel 置 abort_flag（worker 循环顶命中即 CANCELLED）。"""
    tid = _mk_task(db)
    sched = _new_sched(db)
    sched._poll_once()                           # 注册 abort_flag
    sched.request_cancel(tid)
    sched.stop()
    assert sched.abort_flags[tid].is_set()


def test_heartbeat_timeout_marks_failed(db):
    """RUNNING 任务 heartbeat 超时（>300s）→ sweep 标 FAILED（worker 崩溃，不重跑）。"""
    tid = _mk_task(db)
    replay_tasks_db.claim_next_pending()         # → RUNNING，last_heartbeat=now
    # 注入 fake clock = now + 400s（超 _HEARTBEAT_TIMEOUT=300）
    fake_now = datetime.now() + timedelta(seconds=400)
    sched = _new_sched(db, clock=lambda: fake_now)
    sched._sweep_stale()
    got = replay_tasks_db.get_task(tid)
    assert got["status"] == "FAILED"
    assert "超时" in got["error"] or "heartbeat" in got["error"].lower()


def test_sweep_skips_fresh_heartbeat(db):
    """刚 RUNNING（heartbeat 新鲜）→ sweep 不误杀（守护：正常任务不被超时逻辑误标 FAILED）。"""
    tid = _mk_task(db)
    replay_tasks_db.claim_next_pending()         # last_heartbeat=now
    sched = _new_sched(db, clock=datetime.now)   # 真实 now（age≈0）
    sched._sweep_stale()
    assert replay_tasks_db.get_task(tid)["status"] == "RUNNING"
