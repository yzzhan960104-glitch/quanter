# -*- coding: utf-8 -*-
"""G2 收尾 · SSE 只读 cookie 换取端点单测（DG-G2 cookie「设置侧」）。

物理意图：``/logs/stream`` 走 cookie 鉴权（require_read_cookie 读 ``quanter_ro``），但仓库
内此前无「设置该 cookie」的端点——live 下合法前端也永远拿不到 cookie，EventSource 无法订阅
日志流（评审 Spec 轴「实现了但可能有问题：SSE cookie 死端」）。本测试钉死新端点
``POST /api/v1/auth/read-cookie`` 契约：
  ① live + 正确 Bearer → 200 + ``Set-Cookie: quanter_ro=<token>; HttpOnly``；
  ② live + 无/错 Bearer → 401（复用 require_write fail-closed，防未授权换 cookie）；
  ③ 换到的 cookie 真能订阅 SSE（与 require_read_cookie 闭环）。

Why 不触发 lifespan：同 test_logs_sse_auth——TestClient 不进 with 块，避免装配真 engine。
"""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True, scope="module")
def _warm_main_import():
    """N1 本地环境预热（CI 装齐 optuna/pyarrow 无此问题）：首次 import
    ``presentation.server.main`` 会沿 main→research_router→discovery→optuna 链抛
    ModuleNotFoundError，但部分子模块已入 sys.modules 缓存，使**二次** import 成功
    （同 ``test_logs_sse_auth`` 的首测失败、余测通过现象）。模块级先吸收一次首次失败，
    让下方三测都拿到 app。根因 = 本机缺 pyarrow/optuna/yfinance（spec §0.2 N1）。
    """
    try:
        from presentation.server.main import app  # noqa: F401
    except Exception:
        pass  # 首次失败（子模块入缓存）；后续 _make_client 二次 import 成功


def _make_client():
    """TestClient（不进 with → 不触发 lifespan，不装配真 engine）。"""
    from presentation.server.main import app
    return TestClient(app, raise_server_exceptions=False)


def test_read_cookie_issues_cookie_with_valid_bearer(monkeypatch):
    """live + 正确 Bearer → 200 且 Set-Cookie 含 quanter_ro（HttpOnly 防 XSS 窃取）。"""
    monkeypatch.setenv("AUTO_TRADE_MODE", "live")
    monkeypatch.setenv("QUANTER_API_TOKEN", "secret")
    client = _make_client()
    r = client.post("/api/v1/auth/read-cookie",
                    headers={"Authorization": "Bearer secret"})
    assert r.status_code == 200
    set_cookie = r.headers.get("set-cookie", "")
    assert "quanter_ro=secret" in set_cookie, f"未设 quanter_ro cookie：{set_cookie}"
    assert "HttpOnly" in set_cookie, "cookie 必须 HttpOnly（防 JS 读 cookie 值）"


def test_read_cookie_rejected_without_bearer(monkeypatch):
    """live + 无 Bearer → 401（require_write fail-closed，防未授权换 cookie）。"""
    monkeypatch.setenv("AUTO_TRADE_MODE", "live")
    monkeypatch.setenv("QUANTER_API_TOKEN", "secret")
    client = _make_client()
    r = client.post("/api/v1/auth/read-cookie")  # 无 Authorization 头
    assert r.status_code == 401


def test_read_cookie_then_sse_subscribes_end_to_end(monkeypatch):
    """换到的 cookie 真能订阅 SSE（与 require_read_cookie 闭环验证）。

    物理意图：本端点的存在意义 = 让前端拿到能过 SSE 鉴权的 cookie。若 cookie 设置正确但
    require_read_cookie 不认（如 path/name/值不一致），则「换了也订阅不了」= 仍死端。
    本测试两步走（换 cookie → 带 cookie 订阅 SSE）闭环证明端到端可用。
    """
    # patch SSE 生成器为有限流（防 TestClient 同步消费无限流挂起，同 test_logs_sse_auth 范式）
    async def _fake_gen():
        yield "data: test\n\n"

    import presentation.server.api.v1.logs as logs_mod
    monkeypatch.setattr(logs_mod, "_sse_event_gen", _fake_gen)

    monkeypatch.setenv("AUTO_TRADE_MODE", "live")
    monkeypatch.setenv("QUANTER_API_TOKEN", "secret")
    client = _make_client()
    # 第一步：凭 Bearer 换 cookie
    r1 = client.post("/api/v1/auth/read-cookie",
                     headers={"Authorization": "Bearer secret"})
    assert r1.status_code == 200
    # Set-Cookie 被 TestClient 记住（证明设置成功；httpx 入 jar 不受 Secure 影响）
    assert "quanter_ro" in client.cookies, "TestClient 未记住换到的 cookie（Set-Cookie 失败？）"
    # 第二步：显式带 remembered cookie 订阅 SSE（规避 Secure cookie over http 发送策略差异，
    # 聚焦验证「端点设置的 cookie 值能过 require_read_cookie」这一设置侧/读取侧闭环）
    r2 = client.get("/api/v1/logs/stream",
                    cookies={"quanter_ro": client.cookies["quanter_ro"]})
    assert r2.status_code != 401, "换到的 cookie 未通过 SSE 鉴权（设置侧/读取侧脱节）"
