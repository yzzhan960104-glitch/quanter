# -*- coding: utf-8 -*-
"""EmtExecutionGateway 单测：conftest 已注入假 vnemttrader，本模块直接 import。

覆盖：状态映射 + 标的后缀解析 + 登录时序 + 下单/撤单 + 断线锁定 + query 回调聚合。
connect 与后续操作在同一 asyncio.run 内（与 test_qmt_gateway 同款 loop 隔离纪律）。
"""
import asyncio

import pytest

from trading import emt_gateway
from trading.emt_gateway import EmtExecutionGateway, _map_emt_status, _split_symbol
from trading.execution_gateway import OrderRequest
from trading.order_state import OrderState

# emt_gateway.TraderApi 即 conftest 注入的 FakeTraderApi
FakeApi = emt_gateway.TraderApi


def _setup(monkeypatch):
    """重置 FakeApi 类属性到默认（login 成功）+ 配置凭证 env。"""
    monkeypatch.setattr(FakeApi, "login_session", 1)
    monkeypatch.setattr(FakeApi, "order_emt_id_seq", 1000)
    monkeypatch.setattr(FakeApi, "cancel_rc", 1)
    monkeypatch.setattr(FakeApi, "query_position_data", None)
    monkeypatch.setattr(FakeApi, "query_asset_data", None)
    monkeypatch.setenv("EMT_IP", "1.2.3.4")
    monkeypatch.setenv("EMT_PORT", "19088")
    monkeypatch.setenv("EMT_USER", "510100014396")
    monkeypatch.setenv("EMT_PASSWORD", "Kg3625")


# ============ 状态映射 ============
def test_map_status_alltraded():
    assert _map_emt_status(1) == OrderState.FILLED


def test_map_status_parttraded():
    assert _map_emt_status(2) == OrderState.PARTIAL_FILLED


def test_map_status_partcancel():
    assert _map_emt_status(3) == OrderState.PARTIAL_CANCELLED


def test_map_status_canceled():
    assert _map_emt_status(5) == OrderState.CANCELLED


def test_map_status_rejected():
    assert _map_emt_status(6) == OrderState.REJECTED


def test_map_status_intermediate_submitted():
    """0/4/11 中间态/未知 → 保守 SUBMITTED（不冒进终态）。"""
    for s in (0, 4, 11):
        assert _map_emt_status(s) == OrderState.SUBMITTED


# ============ 标的后缀解析 ============
def test_split_symbol_sh():
    assert _split_symbol("600000.SH") == ("600000", 2)


def test_split_symbol_sz():
    assert _split_symbol("000001.SZ") == ("000001", 1)


def test_split_symbol_bj():
    assert _split_symbol("830001.BJ") == ("830001", 5)


def test_split_symbol_invalid_suffix():
    with pytest.raises(ValueError):
        _split_symbol("600000.US")


# ============ 连接 ============
def test_connect_success(monkeypatch):
    _setup(monkeypatch)

    async def run():
        gw = EmtExecutionGateway()
        await gw.connect()
        assert gw._connected is True
        assert gw._lock_down is False
        assert gw._session != 0

    asyncio.run(run())


def test_connect_failure_raises(monkeypatch):
    _setup(monkeypatch)
    monkeypatch.setattr(FakeApi, "login_session", 0)  # login 失败

    async def run():
        gw = EmtExecutionGateway()
        with pytest.raises(ConnectionError):
            await gw.connect()
        assert gw._lock_down is True

    asyncio.run(run())


def test_missing_credentials_raises(monkeypatch):
    monkeypatch.delenv("EMT_USER", raising=False)
    monkeypatch.setenv("EMT_IP", "1.2.3.4")
    monkeypatch.setenv("EMT_PORT", "19088")
    monkeypatch.setenv("EMT_PASSWORD", "x")
    with pytest.raises(ValueError):
        EmtExecutionGateway()


