# -*- coding: utf-8 -*-
"""per-run SSE 回测流：建 run → 流式收到 progress/trade/result → [DONE]。

为什么"两步式"（POST 建 run + GET 流式）：
- 浏览器原生 EventSource 仅支持 GET 方法，无法直接 POST 携带回测参数。
- 故拆为：POST /run/async 提交参数、领取 run_id；GET /run/stream/{run_id}
  用 EventSource 订阅该 run 的事件流。run_id 进程内注册表串联两次请求。

本测试守护"接口契约"：
- test_create_run_returns_runid：POST /run/async 必须返 200 + {run_id}。
- test_stream_unknown_run_404：未知 run_id 必须 404（防注册表未命中时挂起空流）。

端到端流式（progress/trade/result 帧 → [DONE]）依赖真实数据源与线程池驱动，
留作手动验证；此处仅守护契约两端。
"""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    """复用全局 app（lifespan 已装配日志/数据湖等依赖）。

    Why TestClient(app)：FastAPI 推荐的 ASGI 测试入口，能透传 lifespan 启动钩子，
    与 SSE 端点共享同一进程内 _run_registry（关键：注册表是模块级 dict，
    必须与路由引用同一实例才能命中）。
    """
    from server.main import app
    return TestClient(app)


def test_create_run_returns_runid(client):
    """POST /run/async 必须返回 200 + {run_id: <uuid>}。

    断言：
    - status_code == 200：端点存在且参数合法。
    - "run_id" in body.json()：契约字段存在（前端凭它拼 stream URL）。

    payload 满足 BacktestRequest 必填字段（symbol/start_date/end_date），
    initial_capital / signal_freq 走 schema 缺省；start < end 已保证。
    """
    resp = client.post("/api/v1/backtest/run/async", json={
        "symbol": "000001.SZ",
        "start_date": "2024-01-01",
        "end_date": "2024-02-01",
        "initial_capital": 1000000,
        "signal_freq": "1d",
    })
    assert resp.status_code == 200, resp.text
    assert "run_id" in resp.json()


def test_stream_unknown_run_404(client):
    """GET /run/stream/{未知 run_id} 必须 404。

    防 regression：若注册表未命中时不返 404 而是空 yield，客户端会挂起空 SSE 流，
    体验灾难。这里强制守护"未命中即 404"契约。
    """
    resp = client.get("/api/v1/backtest/run/stream/does-not-exist")
    assert resp.status_code == 404
