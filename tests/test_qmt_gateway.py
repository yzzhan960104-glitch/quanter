# -*- coding: utf-8 -*-
"""QmtExecutionGateway 单测：conftest 已全局注入假 xtquant，本模块直接 import。

覆盖：状态映射纯函数 + _assert_status_contract + 连接时序 + 下单/撤单 +
seq→real 映射 + 断线锁定。FakeXtQuantTrader（conftest 注入）记录调用 + 返回可控 rc/seq。

关键纪律：connect 与后续 submit/cancel 必须在**同一个 asyncio.run** 内——
qmt_gateway.connect 捕获 `self._loop = get_running_loop()`，若跨 asyncio.run 调用，
旧 loop 已关闭，run_in_executor/call_soon_threadsafe 会抛 RuntimeError(loop closed)。
"""
import asyncio
import time
import types

import pytest
from unittest.mock import AsyncMock

# Layer2 阶段3：真身迁 broker.qmt（原 trading.qmt_gateway）；W2-H1 再分四文件。
# patch 内部全局（_CONNECT_TIMEOUT/XtQuantTrader/_client_servable 等）须指真身模块
# ——现真身 = 契约根 broker.qmt_connection（broker.qmt 只是组装+re-export 垫片，
# patch 垫片无效）。_ORDER_TIMEOUT 单源收口（N5 · Low ③）：qmt_io/qmt_business 均
# 改调用点模块属性访问，patch 超时统一指 broker.qmt_connection（一处生效三读取方）。
from broker import qmt_connection as qmt_gateway
from broker.qmt import QmtExecutionGateway, _map_qmt_status, _assert_status_contract
from trading.compute.types import OrderRequest  # Layer2 阶段6 follow-up #4b：execution_gateway 垫片已删，直指 compute.types 真身
from trading.types.order_state import OrderState  # Layer2 follow-up #4c：改指 types 真身

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
    assert _map_qmt_status(51) == OrderState.SUBMITTED  # 已报待撤（#9：保守不终态）


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


# ============ L2 轮换客户端可服务闸（G8 · 2026-08-14）============
def test_client_process_alive_states(monkeypatch):
    """进程探测三态：在跑→True / 未起→False / 探测失败→None（保守放行原逻辑）。"""
    import ops.process_topology as pt
    monkeypatch.setattr(pt, "client_status", lambda: {"running": True, "pid": 1, "count": 1})
    assert qmt_gateway._client_process_alive() is True
    monkeypatch.setattr(pt, "client_status", lambda: {"running": False, "pid": None, "count": 0})
    assert qmt_gateway._client_process_alive() is False
    monkeypatch.setattr(pt, "client_status", lambda: {"running": None, "pid": None, "count": None})
    assert qmt_gateway._client_process_alive() is None


def test_client_activity_age_secs(tmp_path, monkeypatch):
    """活跃文件 mtime 年龄：有新文件→年龄秒数；无活跃信号/目录缺失→None。"""
    import os
    monkeypatch.setattr("time.time", lambda: 1_000_000.0)
    # 无任何活跃 patterns（连 quoter 目录都没有）→ None
    assert qmt_gateway._client_activity_age_secs(str(tmp_path)) is None
    quoter = tmp_path / "quoter"
    quoter.mkdir()
    # quoter/* 文件 mtime=now-10 → 年龄 10（注意顺序：先写文件再设旧 mtime——
    # write_bytes 会刷新目录 mtime，pattern "quoter" 会匹配目录本身）
    f = quoter / "tick.bin"
    f.write_bytes(b"x")
    os.utime(f, (999_990.0, 999_990.0))
    os.utime(quoter, (999_990.0, 999_990.0))
    age = qmt_gateway._client_activity_age_secs(str(tmp_path))
    assert age is not None and abs(age - 10.0) < 1.0
    # 目录缺失 → None
    assert qmt_gateway._client_activity_age_secs(str(tmp_path / "nope")) is None


