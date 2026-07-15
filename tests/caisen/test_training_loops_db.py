# -*- coding: utf-8 -*-
"""training_loops 表 CRUD + 重启恢复单测。

用 tmp_path 隔离 DB（monkeypatch _DEFAULT_DB_PATH），不污染生产 data/replay_tasks.db。
"""
import json
from pathlib import Path

from caisen import training_loops_db


def _use_tmp_db(monkeypatch, tmp_path):
    """把模块级默认 DB 路径指向 tmp，生产 DB 不受影响。"""
    db = str(tmp_path / "test_loops.db")
    monkeypatch.setattr(training_loops_db, "_DEFAULT_DB_PATH", db)
    # _resolve 优先参数 path；None 时才回退模块级常量，故 monkeypatch 模块常量即可
    return db


def test_create_and_get_loop(monkeypatch, tmp_path):
    _use_tmp_db(monkeypatch, tmp_path)
    training_loops_db.init_db()
    loop_id = training_loops_db.create_loop({
        "start": "2020-01-01", "end": "2024-12-31",
        "universe": ["000001.SZ"], "base_cfg": {"min_rr_ratio": 1.5},
        "max_rounds": 5,
    })
    loop = training_loops_db.get_loop(loop_id)
    assert loop is not None
    assert loop["status"] == "IDLE"
    assert loop["max_rounds"] == 5
    assert loop["current_round"] == 0
    assert loop["base_cfg"] == {"min_rr_ratio": 1.5}
    assert loop["current_cfg"] == {"min_rr_ratio": 1.5}   # 初始 current = base
    assert loop["history"] == []


def test_update_loop_and_append_history(monkeypatch, tmp_path):
    _use_tmp_db(monkeypatch, tmp_path)
    training_loops_db.init_db()
    loop_id = training_loops_db.create_loop(
        {"start": "2020-01-01", "end": "2024-12-31", "universe": None,
         "base_cfg": {}, "max_rounds": 3})
    # update_loop：改状态/轮次/当前 cfg/待审信息
    training_loops_db.update_loop(loop_id, status="RUNNING", current_round=1,
                                  current_cfg={"min_rr_ratio": 2.0})
    training_loops_db.append_history(loop_id, {"round": 1, "n_hits": 10, "win_rate": 0.6,
                                               "avg_rr": 1.8, "max_dd": -0.12,
                                               "annualized": 0.25})
    loop = training_loops_db.get_loop(loop_id)
    assert loop["status"] == "RUNNING"
    assert loop["current_round"] == 1
    assert loop["current_cfg"] == {"min_rr_ratio": 2.0}
    assert len(loop["history"]) == 1
    assert loop["history"][0]["round"] == 1


import pytest


# 4 个活跃态全列：concurrency=1 守卫核心，任一活跃态漏判会致守卫失效
# （误放行第二个 loop 或永远阻塞）。此前测试只覆盖 AWAITING_REVIEW 一个态
# （review finding I3），现参数化覆盖全部 4 个活跃态都进活跃列表。
_ACTIVE_STATUSES = ["RUNNING", "ANALYZING", "AWAITING_REVIEW", "CONFIRMING"]


@pytest.mark.parametrize("active_status", _ACTIVE_STATUSES)
def test_list_active_loops_concurrency_guard(monkeypatch, tmp_path, active_status):
    """活跃态守卫：每个活跃态 loop 都应进活跃列表；IDLE/STOPPED 不在列。

    每个参数化 case 各起独立 tmp DB（pytest fixture 隔离），造一个该活跃态 loop + 一个
    IDLE loop + 一个 STOPPED loop，断言 list_active_loops 只返回那一个活跃态 loop。
    """
    _use_tmp_db(monkeypatch, tmp_path)
    training_loops_db.init_db()
    active_loop = training_loops_db.create_loop(
        {"start": "2020-01-01", "end": "2024-12-31", "base_cfg": {}, "max_rounds": 3})
    idle_loop = training_loops_db.create_loop(
        {"start": "2020-01-01", "end": "2024-12-31", "base_cfg": {}, "max_rounds": 3})
    stopped_loop = training_loops_db.create_loop(
        {"start": "2020-01-01", "end": "2024-12-31", "base_cfg": {}, "max_rounds": 3})
    # 起步全 IDLE → 空列表（守卫放行）
    assert training_loops_db.list_active_loops() == []
    # 把 active_loop 推到目标活跃态；另两个分别停在 IDLE / 推到 STOPPED
    training_loops_db.update_loop(active_loop, status=active_status)
    training_loops_db.update_loop(stopped_loop, status="STOPPED")  # STOPPED 不是活跃态
    active = training_loops_db.list_active_loops()
    # 守卫核心断言：活跃列表有且仅有 active_loop，idle/stopped 都不在列
    assert len(active) == 1
    assert active[0]["loop_id"] == active_loop
    assert active[0]["status"] == active_status


def test_reset_interrupted(monkeypatch, tmp_path):
    """重启恢复：RUNNING/ANALYZING 残留 → STOPPED；AWAITING_REVIEW/CONFIRMING/STOPPED 不动。

    全局约束："AWAITING_REVIEW/CONFIRMING 不动"——这两个是人审/确认中态，跨重启应保留
    （已花掉的回测+解析算力不能丢）。此前测试只验了 AWAITING_REVIEW，CONFIRMING 态漏覆盖
    （review finding I2）。这里补一个 CONFIRMING loop，断言 reset 后它仍为 CONFIRMING。
    """
    _use_tmp_db(monkeypatch, tmp_path)
    training_loops_db.init_db()
    running = training_loops_db.create_loop(
        {"start": "2020-01-01", "end": "2024-12-31", "base_cfg": {}, "max_rounds": 3})
    analyzing = training_loops_db.create_loop(
        {"start": "2020-01-01", "end": "2024-12-31", "base_cfg": {}, "max_rounds": 3})
    awaiting = training_loops_db.create_loop(
        {"start": "2020-01-01", "end": "2024-12-31", "base_cfg": {}, "max_rounds": 3})
    confirming = training_loops_db.create_loop(
        {"start": "2020-01-01", "end": "2024-12-31", "base_cfg": {}, "max_rounds": 3})
    training_loops_db.update_loop(running, status="RUNNING")
    training_loops_db.update_loop(analyzing, status="ANALYZING")
    training_loops_db.update_loop(awaiting, status="AWAITING_REVIEW")
    training_loops_db.update_loop(confirming, status="CONFIRMING")
    n = training_loops_db.reset_interrupted()
    assert n == 2   # 仅 RUNNING/ANALYZING 被重置
    assert training_loops_db.get_loop(running)["status"] == "STOPPED"
    assert training_loops_db.get_loop(running)["error"] == "进程重启中断"
    assert training_loops_db.get_loop(analyzing)["status"] == "STOPPED"  # ANALYZING 也在重置集
    assert training_loops_db.get_loop(awaiting)["status"] == "AWAITING_REVIEW"  # 不动
    assert training_loops_db.get_loop(confirming)["status"] == "CONFIRMING"  # I2: CONFIRMING 不动
