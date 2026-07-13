# -*- coding: utf-8 -*-
"""replay_tasks_db 单测：SQLite 任务表 CRUD + 状态机 + 重启恢复（Spec 1 · Task 1）。

物理意图：异步回测任务全生命周期持久化的访问层测试。每个用例独立 SQLite 文件
（tmp_path 隔离），monkeypatch 模块级 _DEFAULT_DB_PATH 常量——实现层 path=None 内部
fallback 读全局，故 monkeypatch 生效（避免「默认参数在 def 时绑定」导致隔离失效）。
"""
import pytest

from caisen import replay_tasks_db


@pytest.fixture
def db(tmp_path, monkeypatch):
    """每个用例独立 SQLite 文件（隔离），monkeypatch 路径常量 + 建表。"""
    path = str(tmp_path / "t.db")
    monkeypatch.setattr(replay_tasks_db, "_DEFAULT_DB_PATH", path)
    replay_tasks_db.init_db()
    return path


def _req(**over):
    """构造 create_task 入参（start/end/universe/cfg_override），默认 universe=None 全市场。"""
    base = {"start": "2024-01-01", "end": "2024-06-01", "universe": None, "cfg_override": {}}
    base.update(over)
    return base


def test_create_and_get(db):
    """create_task 生成 task_id + 写 PENDING 行 + progress=0 + universe_n=-1（全市场）。"""
    task_id = replay_tasks_db.create_task(_req(cfg_override={"min_rr_ratio": 1.5}))
    assert isinstance(task_id, str) and len(task_id) > 0
    got = replay_tasks_db.get_task(task_id)
    assert got["task_id"] == task_id
    assert got["status"] == "PENDING"
    assert got["progress"] == 0
    assert got["universe_n"] == -1            # None → -1（全市场）


def test_create_stores_universe_list(db):
    """universe_json 修正项：显式标的列表完整存（worker 还原装配 price_data 必需）。"""
    tid = replay_tasks_db.create_task(_req(universe=["000001.SZ", "600000.SH"]))
    got = replay_tasks_db.get_task(tid)
    assert got["universe"] == ["000001.SZ", "600000.SH"]
    assert got["universe_n"] == 2


def test_create_universe_none_is_all_market(db):
    """universe=None → 存 null → 读回 None（全市场语义）；universe_n=-1。"""
    got = replay_tasks_db.get_task(replay_tasks_db.create_task(_req()))
    assert got["universe"] is None
    assert got["universe_n"] == -1


def test_cfg_override_roundtrip(db):
    """cfg_json 反序列化无损（中文/嵌套结构都应保留）。"""
    got = replay_tasks_db.get_task(
        replay_tasks_db.create_task(
            _req(cfg_override={"min_rr_ratio": 1.5, "标签": "中文"})))
    assert got["cfg_override"] == {"min_rr_ratio": 1.5, "标签": "中文"}


def test_list_tasks_desc_by_created(db):
    """list_tasks 按 created_at 降序（最新在前）。"""
    id1 = replay_tasks_db.create_task(_req())
    id2 = replay_tasks_db.create_task(_req())
    rows = replay_tasks_db.list_tasks()
    assert [r["task_id"] for r in rows] == [id2, id1]


def test_list_tasks_status_filter(db):
    """list_tasks(status=) 精确过滤；mark_failed 后不再属 PENDING。"""
    tid = replay_tasks_db.create_task(_req())
    replay_tasks_db.mark_failed(tid, "boom")
    assert replay_tasks_db.list_tasks(status="PENDING") == []
    assert len(replay_tasks_db.list_tasks(status="FAILED")) == 1


def test_claim_next_pending_atomic(db):
    """claim_next_pending：FIFO 领取最老 PENDING 并即时标 RUNNING（防并发双领）。"""
    id1 = replay_tasks_db.create_task(_req())
    id2 = replay_tasks_db.create_task(_req())
    claimed = replay_tasks_db.claim_next_pending()
    assert claimed["task_id"] == id1          # FIFO 最老
    assert claimed["status"] == "RUNNING"     # 领取即标 RUNNING
    assert replay_tasks_db.claim_next_pending()["task_id"] == id2
    assert replay_tasks_db.claim_next_pending() is None   # 队列空


def test_mark_success_embeds_report(db):
    """mark_success：status=SUCCESS + report_json 反序列化 + progress=100 + finished_at。"""
    tid = replay_tasks_db.create_task(_req())
    replay_tasks_db.mark_success(tid, '{"n_hits": 42}')
    got = replay_tasks_db.get_task(tid)
    assert got["status"] == "SUCCESS"
    assert got["report"] == {"n_hits": 42}    # report_json 反序列化字段
    assert got["progress"] == 100
    assert got["finished_at"] is not None


def test_update_progress(db):
    tid = replay_tasks_db.create_task(_req())
    replay_tasks_db.update_progress(tid, 37)
    assert replay_tasks_db.get_task(tid)["progress"] == 37


def test_update_heartbeat(db):
    """update_heartbeat：调度器周期更新，超时判定 worker 崩溃的依据。"""
    tid = replay_tasks_db.create_task(_req())
    replay_tasks_db.update_heartbeat(tid)
    assert replay_tasks_db.get_task(tid)["last_heartbeat"] is not None


def test_mark_cancelled(db):
    tid = replay_tasks_db.create_task(_req())
    replay_tasks_db.mark_cancelled(tid)
    got = replay_tasks_db.get_task(tid)
    assert got["status"] == "CANCELLED"
    assert got["finished_at"] is not None


def test_reset_running_to_failed(db):
    """重启恢复：残留 RUNNING 标 FAILED + 错误信息含「重启/中断」（不自动重跑）。"""
    tid = replay_tasks_db.create_task(_req())
    replay_tasks_db.claim_next_pending()       # → RUNNING
    n = replay_tasks_db.reset_running_to_failed()
    assert n == 1
    got = replay_tasks_db.get_task(tid)
    assert got["status"] == "FAILED"
    assert "重启" in got["error"] or "中断" in got["error"]


def test_delete_task(db):
    """delete_task：删存在行返 True；删不存在的返 False。"""
    tid = replay_tasks_db.create_task(_req())
    assert replay_tasks_db.delete_task(tid) is True
    assert replay_tasks_db.get_task(tid) is None
    assert replay_tasks_db.delete_task("nope") is False


def test_list_success_runs(db):
    """list_success_runs 只返 SUCCESS（供 /replay/runs，FAILED/CANCELLED 不进列表）。"""
    t1 = replay_tasks_db.create_task(_req())
    t2 = replay_tasks_db.create_task(_req())
    replay_tasks_db.mark_success(t1, '{"n_hits": 1}')
    replay_tasks_db.mark_failed(t2, "boom")
    rows = replay_tasks_db.list_success_runs()
    assert [r["task_id"] for r in rows] == [t1]