def test_client_servable_states(monkeypatch, tmp_path):
    """可服务三态：进程缺失→False；进程在但活跃文件陈旧→False；新鲜→True。"""
    import os
    import ops.process_topology as pt
    # 进程未起 → False
    monkeypatch.setattr(pt, "client_status", lambda: {"running": False, "pid": None, "count": 0})
    assert qmt_gateway._client_servable(str(tmp_path)) is False
    # 探测失败 → None（保守放行）
    monkeypatch.setattr(pt, "client_status", lambda: {"running": None, "pid": None, "count": None})
    assert qmt_gateway._client_servable(str(tmp_path)) is None
    # 进程在跑 + 无活跃文件信号 → None（保守放行）
    monkeypatch.setattr(pt, "client_status", lambda: {"running": True, "pid": 1, "count": 1})
    assert qmt_gateway._client_servable(str(tmp_path)) is None
    # 进程在跑 + 活跃文件陈旧（>300s）→ False
    quoter = tmp_path / "quoter"
    quoter.mkdir()
    f = quoter / "tick.bin"
    f.write_bytes(b"x")
    old = time.time() - 600
    os.utime(f, (old, old))
    os.utime(quoter, (old, old))   # 目录 mtime 同步设旧（patterns 含 "quoter" 目录本身）
    assert qmt_gateway._client_servable(str(tmp_path)) is False
    # 进程在跑 + 活跃文件新鲜（<300s）→ True
    fresh = time.time() - 10
    os.utime(f, (fresh, fresh))
    os.utime(quoter, (fresh, fresh))
    assert qmt_gateway._client_servable(str(tmp_path)) is True


def test_connect_skips_rotation_when_client_not_servable(monkeypatch):
    """G8：connect -1 + 客户端不可服务 → 跳过 L2 轮换直进 L3（快速失败不再无效轮换）。

    物理意图：客户端缺失/未登录时 -1 语义是「客户端未就绪」而非「sid 被占」，轮换
    100 候选纯属无效功（每候选 30s 超时 + 75MB 队列文件爆盘，实测 ~50 分钟挂死 +
    ≈2.7GB 磁盘）。跳过轮换后 connect 抛 ConnectionError 快速返回，恢复交给
    health_guard 60s 轮询。
    """
    _setup_env(monkeypatch)
    monkeypatch.setattr(FakeTrader, "connect_rc", -1)
    monkeypatch.setattr(qmt_gateway, "_client_servable", lambda *a, **k: False)
    # 轮换若被调用 → AssertionError 让测试响亮失败
    monkeypatch.setattr(QmtExecutionGateway, "_try_rotate_session",
                        AsyncMock(side_effect=AssertionError("客户端不可服务不应进入 L2 轮换")))

    async def run():
        gw = QmtExecutionGateway()
        with pytest.raises(ConnectionError):
            await gw.connect()
        assert gw._lock_down is True

    asyncio.run(run())


def test_connect_rotates_when_client_servable(monkeypatch):
    """客户端可服务 → L2 轮换照常执行（G8 不改可服务时的既有语义）。"""
    _setup_env(monkeypatch)
    monkeypatch.setattr(FakeTrader, "connect_rc", -1)
    monkeypatch.setattr(qmt_gateway, "_client_servable", lambda *a, **k: True)
    monkeypatch.setattr(QmtExecutionGateway, "_try_rotate_session",
                        AsyncMock(return_value=0))

    async def run():
        gw = QmtExecutionGateway()
        await gw.connect()          # 轮换成功（sub_rc=0）→ 整体连接成功
        assert gw._connected is True
        assert gw._lock_down is False

    asyncio.run(run())


def test_connect_rotation_exhausted_still_raises(monkeypatch):
    """客户端可服务但轮换耗尽 → 维持原 L3 失败语义（ConnectionError + lock_down）。"""
    _setup_env(monkeypatch)
    monkeypatch.setattr(FakeTrader, "connect_rc", -1)
    monkeypatch.setattr(qmt_gateway, "_client_servable", lambda *a, **k: True)
    monkeypatch.setattr(QmtExecutionGateway, "_try_rotate_session",
                        AsyncMock(return_value=None))

    async def run():
        gw = QmtExecutionGateway()
        with pytest.raises(ConnectionError):
            await gw.connect()
        assert gw._lock_down is True

    asyncio.run(run())


def test_submit_order_timeout_returns_failed(monkeypatch):
    """#9：order_stock_async 阻塞 → wait_for 超时返 FAILED（不抛、不卡事件循环）。"""
    import time
    _setup_env(monkeypatch)
    # N5（Low ③ 单源收口）：qmt_business 调用点已改 ``qmt_connection._ORDER_TIMEOUT``
    # 模块属性访问——patch 契约根即生效（from-import 副本已删，patch 旧读取方无效）。
    from broker import qmt_connection
    monkeypatch.setattr(qmt_connection, "_ORDER_TIMEOUT", 0.05)

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


# ============================================================================
# Task E1（live-mainchain-fixes）：状态 51 保守映射 SUBMITTED（#9）
# ============================================================================
def test_status_51_reported_cancel_maps_to_submitted():
    """51（已报待撤）保守映射 SUBMITTED：撤单刚受理不当终态（#9）。"""
    from broker.qmt import _map_qmt_status
    from trading.types.order_state import OrderState

    assert _map_qmt_status(51) is OrderState.SUBMITTED
