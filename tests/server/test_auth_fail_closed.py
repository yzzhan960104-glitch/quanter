# -*- coding: utf-8 -*-
"""Task G2 · 鉴权 fail-closed 单测（DG-G2）。

物理意图（spec · task-G2-brief）：
    修复历史 fail-open 缺陷——``require_write`` 在 ``QUANTER_API_TOKEN`` 未配置时
    一律放行（仅打 WARNING），导致 live 模式（实盘）可无认证调用下单/熔断/落盘 API。
    本测试钉死翻转后的契约：

      ① live 模式 + 无 token → **fail-closed**（raise 401，绝不放行）。
      ② dry_run 模式 + 无 token → 放行（开发/CI 不阻断，向后兼容）。
      ③ token 已配置 + Bearer 匹配 → 放行（鉴权通过）。
      ④ token 已配置 + Bearer 不匹配 → 401（常量时间比较，防时序侧信道）。

Why cred 显式传 None：``require_write`` 形参 ``cred`` 默认值是 ``Depends(_bearer)``
（FastAPI 依赖注入契约），脱离路由直接调用会落入 Depends 对象。单测显式 ``cred=None``
模拟「客户端未带 Authorization 头」的真实运行态，避免 Depends 对象污染。
"""
import pytest

from presentation.server.http.auth import require_write


class _FakeReq:
    """轻量 Request 替身：仅承载 require_write 用到的 ``client.host``。

    Why 不用 starlette.Request：require_write 仅读 ``request.client.host``（IP 白名单
    分支），构造真 Request 需走 ASGI 协议栈（scope/receive/send），对纯鉴权单测属
    过度装配。_FakeReq 显式隔离被测契约。
    """

    def __init__(self, client_host: str = "127.0.0.1") -> None:
        self.client = type("C", (), {"host": client_host})()


def test_live_mode_no_token_rejected(monkeypatch):
    """live 模式 token 未配 → fail-closed（raise 401，不放行）。DG-G2 核心铁律。

    物理意图：实盘进程（AUTO_TRADE_MODE=live）必须显式配 QUANTER_API_TOKEN，
    否则下单 API 默认裸奔——局域网/同机任意进程可无认证触发真单。fail-closed
    把「忘配 token」从「静默放行+WARNING」翻转为「硬拒」。
    """
    monkeypatch.setenv("AUTO_TRADE_MODE", "live")
    monkeypatch.delenv("QUANTER_API_TOKEN", raising=False)
    with pytest.raises(Exception):  # HTTPException(401)
        require_write(_FakeReq(), cred=None)


def test_dry_run_no_token_allowed(monkeypatch):
    """dry_run 模式允许无 token（开发态/CI 不阻断，向后兼容）。

    物理意图：本地开发与 CI 不强制配 token（既有 API 测试不设 token），fail-closed
    仅作用于 live 模式；dry_run 维持放行+WARNING 语义，避免破坏开发体验与 CI。
    """
    monkeypatch.setenv("AUTO_TRADE_MODE", "dry_run")
    monkeypatch.delenv("QUANTER_API_TOKEN", raising=False)
    assert require_write(_FakeReq(), cred=None) is None  # 放行


def test_token_configured_bearer_match_allowed(monkeypatch):
    """token 已配 + Bearer 匹配 → 放行（鉴权通过，happy path）。

    物理意图：常量时间比较（secrets.compare_digest）通过即放行，无额外 IP 白名单时
    不应误拒合法请求。
    """
    monkeypatch.setenv("AUTO_TRADE_MODE", "live")
    monkeypatch.setenv("QUANTER_API_TOKEN", "secret-token")

    class _Cred:
        credentials = "secret-token"

    assert require_write(_FakeReq(), cred=_Cred()) is None


def test_token_configured_bearer_mismatch_rejected(monkeypatch):
    """token 已配 + Bearer 不匹配 → 401（防时序侧信道：compare_digest 常量时间）。

    物理意图：token 泄漏/伪造场景必须硬拒；compare_digest 避免按字符短路比较
    被时序攻击逐字节爆破。
    """
    monkeypatch.setenv("AUTO_TRADE_MODE", "live")
    monkeypatch.setenv("QUANTER_API_TOKEN", "secret-token")

    class _Cred:
        credentials = "wrong-token"

    with pytest.raises(Exception):  # HTTPException(401)
        require_write(_FakeReq(), cred=_Cred())