# ============ 下单 ============
def test_submit_order_returns_emt_id(monkeypatch):
    _setup(monkeypatch)

    async def run():
        gw = EmtExecutionGateway()
        await gw.connect()
        order = OrderRequest(symbol="510300.SH", qty=100, side="buy", price=5.0)
        res = await gw.submit_order(order)
        assert res.state == OrderState.SUBMITTED
        assert res.order_id == "1000"  # FakeApi.order_emt_id_seq 起始 1000

    asyncio.run(run())


def test_submit_order_market_price_rejected(monkeypatch):
    """第一版仅限价单（price 必填），市价拒。"""
    _setup(monkeypatch)

    async def run():
        gw = EmtExecutionGateway()
        await gw.connect()
        order = OrderRequest(symbol="510300.SH", qty=100, side="buy")  # price=None
        res = await gw.submit_order(order)
        assert res.state == OrderState.REJECTED

    asyncio.run(run())


def test_submit_order_rejected_on_zero_emt_id(monkeypatch):
    """insertOrder 返 0 → REJECTED（柜台拒单）。"""
    _setup(monkeypatch)

    async def run():
        gw = EmtExecutionGateway()
        await gw.connect()
        gw._api.order_emt_id_seq = 0  # 下一次 insertOrder 返 0
        order = OrderRequest(symbol="510300.SH", qty=100, side="buy", price=5.0)
        res = await gw.submit_order(order)
        assert res.state == OrderState.REJECTED

    asyncio.run(run())


def test_submit_order_passes_correct_order_dict(monkeypatch):
    """下单 order_dict 含正确 ticker/market/side/price_type（事实审查）。"""
    _setup(monkeypatch)

    async def run():
        gw = EmtExecutionGateway()
        await gw.connect()
        order = OrderRequest(symbol="000001.SZ", qty=200, side="sell", price=10.5)
        await gw.submit_order(order)
        # insertOrder 调用记录在 gw._api.calls
        order_call = next(c for c in gw._api.calls if c[0] == "insertOrder")
        od = order_call[1]
        assert od["ticker"] == "000001" and od["market"] == 1
        assert od["side"] == 2 and od["price_type"] == 1
        assert od["price"] == 10.5 and od["quantity"] == 200
        assert od["business_type"] == 0 and od["position_effect"] == 1

    asyncio.run(run())


# ============ 撤单 ============
def test_cancel_order_success(monkeypatch):
    _setup(monkeypatch)

    async def run():
        gw = EmtExecutionGateway()
        await gw.connect()
        res = await gw.cancel_order("12345")
        assert res.state == OrderState.CANCELLED  # cancel_rc=1（truthy）

    asyncio.run(run())


def test_cancel_order_failure(monkeypatch):
    _setup(monkeypatch)
    monkeypatch.setattr(FakeApi, "cancel_rc", 0)

    async def run():
        gw = EmtExecutionGateway()
        await gw.connect()
        res = await gw.cancel_order("12345")
        assert res.state == OrderState.FAILED

    asyncio.run(run())


def test_cancel_order_invalid_id(monkeypatch):
    _setup(monkeypatch)

    async def run():
        gw = EmtExecutionGateway()
        await gw.connect()
        res = await gw.cancel_order("not-a-number")
        assert res.state == OrderState.REJECTED

    asyncio.run(run())


# ============ 断线锁定 ============
def test_on_disconnected_locks(monkeypatch):
    _setup(monkeypatch)

    async def run():
        gw = EmtExecutionGateway()
        await gw.connect()
        assert gw.is_locked is False
        gw._api.onDisconnected(0)  # _EmtCallback 重写的 onDisconnected：置锁+投递
        await asyncio.sleep(0.01)
        assert gw.is_locked is True
        assert gw._connected is False

    asyncio.run(run())


# ============ query 回调聚合 ============
def test_fetch_broker_positions(monkeypatch):
    _setup(monkeypatch)
    monkeypatch.setattr(FakeApi, "query_position_data", [
        {"ticker": "510300", "market": 2, "total_qty": 200, "sellable_qty": 200, "avg_price": 5.0},
        {"ticker": "000001", "market": 1, "total_qty": 0, "sellable_qty": 0},  # 过滤掉
    ])

    async def run():
        gw = EmtExecutionGateway()
        await gw.connect()
        pos = await gw._fetch_broker_positions()
        assert pos == {"510300.SH": 200.0}  # sellable_qty=0 的被过滤

    asyncio.run(run())


