# -*- coding: utf-8 -*-
"""QmtExecutionGateway 单测：conftest 已全局注入假 xtquant，本模块直接 import。

覆盖：状态映射纯函数 + _assert_status_contract + 连接时序 + 下单/撤单 +
seq→real 映射 + 断线锁定。FakeXtQuantTrader（conftest 注入）记录调用 + 返回可控 rc/seq。

关键纪律：connect 与后续 submit/cancel 必须在**同一个 asyncio.run** 内——
qmt_gateway.connect 捕获 `self._loop = get_running_loop()`，若跨 asyncio.run 调用，
旧 loop 已关闭，run_in_executor/call_soon_threadsafe 会抛 RuntimeError(loop closed)。
"""
import asyncio
import types

import pytest

# Layer2 阶段3：真身迁 broker.qmt（原 trading.qmt_gateway）。
# patch 内部全局（_CONNECT_TIMEOUT/_ORDER_TIMEOUT/XtQuantTrader 等）须指真身模块，
# trading.qmt_gateway 垫片的 re-export 副本与真身非同一对象，patch 垫片无效。
from broker import qmt as qmt_gateway
from broker.qmt import QmtExecutionGateway, _map_qmt_status, _assert_status_contract
from trading.compute.types import OrderRequest  # Layer2 阶段6 follow-up #4b：execution_gateway 垫片已删，直指 compute.types 真身
from trading.order_state import OrderState

# qmt_gateway.XtQuantTrader 就是 conftest 注入的 FakeXtQuantTrader 类对象
FakeTrader = qmt_gateway.XtQuantTrader


def _setup_env(monkeypatch):
    """每个测试前重置 FakeTrader 类属性 + 配置凭证环境变量。"""
    monkeypatch.setattr(FakeTrader, "connect_rc", 0)
    monkeypatch.setattr(FakeTrader, "subscribe_rc", 0)
    monkeypatch.setattr(FakeTrader, "cancel_rc", 0)
    monkeypatch.setattr(FakeTrader, "order_seq", 100)
    monkeypatch.setattr(FakeTrader, "positions", None)
    monkeypatch.setenv("QMT_USERDATA_PATH", "D:/fake/userdata_mini")
    monkeypatch.setenv("QMT_ACCOUNT_ID", "62138335")


# ============ 状态映射纯函数 ============
def test_map_status_succeeded():
    assert _map_qmt_status(56) == OrderState.FILLED


def test_map_status_partial():
    assert _map_qmt_status(55) == OrderState.PARTIAL_FILLED


def test_map_status_junk():
    assert _map_qmt_status(57) == OrderState.REJECTED


def test_map_status_canceled_and_reported_cancel():
    assert _map_qmt_status(54) == OrderState.CANCELLED
    assert _map_qmt_status(51) == OrderState.CANCELLED  # 已报待撤


def test_map_status_partial_cancel():
    assert _map_qmt_status(53) == OrderState.PARTIAL_CANCELLED
    assert _map_qmt_status(52) == OrderState.PARTIAL_CANCELLED  # 部成待撤


def test_map_status_intermediate_returns_submitted():
    """48/49/50/255 中间态/未知 → 保守 SUBMITTED（不冒进终态）。"""
    for s in (48, 49, 50, 255):
        assert _map_qmt_status(s) == OrderState.SUBMITTED


def test_assert_status_contract_ok():
    """注入一致枚举 → 不抛。"""
    _assert_status_contract()  # 不抛即通过


# ============ 连接时序 ============
def test_connect_success(monkeypatch):
    _setup_env(monkeypatch)

    async def run():
        gw = QmtExecutionGateway()
        await gw.connect()
        assert gw._connected is True
        assert gw._lock_down is False
        # 时序：register_callback → start → connect → subscribe
        assert gw._trader.calls[:4] == ["register_callback", "start", "connect", "subscribe"]

    asyncio.run(run())


def test_connect_failure_raises(monkeypatch):
    _setup_env(monkeypatch)
    monkeypatch.setattr(FakeTrader, "connect_rc", 1)  # connect 返非 0
    gw = QmtExecutionGateway()
    with pytest.raises(ConnectionError):
        asyncio.run(gw.connect())


def test_connect_timeout_raises(monkeypatch):
    """#9：connect 柜台无响应（阻塞）→ wait_for 超时 raise ConnectionError，事件循环不卡死。

    物理意图：柜台断连/无响应时底层 start/connect/subscribe 是同步阻塞 C++ 调用，无 wait_for
    会永久卡住事件循环。wait_for 兜底后超时转 ConnectionError，由 _reconnect 重试。
    """
    import time
    _setup_env(monkeypatch)
    monkeypatch.setattr(qmt_gateway, "_CONNECT_TIMEOUT", 0.05)

    def _slow_connect(self):
        time.sleep(0.5)   # 模拟柜台无响应
        return 0
    monkeypatch.setattr(FakeTrader, "connect", _slow_connect)

    gw = QmtExecutionGateway()
    with pytest.raises(ConnectionError):
        asyncio.run(gw.connect())


