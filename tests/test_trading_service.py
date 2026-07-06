# -*- coding: utf-8 -*-
"""trading_service 四态心跳 + 熔断幂等 + Phase 1 下单/连接/流水测试。

锁定契约：
1) status 四态严格镜像网关：unavailable / disconnected / live / vetoed_by_risk
2) emergency_halt 幂等：lock_down 已置位时重复调用不重复撤单
3) 网关未装配时 raise RuntimeError（路由层转 503）
4) Phase 1：submit_order dry_run/挡板/真单三分支 + 流水全覆盖
"""
import asyncio

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


# ============ Phase 1 Task 5：submit_order / connect / 流水 ============
def _fake_gw_connected():
    """造一个已连接、未锁定的假网关，记录 submit_order 调用。"""
    class _FakeGW:
        def __init__(self):
            self._connected = True
            self._lock_down = False
            self._orders = {}
            self.submit_calls = []
            self.connect_called = False

        @property
        def is_locked(self):
            return self._lock_down

        async def connect(self):
            self.connect_called = True
            self._connected = True
            self._lock_down = False

        async def disconnect(self):
            self._connected = False

        async def submit_order(self, order):
            self.submit_calls.append(order)
            from trading.execution_gateway import OrderResult
            from trading.order_state import OrderState
            return OrderResult(order_id="100", state=OrderState.SUBMITTED, message="ok")

        async def cancel_order(self, order_id):
            from trading.execution_gateway import OrderResult
            from trading.order_state import OrderState
            return OrderResult(order_id=order_id, state=OrderState.CANCELLED, message="ok")
    return _FakeGW()


def test_connect_gateway(monkeypatch):
    from server.services import trading_service
    gw = _fake_gw_connected()
    monkeypatch.setattr(trading_service, "get_qmt_gateway", lambda: gw)
    asyncio.run(trading_service.connect_gateway())
    assert gw.connect_called is True


def test_connect_gateway_unavailable(monkeypatch):
    from server.services import trading_service
    monkeypatch.setattr(trading_service, "get_qmt_gateway", lambda: None)
    with pytest.raises(RuntimeError):
        asyncio.run(trading_service.connect_gateway())


def test_submit_order_dry_run_records_and_returns(monkeypatch):
    """dry_run=True → 不调网关下单，落 DRY_RUN_BUY 流水，返 state=DRY_RUN。

    不 patch get_quote：conftest 假 xtdata.get_full_tick 返 {} → get_quote 返 None，
    挡板跳过涨跌停关（dry_run 在第 2 关即命中，根本到不了第 9 关）。
    """
    from server.services import trading_service
    from trading.execution_gateway import OrderRequest

    gw = _fake_gw_connected()
    monkeypatch.setattr(trading_service, "get_qmt_gateway", lambda: gw)

    recorded = []
    monkeypatch.setattr(trading_service, "record_live_trade",
                        lambda *a, **kw: recorded.append((a, kw)))

    order = OrderRequest(symbol="510300.SH", qty=100, side="buy", price=5.0)
    r = asyncio.run(trading_service.submit_order(order, dry_run=True, confirm=True))
    assert r["state"] == "DRY_RUN"
    assert gw.submit_calls == []  # 未真下单
    assert recorded and recorded[0][0][1] == "DRY_RUN_BUY"  # 落 DRY_RUN 流水


def test_submit_order_blocked_raises(monkeypatch):
    """挡板命中（白名单外）→ raise RuntimeError + 落 BLOCKED 流水。"""
    from server.services import trading_service
    from trading.execution_gateway import OrderRequest

    gw = _fake_gw_connected()
    monkeypatch.setattr(trading_service, "get_qmt_gateway", lambda: gw)

    recorded = []
    monkeypatch.setattr(trading_service, "record_live_trade",
                        lambda *a, **kw: recorded.append(a))
    # env helper 用 raising=False：实现前属性可能不存在，不报错直接创建
    monkeypatch.setattr(trading_service, "_allow_live", lambda: True, raising=False)
    monkeypatch.setattr(trading_service, "_whitelist", lambda: {"510300.SH"}, raising=False)

    order = OrderRequest(symbol="000001.SZ", qty=100, side="buy", price=5.0)
    with pytest.raises(RuntimeError):
        asyncio.run(trading_service.submit_order(order, dry_run=False, confirm=True))
    assert recorded and recorded[0][1] == "BLOCKED"


def test_submit_order_live_calls_gateway(monkeypatch):
    """dry_run=False + 全过 → 调网关 submit_order。"""
    from server.services import trading_service
    from trading.execution_gateway import OrderRequest

    gw = _fake_gw_connected()
    monkeypatch.setattr(trading_service, "get_qmt_gateway", lambda: gw)
    monkeypatch.setattr(trading_service, "_allow_live", lambda: True, raising=False)
    monkeypatch.setattr(trading_service, "_whitelist", lambda: {"510300.SH"}, raising=False)
    monkeypatch.setattr(trading_service, "_max_amount", lambda: 10000.0, raising=False)
    monkeypatch.setattr(trading_service, "_max_shares", lambda: 1000, raising=False)
    monkeypatch.setattr(trading_service, "_enforce_session", lambda: False, raising=False)

    order = OrderRequest(symbol="510300.SH", qty=100, side="buy", price=5.0)
    r = asyncio.run(trading_service.submit_order(order, dry_run=False, confirm=True))
    assert r["order_id"] == "100"
    assert gw.submit_calls and gw.submit_calls[0].symbol == "510300.SH"


def test_submit_order_disconnected_blocks(monkeypatch):
    """网关未连接 → 挡板 connection 关命中。"""
    from server.services import trading_service
    from trading.execution_gateway import OrderRequest

    gw = _fake_gw_connected()
    gw._connected = False
    monkeypatch.setattr(trading_service, "get_qmt_gateway", lambda: gw)

    order = OrderRequest(symbol="510300.SH", qty=100, side="buy", price=5.0)
    with pytest.raises(RuntimeError, match="连接"):
        asyncio.run(trading_service.submit_order(order, dry_run=False, confirm=True))
