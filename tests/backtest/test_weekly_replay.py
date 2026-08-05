# -*- coding: utf-8 -*-
"""周度「近期回测」自动提交单测（2026-08-03）。

闸逻辑三态：间隔内不提交 / 过期提交（全市场 × 冠军参数 × 近 3 个月）/
存在未终态任务不提交。全部用 tmp DB，不碰真实 data/replay_tasks.db。
"""
import json
import sqlite3
from datetime import datetime, timedelta

from backtest import tasks_db as replay_tasks_db
from backtest import weekly_replay


def _seed_task(db_path: str, created_at: str, status: str = "SUCCESS",
               report: dict | None = None) -> str:
    replay_tasks_db.init_db(db_path)
    task_id = replay_tasks_db.create_task({
        "strategy_name": "neckline", "start": "2026-04-01", "end": "2026-07-01",
        "universe": None, "cfg_override": {},
    }, path=db_path)
    con = sqlite3.connect(db_path)
    con.execute("UPDATE replay_tasks SET created_at=?, status=? WHERE task_id=?",
                (created_at, status, task_id))
    if report is not None:
        con.execute("UPDATE replay_tasks SET report_json=? WHERE task_id=?",
                    (json.dumps(report), task_id))
    con.commit()
    con.close()
    return task_id


def test_no_enqueue_when_recent_task_exists(tmp_path, monkeypatch):
    """最近任务 <7 天 → 不重复提交。"""
    db = str(tmp_path / "tasks.db")
    monkeypatch.setattr(weekly_replay, "_champion_cfg_override", lambda: {"min_rr": 2.0})
    _seed_task(db, (datetime(2026, 8, 2, 19, 0)).isoformat())

    tid = weekly_replay.maybe_enqueue_weekly_replay(
        now=datetime(2026, 8, 3, 12, 0), db_path=db)
    assert tid is None
    assert len(replay_tasks_db.list_tasks(path=db)) == 1


def test_enqueue_when_stale(tmp_path, monkeypatch):
    """最近任务 ≥7 天 → 提交新任务（全市场 × 冠军参数 × 近 3 个月窗口）。"""
    db = str(tmp_path / "tasks.db")
    monkeypatch.setattr(weekly_replay, "_champion_cfg_override",
                        lambda: {"min_rr": 2.0, "window": 80})
    _seed_task(db, (datetime(2026, 7, 16, 10, 0)).isoformat())

    tid = weekly_replay.maybe_enqueue_weekly_replay(
        now=datetime(2026, 8, 3, 12, 0), db_path=db)
    assert tid
    tasks = replay_tasks_db.list_tasks(path=db)
    assert len(tasks) == 2
    new = next(t for t in tasks if t["task_id"] == tid)
    assert new["status"] == "PENDING"
    assert new["universe"] is None          # 全市场
    assert new["start"] == "2026-05-05"     # 2026-08-03 往前 90 天
    assert new["end"] == "2026-08-03"
    assert new["cfg_override"] == {"min_rr": 2.0, "window": 80}


def test_no_enqueue_when_active_task_pending(tmp_path, monkeypatch):
    """存在 PENDING/RUNNING 未终态任务 → 不重复提交（防调度器堆积双跑）。"""
    db = str(tmp_path / "tasks.db")
    monkeypatch.setattr(weekly_replay, "_champion_cfg_override", lambda: {})
    _seed_task(db, (datetime(2026, 7, 1, 10, 0)).isoformat(), status="PENDING")

    tid = weekly_replay.maybe_enqueue_weekly_replay(
        now=datetime(2026, 8, 3, 12, 0), db_path=db)
    assert tid is None
    assert len(replay_tasks_db.list_tasks(path=db)) == 1


def test_champion_cfg_override_prefers_active_experiment(monkeypatch):
    """有 ACTIVE 实验 → 周度回测用 experiment 参数（单一真相源，不再读 legacy state）。"""
    from experiment.models import ActiveExperiment
    monkeypatch.setattr(
        weekly_replay, "resolve_active",
        lambda: [ActiveExperiment(
            experiment_id="neckline_disc_20260725_25c602", strategy_name="neckline",
            params={"min_rr": 1.7, "window": 80}, weight=1.0,
            activated_at="2026-07-27T07:20:02")])

    assert weekly_replay._champion_cfg_override() == {"min_rr": 1.7, "window": 80}


def test_champion_cfg_override_empty_when_no_active(monkeypatch):
    """无 ACTIVE 实验 → {}（B3 2026-08-05 起彻底切断 legacy JSON 回退，单一真相源）。"""
    monkeypatch.setattr(weekly_replay, "resolve_active", lambda: [])

    assert weekly_replay._champion_cfg_override() == {}
