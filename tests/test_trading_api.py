# -*- coding: utf-8 -*-
"""交易路由端到端冒烟（FastAPI TestClient）。

验证 HTTP 码映射 + dry_run 字段透传 + 挡板命中→409 + 模拟→200(DRY_RUN)。
"""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from server.main import app
    return TestClient(app)


def test_status_endpoint(client):
    """GET /trading/status 始终可访问（无网关时 unavailable）。"""
    r = client.get("/api/v1/trading/status")
    assert r.status_code == 200
    assert "mode" in r.json()


def test_submit_order_dry_run(client, monkeypatch):
    """dry_run=true → 200 + state=DRY_RUN（不真下单）。"""
    from server.services import trading_service

    class _FakeGW:
        _connected = True
        _lock_down = False
        @property
        def is_locked(self):
            return False
    monkeypatch.setattr(trading_service, "get_gateway", lambda: _FakeGW())
    monkeypatch.setattr(trading_service, "record_live_trade", lambda *a, **kw: None)

    r = client.post("/api/v1/trading/submit_order", json={
        "symbol": "510300.SH", "qty": 100, "side": "buy", "price": 5.0,
        "dry_run": True, "confirm": True,
    })
    assert r.status_code == 200
    assert r.json()["state"] == "DRY_RUN"


def test_submit_order_no_confirm_returns_409(client, monkeypatch):
    """缺 confirm（且 allow_live=True）→ 挡板 confirm 关命中 → 409。"""
    from server.services import trading_service

    class _FakeGW:
        _connected = True
        _lock_down = False
        @property
        def is_locked(self):
            return False
    monkeypatch.setattr(trading_service, "get_gateway", lambda: _FakeGW())
    monkeypatch.setattr(trading_service, "record_live_trade", lambda *a, **kw: None)
    monkeypatch.setattr(trading_service, "_allow_live", lambda: True, raising=False)

    r = client.post("/api/v1/trading/submit_order", json={
        "symbol": "510300.SH", "qty": 100, "side": "buy", "price": 5.0,
        "dry_run": False, "confirm": False,
    })
    assert r.status_code == 409


def test_submit_order_unavailable(client, monkeypatch):
    """无网关 → submit_order raise RuntimeError → 路由当前实现未捕获会 500。

    本用例锁定：无网关时 submit_order 不静默成功（至少非 200）。
    """
    from server.services import trading_service
    monkeypatch.setattr(trading_service, "get_gateway", lambda: None)
    r = client.post("/api/v1/trading/submit_order", json={
        "symbol": "510300.SH", "qty": 100, "side": "buy",
        "dry_run": True, "confirm": True,
    })
    assert r.status_code in (409, 500, 503)


def test_orders_and_asset_empty(client, monkeypatch):
    """无网关 → orders 返空 list，asset 返空 dict（均 200，非 503）。"""
    from server.services import trading_service
    monkeypatch.setattr(trading_service, "get_gateway", lambda: None)
    ro = client.get("/api/v1/trading/orders")
    ra = client.get("/api/v1/trading/asset")
    assert ro.status_code == 200 and ra.status_code == 200
    assert ro.json()["orders"] == []
    assert ra.json()["asset"] == {}


def test_connect_unavailable_503(client, monkeypatch):
    """无网关 → /connect 返 503。"""
    from server.services import trading_service
    monkeypatch.setattr(trading_service, "get_gateway", lambda: None)
    r = client.post("/api/v1/trading/connect")
    assert r.status_code == 503