def test_submit_order_timeout_returns_failed(monkeypatch):
    """#9：order_stock_async 阻塞 → wait_for 超时返 FAILED（不抛、不卡事件循环）。"""
    import time
    _setup_env(monkeypatch)
    monkeypatch.setattr(qmt_gateway, "_ORDER_TIMEOUT", 0.05)

    def _slow_order(self, *args):
        time.sleep(0.5)
        return 999
    monkeypatch.setattr(FakeTrader, "order_stock_async", _slow_order)

    async def run():
        gw = QmtExecutionGateway()
        await gw.connect()   # 正常 connect（_CONNECT_TIMEOUT 默认 30s 不超时）
        order = OrderRequest(symbol="510300.SH", qty=100, side="buy", price=5.0)
        res = await gw.submit_order(order)
        assert res.state == OrderState.FAILED   # 超时兜底返 FAILED

    asyncio.run(run())


def test_cleanup_orders_removes_stale_terminal_only(monkeypatch):
    """#10：cleanup_orders 删终态+超期单；保留非终态（无论时长）+ 终态未超期。"""
    import time
    _setup_env(monkeypatch)
    now = time.time()
    gw = QmtExecutionGateway()
    gw._orders["old_filled"] = {"state": OrderState.FILLED, "_gc_ts": now - 8 * 86400}      # 删
    gw._orders["old_cancel"] = {"state": OrderState.CANCELLED, "_gc_ts": now - 8 * 86400}   # 删
    gw._orders["new_filled"] = {"state": OrderState.FILLED, "_gc_ts": now - 86400}          # 留（未超期）
    gw._orders["pending"] = {"state": OrderState.SUBMITTED, "_gc_ts": now - 10 * 86400}     # 留（非终态）
    removed = gw.cleanup_orders(keep_seconds=7 * 86400)
    assert removed == 2
    assert "old_filled" not in gw._orders and "old_cancel" not in gw._orders
    assert "new_filled" in gw._orders and "pending" in gw._orders


def test_missing_credentials_raises(monkeypatch):
    """无 QMT_USERDATA_PATH → 构造即 ValueError。"""
    monkeypatch.delenv("QMT_USERDATA_PATH", raising=False)
    monkeypatch.setenv("QMT_ACCOUNT_ID", "62138335")
    with pytest.raises(ValueError):
        QmtExecutionGateway()


# ============ 下单/撤单 ============
def test_submit_order_returns_seq(monkeypatch):
    _setup_env(monkeypatch)

    async def run():
        gw = QmtExecutionGateway()
        await gw.connect()
        order = OrderRequest(symbol="510300.SH", qty=100, side="buy", price=5.0)
        res = await gw.submit_order(order)
        assert res.state == OrderState.SUBMITTED
        assert res.order_id == "100"  # FakeTrader.order_seq 起始 100

    asyncio.run(run())


def test_submit_order_rejected_on_neg_seq(monkeypatch):
    """order_stock_async 返 -1 → REJECTED（柜台拒单）。"""
    _setup_env(monkeypatch)
    monkeypatch.setattr(FakeTrader, "order_seq", -1)  # 下一次 order_stock_async 返 -1

    async def run():
        gw = QmtExecutionGateway()
        await gw.connect()
        order = OrderRequest(symbol="510300.SH", qty=100, side="buy", price=5.0)
        res = await gw.submit_order(order)
        assert res.state == OrderState.REJECTED

    asyncio.run(run())


def test_cancel_without_mapping_fails(monkeypatch):
    """seq→real 映射未建立 → cancel FAILED（引导上层短暂重试）。"""
    _setup_env(monkeypatch)

    async def run():
        gw = QmtExecutionGateway()
        await gw.connect()
        res = await gw.cancel_order("999")
        assert res.state == OrderState.FAILED

    asyncio.run(run())


def test_cancel_after_async_response(monkeypatch):
    """on_order_stock_async_response 建立映射后 → cancel 成功发出。"""
    _setup_env(monkeypatch)

    async def run():
        gw = QmtExecutionGateway()
        await gw.connect()
        # 模拟 async_response 回调：seq=100 → real_order_id=8888
        gw.on_order_stock_async_response(
            types.SimpleNamespace(seq=100, order_id=8888)
        )
        assert gw._seq_to_real[100] == 8888
        res = await gw.cancel_order("100")
        assert res.state == OrderState.CANCELLED

    asyncio.run(run())


# ============ 断线锁定 ============
def test_on_disconnected_locks(monkeypatch):
    """on_disconnected 回调 → is_locked=True（断线熔断）。

    在同一活 loop 内 connect + on_disconnected，确保 call_soon_threadsafe 投递成功。
    """
    _setup_env(monkeypatch)

    async def run():
        gw = QmtExecutionGateway()
        await gw.connect()
        assert gw.is_locked is False
        gw.on_disconnected()  # self._loop 是当前运行 loop
        await asyncio.sleep(0.01)  # 让 call_soon_threadsafe 投递落地
        assert gw.is_locked is True
        assert gw._connected is False

    asyncio.run(run())
