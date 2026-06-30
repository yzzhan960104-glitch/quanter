"""Explorer API：CPU 探针拒绝、Redis 宕机降级、正常派发。"""
from unittest.mock import patch, MagicMock
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from server.main import app
    return TestClient(app)


def test_grid_rejects_when_cpu_high(client, monkeypatch):
    monkeypatch.setattr("psutil.cpu_percent", lambda interval=0.0: 95.0)
    resp = client.post("/api/v1/explorer/grid", json={
        "factor": "cross_sectional_momentum", "universe": ["000001.SZ"],
        "start": "2024-01-01", "end": "2024-06-01"})
    assert resp.status_code in (429, 503)


def test_grid_falls_back_on_redis_down(client, monkeypatch):
    """Redis 连不上 → 降级线程池执行，返回 degraded=True。"""
    monkeypatch.setattr("psutil.cpu_percent", lambda interval=0.0: 10.0)
    import redis
    monkeypatch.setattr("server.api.v1.explorer.run_factor_grid",
                        MagicMock(delay=MagicMock(side_effect=redis.ConnectionError("down"))))
    # 让降级实现立刻返回一个哨兵
    monkeypatch.setattr("server.api.v1.explorer.run_factor_grid_impl",
                        lambda spec: {"degraded_marker": True})
    resp = client.post("/api/v1/explorer/grid", json={
        "factor": "cross_sectional_momentum", "universe": ["000001.SZ"],
        "start": "2024-01-01", "end": "2024-06-01"})
    assert resp.status_code == 200
    body = resp.json()
    assert body.get("degraded") is True
