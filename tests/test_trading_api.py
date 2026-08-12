# -*- coding: utf-8 -*-
"""交易路由端到端冒烟（FastAPI TestClient）。

验证 HTTP 码映射 + dry_run 字段透传 + 挡板命中→409 + 模拟→200(DRY_RUN)。
"""
import pytest
from fastapi.testclient import TestClient

# Task 10：jobs 端点用例需要直接 patch app.state（TestClient 不跑 lifespan，
# state 上没有 trading_engine/catchup_task；用 monkeypatch 注入鸭子类型 _FakeTask）。
from presentation.server.main import app


@pytest.fixture
def client():
    return TestClient(app)


def test_status_endpoint(client):
    """GET /trading/status 始终可访问（无网关时 unavailable）。"""
    r = client.get("/api/v1/trading/status")
    assert r.status_code == 200
    assert "mode" in r.json()


def test_submit_order_dry_run(client, monkeypatch):
    """dry_run=true → 200 + state=DRY_RUN（不真下单）。"""
    from trading import gateway_service as trading_service

    class _FakeGW:
        _connected = True
        _lock_down = False
        @property
        def is_locked(self):
            return False
    monkeypatch.setattr(trading_service, "get_gateway", lambda: _FakeGW())

    r = client.post("/api/v1/trading/submit_order", json={
        "symbol": "510300.SH", "qty": 100, "side": "buy", "price": 5.0,
        "dry_run": True, "confirm": True,
    })
    assert r.status_code == 200
    assert r.json()["state"] == "DRY_RUN"


def test_submit_order_unavailable(client, monkeypatch):
    """无网关 → submit_order raise RuntimeError → 路由当前实现未捕获会 500。

    本用例锁定：无网关时 submit_order 不静默成功（至少非 200）。
    """
    from trading import gateway_service as trading_service
    monkeypatch.setattr(trading_service, "get_gateway", lambda: None)
    r = client.post("/api/v1/trading/submit_order", json={
        "symbol": "510300.SH", "qty": 100, "side": "buy",
        "dry_run": True, "confirm": True,
    })
    assert r.status_code in (409, 500, 503)


def test_orders_and_asset_empty(client, monkeypatch):
    """无网关 → orders 返空 list，asset 返空 dict（均 200，非 503）。"""
    from trading import gateway_service as trading_service
    monkeypatch.setattr(trading_service, "get_gateway", lambda: None)
    ro = client.get("/api/v1/trading/orders")
    ra = client.get("/api/v1/trading/asset")
    assert ro.status_code == 200 and ra.status_code == 200
    assert ro.json()["orders"] == []
    assert ra.json()["asset"] == {}


def test_connect_unavailable_503(client, monkeypatch):
    """无网关 → /connect 返 503。"""
    from trading import gateway_service as trading_service
    monkeypatch.setattr(trading_service, "get_gateway", lambda: None)
    r = client.post("/api/v1/trading/connect")
    assert r.status_code == 503


# ============================================================================
# Phase 2 Task 10：作业驾驶舱 GET /jobs 端点（TDD）
# ============================================================================
# _FakeTask 鸭子类型：模拟 asyncio.Task 的 done/exception/result 三态，
# 与 tests/test_trading_service.py 中 catchup 用例同构（service 层 _resolve_catchup_state
# 只 probe 这三个方法，不依赖真实 Task 继承链）。
class _FakeTask:
    def __init__(self, done, exc=None, result=None):
        self._done, self._exc, self._res = done, exc, result
    def done(self): return self._done
    def exception(self): return self._exc
    def result(self): return self._res


def test_jobs_default_date_today(client, monkeypatch):
    """GET /jobs 无 date → date 缺省 = clock.today()；无 catchup_task → not_started。"""
    from trading import clock
    # 清掉可能残留的 catchup_task（TestClient 不跑 lifespan，state 本来就没它）
    monkeypatch.setattr(app.state, "catchup_task", None, raising=False)
    r = client.get("/api/v1/trading/jobs")
    assert r.status_code == 200
    body = r.json()
    assert body["date"] == clock.today()
    assert body["catchup"]["state"] == "not_started"
    assert isinstance(body["jobs"], list)


def test_jobs_catchup_running(client, monkeypatch):
    """注入未完成 catchup_task → state=running。"""
    monkeypatch.setattr(app.state, "catchup_task", _FakeTask(done=False), raising=False)
    r = client.get("/api/v1/trading/jobs")
    assert r.json()["catchup"]["state"] == "running"


def test_jobs_catchup_done(client, monkeypatch):
    """注入已完成 catchup_task → state=done + result 透传。"""
    res = {"pipeline": True, "brief": False, "pre_open": False, "pre_open_note": "", "error": None}
    monkeypatch.setattr(app.state, "catchup_task", _FakeTask(done=True, result=res), raising=False)
    body = client.get("/api/v1/trading/jobs").json()
    assert body["catchup"]["state"] == "done"
    assert body["catchup"]["result"] == res


def test_jobs_with_explicit_date(client, monkeypatch):
    """GET /jobs?date=2026-07-31 → date 原样回显。"""
    monkeypatch.setattr(app.state, "catchup_task", None, raising=False)
    r = client.get("/api/v1/trading/jobs?date=2026-07-31")
    assert r.json()["date"] == "2026-07-31"