def test_fetch_asset(monkeypatch):
    _setup(monkeypatch)
    monkeypatch.setattr(FakeApi, "query_asset_data", [
        {"total_asset": 100000.0, "buying_power": 50000.0, "withholding_amount": 0.0},
    ])

    async def run():
        gw = EmtExecutionGateway()
        await gw.connect()
        asset = await gw._fetch_asset()
        assert asset["total_asset"] == 100000.0
        assert asset["cash"] == 50000.0

    asyncio.run(run())


# ============ #9 网关超时兜底 ============
def test_connect_timeout_raises(monkeypatch):
    """#9：login 柜台无响应（阻塞）→ wait_for 超时 raise ConnectionError，事件循环不卡死。

    物理意图：柜台断连/无响应时底层 login 是同步阻塞 C++ 调用，无 wait_for 会永久卡住
    事件循环（所有协程停摆）。wait_for 兜底后超时转 ConnectionError，由 _reconnect 重试。
    """
    import time
    _setup(monkeypatch)
    monkeypatch.setattr(emt_gateway, "_CONNECT_TIMEOUT", 0.05)

    def _slow_login(self, ip, port, user, pwd, sock, local_ip):
        time.sleep(0.5)  # 模拟柜台无响应
        return 1
    monkeypatch.setattr(FakeApi, "login", _slow_login)

    async def run():
        gw = EmtExecutionGateway()
        with pytest.raises(ConnectionError):
            await gw.connect()
        assert gw._lock_down is True   # 超时即视为断线，置锁拒单

    asyncio.run(run())


def test_submit_order_timeout_returns_failed(monkeypatch):
    """#9：insertOrder 阻塞 → wait_for 超时返 FAILED（不抛、不卡事件循环）。"""
    import time
    _setup(monkeypatch)
    monkeypatch.setattr(emt_gateway, "_ORDER_TIMEOUT", 0.05)

    def _slow_insert(self, order, session):
        time.sleep(0.5)
        return 9999
    monkeypatch.setattr(FakeApi, "insertOrder", _slow_insert)

    async def run():
        gw = EmtExecutionGateway()
        await gw.connect()   # 正常 login（_CONNECT_TIMEOUT 默认 30s，不超时）
        order = OrderRequest(symbol="510300.SH", qty=100, side="buy", price=5.0)
        res = await gw.submit_order(order)
        assert res.state == OrderState.FAILED   # 超时兜底返 FAILED

    asyncio.run(run())


def test_cleanup_orders_removes_stale_terminal_only(monkeypatch):
    """#10：cleanup_orders 删终态+超期单；保留非终态（无论时长）+ 终态未超期。

    物理意图：_orders 终态单调增长致内存泄漏。GC 仅删「终态且超期」——非终态单
    （SUBMITTED/PARTIAL_FILLED）即使超期也必须保留（等回报推进，误删=丢订单状态）。
    """
    import time
    _setup(monkeypatch)
    now = time.time()
    gw = EmtExecutionGateway()
    gw._orders["old_filled"] = {"state": OrderState.FILLED, "_gc_ts": now - 8 * 86400}      # 删
    gw._orders["old_cancel"] = {"state": OrderState.CANCELLED, "_gc_ts": now - 8 * 86400}   # 删
    gw._orders["new_filled"] = {"state": OrderState.FILLED, "_gc_ts": now - 86400}          # 留（未超期）
    gw._orders["pending"] = {"state": OrderState.SUBMITTED, "_gc_ts": now - 10 * 86400}     # 留（非终态）
    removed = gw.cleanup_orders(keep_seconds=7 * 86400)
    assert removed == 2
    assert "old_filled" not in gw._orders and "old_cancel" not in gw._orders
    assert "new_filled" in gw._orders and "pending" in gw._orders
