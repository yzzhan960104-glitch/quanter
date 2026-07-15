# -*- coding: utf-8 -*-
"""training_loop 状态机单测。mock 回测 DB + analyzer + notifier，不碰真网络/真回测。"""
import json
from unittest.mock import patch

import pytest

from caisen import training_loop
from caisen.training_loop import TrainingLoopOrchestrator, LoopBusyError


class FakeNotifier:
    """记录推送/回显，便于断言 loop 在正确时机调了 notifier。"""
    def __init__(self):
        self.pushed = []   # [(loop_id, text)]
    def push(self, loop_id, text):
        self.pushed.append((loop_id, text))


@pytest.fixture
def orch(monkeypatch, tmp_path):
    """装配一个用 tmp DB + fake notifier 的编排器。"""
    db = str(tmp_path / "loops.db")
    monkeypatch.setattr(training_loop.training_loops_db, "_DEFAULT_DB_PATH", db)
    training_loop.training_loops_db.init_db()
    monkeypatch.setattr(training_loop.replay_tasks_db, "_DEFAULT_DB_PATH",
                        str(tmp_path / "replay.db"))
    training_loop.replay_tasks_db.init_db()
    notifier = FakeNotifier()
    o = TrainingLoopOrchestrator(notifier)
    return o, notifier


def test_start_runs_round_then_awaits_review(orch, monkeypatch):
    """核心动线：start → 提交回测 → 轮询到 SUCCESS → analyze → AWAITING_REVIEW + 推报告。

    用 _step_once 手动推进状态机（不起 daemon 线程），可控可测。
    """
    o, notifier = orch
    loop_id = o.start({"start": "2020-01-01", "end": "2024-12-31", "universe": None,
                       "base_cfg": {"min_rr_ratio": 1.5}, "max_rounds": 3})
    # mock：提交回测后立刻把它标 SUCCESS + 写 report
    def fake_get_task(task_id, path=None):
        return {"task_id": task_id, "status": "SUCCESS",
                "report": {"n_hits": 10, "win_rate": 0.6, "avg_rr": 1.8,
                           "max_drawdown": -0.1, "annualized_return": 0.2,
                           "pattern_dist": {}, "trades": []}}
    monkeypatch.setattr(training_loop.replay_tasks_db, "get_task", fake_get_task)
    monkeypatch.setattr(training_loop.training_analyzer, "analyze_round",
                        lambda r, c, h: "## 报告：表现尚可")

    o._step_once(loop_id)   # RUNNING：提交回测 + 轮询到 SUCCESS → ANALYZING → AWAITING_REVIEW

    loop = training_loop.training_loops_db.get_loop(loop_id)
    assert loop["status"] == "AWAITING_REVIEW"
    assert loop["current_round"] == 1
    assert len(loop["history"]) == 1              # 第1轮统计已入 history
    assert notifier.pushed                         # 报告已推
    assert "报告" in notifier.pushed[-1][1]


def test_start_rejects_second_active_loop(orch):
    """concurrency=1 守卫：已有活跃 loop 再 start → LoopBusyError。"""
    o, _ = orch
    o.start({"start": "2020-01-01", "end": "2024-12-31", "base_cfg": {}, "max_rounds": 3})
    # 手动把第一个标活跃（start 落 IDLE，不在 list_active_loops 里）
    from caisen import training_loops_db
    lid = training_loops_db.list_loops()[0]["loop_id"]
    training_loops_db.update_loop(lid, status="RUNNING")
    with pytest.raises(LoopBusyError):
        o.start({"start": "2020-01-01", "end": "2024-12-31", "base_cfg": {}, "max_rounds": 3})


def test_confirm_rerun_accumulates_cfg(orch, monkeypatch):
    """CONFIRMING：parse→回显→「确认」→ 下一轮 cfg 累积 + 状态回 RUNNING。

    时序：主线程 _step_once 进 _handle_awaiting_review 阻塞等 event；子线程 sleep(0.2)
    后 submit_review 喂"重跑意图"唤醒第一次（进 _confirm 回显），_confirm 再等"确认"，
    子线程再 submit_review("确认") 唤醒第二次 → _apply_confirmed → RUNNING。
    验证 _confirm 的两段 wait 都能被 submit_review 正确唤醒（不靠加 sleep 硬等）。
    """
    o, notifier = orch
    loop_id = o.start({"start": "2020-01-01", "end": "2024-12-31", "base_cfg": {"min_rr_ratio": 1.5},
                       "max_rounds": 3})
    # 直接置 AWAITING_REVIEW 模拟已到人审关卡（跳过回测链路，聚焦 CONFIRMING）
    from caisen import training_loops_db
    training_loops_db.update_loop(loop_id, status="AWAITING_REVIEW", current_round=1)
    monkeypatch.setattr(training_loop.training_analyzer, "parse_review",
                        lambda t, c: {"cfg_override": {"max_holding_bars": 20}, "action": "rerun"})

    # 起一个线程模拟你两次回复：第一次触发 _confirm（parse 后回显），第二次「确认」落库。
    # _POLL_INTERVAL 默认 3s，测试里把它压到 0.05 让 wait 超时快、唤醒响应快（不硬等 3s）。
    monkeypatch.setattr(training_loop, "_POLL_INTERVAL", 0.05)
    import threading
    import time as _t
    replies = ["min_rr 改2.0 重跑", "确认"]
    def reply_later():
        for r in replies:
            _t.sleep(0.08)   # 等 daemon 进入 wait 阻塞
            o.submit_review(loop_id, r)
    threading.Thread(target=reply_later).start()
    o._step_once(loop_id)   # AWAITING_REVIEW → CONFIRMING → 等「确认」 → RUNNING

    loop = training_loops_db.get_loop(loop_id)
    assert loop["status"] == "RUNNING"
    assert loop["current_round"] == 1   # RUNNING 等 _handle_running 才 +1
    assert loop["current_cfg"] == {"min_rr_ratio": 1.5, "max_holding_bars": 20}  # 累积
