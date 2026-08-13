# -*- coding: utf-8 -*-
"""Task G2 · SSE 日志流 cookie 鉴权单测（DG-G2）。

物理意图（DG-G2 裁决：cookie 优先，非 query token）：
    ``/api/v1/logs/stream`` 是 SSE 长连接，前端用 ``EventSource`` 订阅。EventSource
    API **无法自定义请求头**（不能加 ``Authorization: Bearer``），故 Bearer 鉴权在此
    场景失效。DG-G2 裁决走 **cookie**（``Cookie: quanter_ro=<token>``）：

      - 同源自动携带（前端 document.cookie 设置后，EventSource 请求自动带上）；
      - 不进 URL / access log（query 参数 ``?token=xxx`` 会进 nginx/uvicorn access log
        泄露，cookie 走 Header 不进 access log 的 query 段）。

    本测试钉死契约：
      ① live + 已配 token + 无 cookie → 401（fail-closed，防 SSE 裸奔）；
      ② live + 已配 token + 正确 cookie → 非 401（鉴权通过，进入 SSE 流）；
      ③ live + 已配 token + 错误 cookie → 401（cookie 值必须 == QUANTER_API_TOKEN）；
      ④ dry_run + 无 token → 放行（开发态 SSE 不阻断，与 require_write 同语义）。

Why patch ``_sse_event_gen``：真 SSE 生成器是**无限流**（循环 await q.get()），TestClient
会同步消费整个响应体直至流结束 → 永久挂起。本测试目标只验**鉴权层**（cookie 依赖），
不验流体本身（流体契约由 logs.py 自身保证），故把 ``_sse_event_gen`` 替换为「yield 一帧
即 return」的有限生成器——依赖层 raise 401 时不进入生成器，依赖层通过时进入生成器
yield 一帧正常结束，两种路径都不挂起。

Why 不触发 lifespan：``_make_client`` 不进 with 块。main.py lifespan 装配 TradingEngine
（bootstrap 连 QMT 网关），单测环境无凭证 + 端口/线程污染。logs_router 只依赖模块级
``log_stream_hub`` 单例（不读 app.state），且测试 patch 了生成器，lifespan 不跑不影响
鉴权层契约验证。
"""
import pytest

from fastapi.testclient import TestClient


async def _fake_sse_gen():
    """有限 SSE 生成器替身：yield 一帧即 return（避免无限流挂起 TestClient）。

    Why async def + yield：与真 ``_sse_event_gen`` 同为 async generator，
    StreamingResponse 接受无差别。
    """
    yield "data: test\n\n"


@pytest.fixture(autouse=True)
def _patch_sse_gen(monkeypatch):
    """所有测试统一 patch 掉无限 SSE 生成器（鉴权层测试不依赖真实日志流体）。"""
    import presentation.server.api.v1.logs as logs_mod
    monkeypatch.setattr(logs_mod, "_sse_event_gen", _fake_sse_gen)


def _make_client():
    """构造 TestClient（不进 with → 不触发 lifespan，不装配真 engine）。"""
    from presentation.server.main import app
    return TestClient(app, raise_server_exceptions=False)


def test_logs_stream_without_cookie_rejected(monkeypatch):
    """live + 已配 token + 无 cookie → 401（SSE fail-closed，防日志流裸奔）。"""
    monkeypatch.setenv("AUTO_TRADE_MODE", "live")
    monkeypatch.setenv("QUANTER_API_TOKEN", "secret")
    client = _make_client()
    r = client.get("/api/v1/logs/stream")
    assert r.status_code == 401


def test_logs_stream_with_cookie_accepted(monkeypatch):
    """live + 已配 token + 正确 cookie → 非 401（鉴权通过，进入 SSE 流）。"""
    monkeypatch.setenv("AUTO_TRADE_MODE", "live")
    monkeypatch.setenv("QUANTER_API_TOKEN", "secret")
    client = _make_client()
    r = client.get(
        "/api/v1/logs/stream", cookies={"quanter_ro": "secret"}
    )
    assert r.status_code != 401  # 鉴权通过（200，进入 patched 有限 SSE 流）


def test_logs_stream_with_wrong_cookie_rejected(monkeypatch):
    """live + 已配 token + 错误 cookie → 401（cookie 值必须 == QUANTER_API_TOKEN）。"""
    monkeypatch.setenv("AUTO_TRADE_MODE", "live")
    monkeypatch.setenv("QUANTER_API_TOKEN", "secret")
    client = _make_client()
    r = client.get(
        "/api/v1/logs/stream", cookies={"quanter_ro": "wrong-token"}
    )
    assert r.status_code == 401


def test_logs_stream_dry_run_no_token_allowed(monkeypatch):
    """dry_run + 无 token → 放行（开发态 SSE 不阻断，与 require_write 同语义）。"""
    monkeypatch.setenv("AUTO_TRADE_MODE", "dry_run")
    monkeypatch.delenv("QUANTER_API_TOKEN", raising=False)
    client = _make_client()
    r = client.get("/api/v1/logs/stream")
    assert r.status_code != 401  # 放行，进入 patched 有限 SSE 流
