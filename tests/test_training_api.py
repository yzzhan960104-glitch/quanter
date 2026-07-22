# -*- coding: utf-8 -*-
"""training API 端点集成测试（Spec 3 Task 6）。

策略：注入 fake orchestrator 到 app.state（不依赖真状态机/钉钉/DB），断言：
- start/stop 走 orchestrator（start 传参、stop 唤醒 event 必须经编排器）。
- get/list 直调 training_loops_db（纯读，monkeypatch db 模块函数）。
- start 的 LoopBusyError → HTTP 422（业务规则冲突语义）。
"""
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.api.v1 import training as training_api


def _app_with_fake_orch(orch):
    """构造最小 FastAPI app + 注入 fake orchestrator + 挂 training router。

    生产中 app.state.training_orchestrator 由 main.py lifespan 装配（Task7）；
    测试直接赋值即可，TestClient 不触发 lifespan 也能读 app.state。
    """
    app = FastAPI()
    app.state.training_orchestrator = orch
    app.include_router(training_api.router, prefix="/api/v1")
    return TestClient(app)


def test_start_returns_loop_id():
    """start → orchestrator.start 被调，返 200 + {loop_id}。"""
    orch = MagicMock()
    orch.start.return_value = "loop-xyz"
    client = _app_with_fake_orch(orch)
    r = client.post("/api/v1/training/start", json={
        "start": "2020-01-01", "end": "2024-12-31",
        "base_cfg_override": {}, "max_rounds": 5})
    assert r.status_code == 200
    assert r.json()["loop_id"] == "loop-xyz"
    orch.start.assert_called_once()
    # 传给 orchestrator.start 的 req dict 字段对齐（base_cfg_override → base_cfg 重命名）
    req = orch.start.call_args.args[0]
    assert req["start"] == "2020-01-01" and req["end"] == "2024-12-31"
    assert req["max_rounds"] == 5 and req["base_cfg"] == {}


def test_start_rejects_when_busy():
    """已有活跃 loop → orchestrator.start 抛 LoopBusyError → HTTP 422。"""
    from backtest.optimize.training_loop import LoopBusyError
    orch = MagicMock()
    orch.start.side_effect = LoopBusyError("busy")
    client = _app_with_fake_orch(orch)
    r = client.post("/api/v1/training/start", json={
        "start": "2020-01-01", "end": "2024-12-31",
        "base_cfg_override": {}, "max_rounds": 5})
    assert r.status_code == 422


def test_get_loop_state(monkeypatch):
    """get 直调 training_loops_db.get_loop（不经 orchestrator）→ 200 + 状态。

    response_model=TrainingLoopState 锁定契约后：
    - universe 字段仍可见（schema 已声明，合法下发）。
    - 未声明字段（如此处的 rogue_col）被 Pydantic 过滤掉，验证 schema 真正生效。
    """
    monkeypatch.setattr(
        training_api.training_loops_db, "get_loop",
        lambda lid: {"loop_id": lid, "status": "AWAITING_REVIEW",
                     "current_round": 1, "max_rounds": 20, "history": [], "current_cfg": {},
                     "universe": ["000001.SZ", "600000.SH"], "created_at": "2026-07-15 10:00:00",
                     "rogue_col": "SHOULD_BE_FILTERED"})  # 模拟未来加列泄漏
    client = _app_with_fake_orch(MagicMock())
    r = client.get("/api/v1/training/l1")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "AWAITING_REVIEW"
    # universe 是 schema 声明的合法字段 → response_model 保留
    assert body["universe"] == ["000001.SZ", "600000.SH"]
    # rogue_col 未声明 → response_model 过滤，验证契约锁定（防未来加列自动泄漏）
    assert "rogue_col" not in body


def test_get_loop_not_found(monkeypatch):
    """get 不存在 → 404（get_loop 返 None）。"""
    monkeypatch.setattr(training_api.training_loops_db, "get_loop", lambda lid: None)
    client = _app_with_fake_orch(MagicMock())
    r = client.get("/api/v1/training/nope")
    assert r.status_code == 404


def test_stop_loop():
    """stop → orchestrator.stop(loop_id) 被调，返 200 + {status: STOPPED}。"""
    orch = MagicMock()
    client = _app_with_fake_orch(orch)
    r = client.post("/api/v1/training/l1/stop")
    assert r.status_code == 200
    assert r.json()["status"] == "STOPPED"
    orch.stop.assert_called_once_with("l1")


def test_list_loops(monkeypatch):
    """list 直调 training_loops_db.list_loops（不经 orchestrator）→ 200 + 数组。"""
    monkeypatch.setattr(
        training_api.training_loops_db, "list_loops",
        lambda limit=100: [{"loop_id": "l1", "status": "STOPPED",
                            "current_round": 1, "max_rounds": 20}])
    client = _app_with_fake_orch(MagicMock())
    r = client.get("/api/v1/training")
    assert r.status_code == 200
    assert len(r.json()) == 1


def test_start_503_when_orchestrator_missing():
    """orchestrator 未装配（app.state 无 training_orchestrator）→ 503。"""
    app = FastAPI()  # 故意不注入 orchestrator
    app.include_router(training_api.router, prefix="/api/v1")
    client = TestClient(app)
    r = client.post("/api/v1/training/start", json={
        "start": "2020-01-01", "end": "2024-12-31",
        "base_cfg_override": {}, "max_rounds": 5})
    assert r.status_code == 503
