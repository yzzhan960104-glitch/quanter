# -*- coding: utf-8 -*-
"""API 鉴权依赖测试（B-1：HTTPBearer token + 可选 IP 白名单）。

覆盖契约：
  - QUANTER_API_TOKEN 未配置 → 开发态放行（但 WARNING，生产必须配置）；
  - 配置后：缺 Bearer / 错 Bearer → 401；正确 Bearer → 200；
  - QUANTER_ALLOWED_IPS 配置时：来源 IP 不在白名单 → 403（纵深防御）。

Why「未配置即放行」：避免破坏本地开发/CI（既有 API 测试不设 token）。生产部署
必须显式配置 QUANTER_API_TOKEN，未配置会在每次请求打 WARNING 提醒。
"""
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from server.core.auth import require_write


def _app_with_protected_endpoint() -> FastAPI:
    """最小 app：一个受 require_write 保护的 POST 端点。"""
    app = FastAPI()

    @app.post("/protected", dependencies=[Depends(require_write)])
    def _protected():
        return {"ok": True}

    return app


def test_no_token_configured_allows_dev_mode(monkeypatch):
    """token 未配置 → 开发态放行（不阻断本地/CI）。"""
    monkeypatch.delenv("QUANTER_API_TOKEN", raising=False)
    client = TestClient(_app_with_protected_endpoint())
    assert client.post("/protected").status_code == 200


def test_token_configured_rejects_missing_bearer(monkeypatch):
    """token 已配置但请求无 Authorization 头 → 401。"""
    monkeypatch.setenv("QUANTER_API_TOKEN", "s3cret")
    client = TestClient(_app_with_protected_endpoint())
    assert client.post("/protected").status_code == 401


def test_token_configured_rejects_wrong_token(monkeypatch):
    """token 已配置但 Bearer 值错误 → 401（常量时间比较，防时序攻击）。"""
    monkeypatch.setenv("QUANTER_API_TOKEN", "s3cret")
    client = TestClient(_app_with_protected_endpoint())
    r = client.post("/protected", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


def test_token_configured_accepts_correct_token(monkeypatch):
    """token 已配置且 Bearer 正确 → 200。"""
    monkeypatch.setenv("QUANTER_API_TOKEN", "s3cret")
    client = TestClient(_app_with_protected_endpoint())
    r = client.post("/protected", headers={"Authorization": "Bearer s3cret"})
    assert r.status_code == 200


def test_ip_whitelist_rejects_unknown_ip(monkeypatch):
    """QUANTER_ALLOWED_IPS 配置且来源 IP 不在白名单 → 403（纵深防御）。"""
    monkeypatch.setenv("QUANTER_API_TOKEN", "s3cret")
    # 白名单只含内网 IP，TestClient 来源（testclient/127.0.0.1）不在其中 → 403
    monkeypatch.setenv("QUANTER_ALLOWED_IPS", "10.0.0.1,10.0.0.2")
    client = TestClient(_app_with_protected_endpoint())
    r = client.post("/protected", headers={"Authorization": "Bearer s3cret"})
    assert r.status_code == 403


def test_ip_whitelist_allows_listed_ip(monkeypatch):
    """来源 IP 在白名单内 → 200（TestClient 的 client.host 为 "testclient"）。"""
    monkeypatch.setenv("QUANTER_API_TOKEN", "s3cret")
    monkeypatch.setenv("QUANTER_ALLOWED_IPS", "testclient,127.0.0.1")
    client = TestClient(_app_with_protected_endpoint())
    r = client.post("/protected", headers={"Authorization": "Bearer s3cret"})
    assert r.status_code == 200
