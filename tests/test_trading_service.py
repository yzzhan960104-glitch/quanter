# -*- coding: utf-8 -*-
"""trading_service 四态心跳 + 熔断幂等测试（无 xtquant 环境下的优雅降级）。

锁定契约：
1) status 四态严格镜像网关：unavailable / disconnected / live / vetoed_by_risk
2) emergency_halt 幂等：lock_down 已置位时重复调用不重复撤单
3) 网关未装配时 raise RuntimeError（路由层转 503）
"""
import pytest


def test_status_unavailable_when_no_gateway(monkeypatch):
    """无网关单例（缺 QMT 凭证）→ mode='unavailable'。"""
    from server.services import trading_service
    monkeypatch.setattr(trading_service, "get_qmt_gateway", lambda: None)
    s = trading_service.get_status()
    assert s == {"connected": False, "locked": False, "mode": "unavailable"}


def test_status_disconnected_when_gateway_not_connected(monkeypatch):
    """网关存在但未 connect → mode='disconnected'。"""
    from server.services import trading_service
    gw = type("G", (), {"_connected": False, "is_locked": False})()
    monkeypatch.setattr(trading_service, "get_qmt_gateway", lambda: gw)
    s = trading_service.get_status()
    assert s["mode"] == "disconnected" and s["connected"] is False


def test_status_live_when_connected(monkeypatch):
    """已连接且未锁定 → mode='live'。"""
    from server.services import trading_service
    gw = type("G", (), {"_connected": True, "is_locked": False})()
    monkeypatch.setattr(trading_service, "get_qmt_gateway", lambda: gw)
    assert trading_service.get_status()["mode"] == "live"


def test_status_vetoed_when_locked(monkeypatch):
    """断线锁定 → mode='vetoed_by_risk'（锁定优先于 connected）。"""
    from server.services import trading_service
    gw = type("G", (), {"_connected": True, "is_locked": True})()
    monkeypatch.setattr(trading_service, "get_qmt_gateway", lambda: gw)
    assert trading_service.get_status()["mode"] == "vetoed_by_risk"


def test_emergency_halt_idempotent(monkeypatch):
    """连续两次 emergency_halt：第一次置 lock_down，第二次返'已处于'（不重复撤单）。"""
    from server.services import trading_service

    class FakeGW:
        def __init__(self):
            self._lock_down = False
            self._connected = True
            self._orders = {"1": {"state": "SUBMITTED"}, "2": {"state": "FILLED"}}

        @property
        def is_locked(self):
            return self._lock_down

    # 屏蔽告警副作用：mock 消费掉未 await 的 coroutine，避免 RuntimeWarning
    def _swallow_fire_and_forget(coro=None, *a, **kw):
        if coro is not None and hasattr(coro, "close"):
            coro.close()

    monkeypatch.setattr(trading_service, "fire_and_forget", _swallow_fire_and_forget)

    gw = FakeGW()
    monkeypatch.setattr(trading_service, "get_qmt_gateway", lambda: gw)

    r1 = trading_service.emergency_halt()
    assert r1["halted"] is True and gw._lock_down is True

    r2 = trading_service.emergency_halt()
    assert r2["halted"] is True
    assert "已处于" in r2["message"]


def test_emergency_halt_unavailable(monkeypatch):
    """无网关 → raise RuntimeError（路由层转 503）。"""
    from server.services import trading_service
    monkeypatch.setattr(trading_service, "get_qmt_gateway", lambda: None)
    with pytest.raises(RuntimeError):
        trading_service.emergency_halt()
