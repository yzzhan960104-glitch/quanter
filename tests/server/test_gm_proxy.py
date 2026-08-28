# -*- coding: utf-8 -*-
"""W4-B：掘金 7002 只读观测代理（/api/v1/gm/*）契约测试。

焦点：① 只读面（六路由全 GET，无任何交易动作族）；② 上游非 200/断连 → 502
中文排障（不裸抛）；③ 7002 调用与 runtime 读取单源走 gm_ops_common（monkeypatch
单点即可隔离）；④ cookie 只读鉴权在挂载层（本测试直接调函数，鉴权面由
test_auth_fail_closed 族守卫）。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from ops import gm_ops_common as gc


@pytest.fixture()
def client(monkeypatch):
    """只挂 gm 路由的极简 app（鉴权/ lifespan 与 main 解耦，聚焦代理语义）。"""
    from fastapi import FastAPI
    from presentation.server.api.v1.gm import router
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    return TestClient(app)


def _fake_apis(monkeypatch, *, status=200, payload=None, cfg=None):
    monkeypatch.setattr(gc, "runtime_config",
                        lambda strategy_dir=None: cfg or {"token": "t", "account_id": "ACC"})
    calls = []

    def fake_get(path, token, timeout=3.0):
        calls.append(path)
        return status, payload

    monkeypatch.setattr(gc, "api_get", fake_get)
    return calls


def test_positions_proxies_and_wraps(client, monkeypatch):
    calls = _fake_apis(monkeypatch, payload={"data": [{"symbol": "SZSE.300433", "volume": 100}]})
    r = client.get("/api/v1/gm/positions")
    assert r.status_code == 200
    assert r.json()["data"][0]["symbol"] == "SZSE.300433"
    assert calls == ["/v3/account-trade/positions/ACC"], "account 占位必须被真实账户替换"


def test_upstream_down_returns_502_with_cn_hint(client, monkeypatch):
    _fake_apis(monkeypatch, status=0, payload=None)   # 0=连不上（api_get 契约）
    r = client.get("/api/v1/gm/asset")
    assert r.status_code == 502
    assert "7002" in r.json()["detail"]


def test_overview_aggregates_two_calls(client, monkeypatch):
    monkeypatch.setattr(gc, "runtime_config",
                        lambda strategy_dir=None: {"token": "t", "account_id": "ACC"})

    def fake_get(path, token, timeout=3.0):
        if path == "/v3/strategies":
            return 200, {"data": [{"id": "s1", "stage": "running"}]}
        if path == "/v3/account-statuses":
            return 200, {"data": [{"accountId": "ACC", "status": "OK"}]}
        raise AssertionError(path)

    monkeypatch.setattr(gc, "api_get", fake_get)
    r = client.get("/api/v1/gm/overview")
    assert r.status_code == 200
    body = r.json()
    assert body["strategies"][0]["stage"] == "running"
    assert body["account_statuses"][0]["status"] == "OK"


def test_overview_accounts_channel_degraded_not_fatal(client, monkeypatch):
    """account-statuses 通道挂了（非 200）→ overview 仍返 strategies（降级不阻断）。"""
    monkeypatch.setattr(gc, "runtime_config",
                        lambda strategy_dir=None: {"token": "t", "account_id": "ACC"})

    def fake_get(path, token, timeout=3.0):
        if path == "/v3/strategies":
            return 200, {"data": [{"id": "s1", "stage": "running"}]}
        return 500, None

    monkeypatch.setattr(gc, "api_get", fake_get)
    r = client.get("/api/v1/gm/overview")
    assert r.status_code == 200
    assert r.json()["account_statuses"] is None


def test_runtime_missing_gives_502(client, monkeypatch):
    def boom(strategy_dir=None):
        raise FileNotFoundError("runtime.json 缺失")

    monkeypatch.setattr(gc, "runtime_config", boom)
    r = client.get("/api/v1/gm/trades")
    assert r.status_code == 502
    assert "runtime.json" in r.json()["detail"]


def test_readonly_surface_only():
    """六路由全 GET——交易动作族（下单/撤单/启停/风控配置）绝不进本路由。"""
    from presentation.server.api.v1.gm import router
    for route in router.routes:
        assert set(route.methods) == {"GET"}, f"{route.path} 必须只读（GET）"
    paths = " ".join(r.path for r in router.routes)
    for banned in ("order", "cancel", "stop", "risk", "close"):
        assert banned not in paths.lower().replace("orders", "ORDER_KEEP"), \
            f"代理面出现疑似写操作路径：{banned}"
