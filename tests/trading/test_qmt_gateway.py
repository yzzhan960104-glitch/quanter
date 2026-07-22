# -*- coding: utf-8 -*-
"""QmtExecutionGateway 补全单测（on_account_status / query_asset / query_orders / 兜底 / polish / 持仓扩展）。"""
import asyncio
import pytest

from trading import qmt_gateway
from trading.order_state import OrderState
from trading.qmt_gateway import QmtExecutionGateway


class _FakeLoop:
    """模拟 asyncio loop：捕 call_soon_threadsafe 投递的回调，供断言。

    扩展支持 run_in_executor（T2 query_asset 需要）：同步执行并返回结果，
    不真起线程池（单元测试不需要并发，只要语义正确）。
    """
    def __init__(self):
        self.calls = []
    def call_soon_threadsafe(self, cb, *args):
        self.calls.append((cb, args))
    def create_task(self, coro):
        # 防 fire_and_forget 真起线程；静默关闭协程
        coro.close()
    def run_in_executor(self, executor, func, *args):
        # 同步执行：run_in_executor(None, lambda: trader.query_stock_asset(acc))
        # 真实 loop 返 concurrent.futures.Future 且自动跨线程桥接；测试场景下
        # 调用方本就在 loop 线程内（asyncio.run 建立的 loop），直接返 asyncio.Future
        # 并立即 set_result 即可被 await 正确消费（Python 3.10 asyncio.Future
        # 在同 loop 内 set_result + await 语义合法）。
        fut = asyncio.Future()
        try:
            fut.set_result(func(*args))
        except Exception as exc:
            fut.set_exception(exc)
        return fut


class _FakeStatus:
    """模拟 XtAccountStatus。"""
    def __init__(self, status: int):
        self.account_id = "1000000365"
        self.account_type = 2
        self.status = status


def _make_gw_with_fake_loop(monkeypatch):
    """构造一个绕过 xtquant/连接的 QmtExecutionGateway + fake loop（专测回调处理）。"""
    monkeypatch.setenv("QMT_USERDATA_PATH", "D:\\fake")
    monkeypatch.setenv("QMT_ACCOUNT_ID", "1000000365")
    gw = QmtExecutionGateway()
    gw._loop = _FakeLoop()
    gw._lock_down = False  # 初始未锁
    return gw


def test_on_account_status_disables_sys_locks_and_alerts(monkeypatch):
    """DISABLEBYSYS(8) → 置 _lock_down=True + 告警通道被触发。"""
    gw = _make_gw_with_fake_loop(monkeypatch)
    alerted = []
    monkeypatch.setattr(qmt_gateway, "_alert_account_status", lambda g, s, lvl: alerted.append((s, lvl)))
    gw.on_account_status(_FakeStatus(8))  # DISABLEBYSYS
    # C++ 线程投递了主线程处理
    assert len(gw._loop.calls) == 1
    cb, args = gw._loop.calls[0]
    cb(*args)  # 主线程执行 _on_account_status_change
    assert gw._lock_down is True
    assert alerted == [(8, "ERROR")]


def test_on_account_status_ok_clears_lock(monkeypatch):
    """OK(0) → 清 _lock_down。"""
    gw = _make_gw_with_fake_loop(monkeypatch)
    gw._lock_down = True
    monkeypatch.setattr(qmt_gateway, "_alert_account_status", lambda g, s, lvl: None)
    gw.on_account_status(_FakeStatus(0))
    cb, args = gw._loop.calls[0]
    cb(*args)
    assert gw._lock_down is False


def test_on_account_status_intermediate_states_only_log(monkeypatch):
    """CORRECTING(5)/WAITING_LOGIN(1)/INITING(4) 中间态不锁只 log。"""
    gw = _make_gw_with_fake_loop(monkeypatch)
    monkeypatch.setattr(qmt_gateway, "_alert_account_status", lambda g, s, lvl: None)
    for s in (5, 1, 4):
        gw._loop.calls.clear()
        gw.on_account_status(_FakeStatus(s))
        cb, args = gw._loop.calls[0]
        cb(*args)
        assert gw._lock_down is False  # 中间态不锁


def test_on_account_status_closed_not_lock(monkeypatch):
    """CLOSED(6) 收盘后不锁。"""
    gw = _make_gw_with_fake_loop(monkeypatch)
    monkeypatch.setattr(qmt_gateway, "_alert_account_status", lambda g, s, lvl: None)
    gw.on_account_status(_FakeStatus(6))
    cb, args = gw._loop.calls[0]
    cb(*args)
    assert gw._lock_down is False


# === T2: query_asset（解锁二期熔断 equity 源）=================================

class _FakeAsset:
    """模拟 XtAsset（xttrader.md「资产查询」返回结构）。

    字段对齐 xtquant.xttype.XtAsset：account_id/cash/frozen_cash/market_value/total_asset。
    """
    def __init__(self):
        self.account_id = "1000000365"
        self.cash = 50000.0
        self.frozen_cash = 1000.0      # brief 要求：frozen_cash 不在对外 4 字段里
        self.market_value = 200000.0
        self.total_asset = 250000.0


class _FakeTraderAsset:
    """模拟 self._trader，query_stock_asset 返 FakeAsset / None。"""
    def __init__(self, asset):
        self._asset = asset

    def query_stock_asset(self, account):
        # 忽略 account 参数（测试仅断言返值标准化）
        return self._asset


def test_query_asset_normalizes_to_4fields(monkeypatch):
    """query_stock_asset 返 XtAsset → 标准化 {account_id, cash, total_asset, market_value}。

    Why 4 字段对齐：一期 trading_service.get_asset 的 QMT 分支 + EMT _fetch_asset +
    前端 Asset 类型均只消费这 4 字段；frozen_cash 前端不用（YAGNI），故不透出。
    """
    gw = _make_gw_with_fake_loop(monkeypatch)
    gw._trader = _FakeTraderAsset(_FakeAsset())
    gw._account = object()
    gw._connected = True
    result = asyncio.run(gw.query_asset())
    assert result == {"account_id": "1000000365", "cash": 50000.0,
                      "total_asset": 250000.0, "market_value": 200000.0}
    # frozen_cash 不返回（前端不用，YAGNI）


def test_query_asset_none_returns_empty(monkeypatch):
    """query_stock_asset 返 None（查询失败/无资产）→ 返 {}。

    Why 降级语义对齐一期 get_asset 缺失：调用方按 {} 降级（如二期 circuit_breaker
    跳过当日损失检查），不抛异常、不脏读。
    """
    gw = _make_gw_with_fake_loop(monkeypatch)
    gw._trader = _FakeTraderAsset(None)
    gw._account = object()
    gw._connected = True
    result = asyncio.run(gw.query_asset())
    assert result == {}


def test_query_asset_locked_returns_empty(monkeypatch):
    """网关锁定（断线保护）→ 返 {}（不脏读）。

    Why 锁定拒读：断线/账号 DISABLEBYSYS 窗口期内 query_stock_asset 可能返回陈旧快照，
    若透出给 circuit_breaker 会让熔断基于错乱 equity 误判，故与 submit_order 同口径直接返 {}。
    """
    gw = _make_gw_with_fake_loop(monkeypatch)
    gw._trader = _FakeTraderAsset(_FakeAsset())
    gw._account = object()
    gw._connected = True
    gw._lock_down = True
    result = asyncio.run(gw.query_asset())
    assert result == {}


# === T4: query_orders / query_trades（主动查询，subscribe 兜底 + 对账强化）=====

class _FakeOrder:
    """模拟 XtOrder（xttrader.md「委托查询」返回结构）。

    字段对齐 xtquant.xttype.XtOrder：order_id/stock_code/order_type/order_volume/
    price/traded_volume/traded_price/order_status/status_msg/order_remark。
    order_status=56 即 _QMT_ORDER_SUCCEEDED，映射 OrderState.FILLED。
    """
    def __init__(self):
        self.order_id = 100
        self.stock_code = "600000.SH"
        self.order_type = 23
        self.order_volume = 1000
        self.price = 10.5
        self.traded_volume = 1000
        self.traded_price = 10.5
        self.order_status = 56  # SUCCEEDED → FILLED
        self.status_msg = ""
        self.order_remark = "test"


class _FakeTrade:
    """模拟 XtTrade（xttrader.md「成交查询」返回结构）。

    字段对齐 xtquant.xttype.XtTrade：order_id/stock_code/traded_volume/traded_price/
    traded_amount/traded_time。
    """
    def __init__(self):
        self.order_id = 100
        self.stock_code = "600000.SH"
        self.traded_volume = 1000
        self.traded_price = 10.5
        self.traded_amount = 10500.0
        self.traded_time = 20260722093000


class _FakeTraderOrders:
    """模拟 self._trader 的委托/成交查询（忽略 account，只返注入的 orders/trades）。"""
    def __init__(self, orders, trades):
        self._orders = orders
        self._trades = trades

    def query_stock_orders(self, account, cancelable_only=False):
        # 忽略 account / cancelable_only（测试仅断言返值标准化）
        return self._orders

    def query_stock_trades(self, account):
        # 忽略 account
        return self._trades


def test_query_orders_normalizes(monkeypatch):
    """query_stock_orders 返 list[XtOrder] → 标准化 list[dict]（state 返 OrderState 枚举）。

    Why state 返 OrderState 枚举（非 .name 字符串）：与 on_stock_order 回调存
    _orders 的 state 同型，亦与 circuit_breaker._TERMINAL（frozenset[OrderState]）
    同型；T5 惰性同步 merge _orders 时直接可用，消除类型转换埋点，避免 circuit_breaker
    终态判定踩「枚举≠字符串」陷阱（OrderState.FILLED not in {...字符串...} 恒 True）。
    """
    gw = _make_gw_with_fake_loop(monkeypatch)
    gw._trader = _FakeTraderOrders([_FakeOrder()], None)
    gw._account = object()
    gw._connected = True
    result = asyncio.run(gw.query_orders())
    assert len(result) == 1
    o = result[0]
    assert o["order_id"] == 100
    assert o["stock_code"] == "600000.SH"
    assert o["order_volume"] == 1000
    assert "state" in o          # _map_qmt_status(56) -> OrderState.FILLED
    assert o["state"] == OrderState.FILLED


def test_query_orders_none_returns_empty(monkeypatch):
    """query_stock_orders/query_stock_trades 返 None（查询失败/当日空）→ 返 []。

    Why 降级语义对齐 query_asset 的 {} 空降级：调用方（T5 惰性同步 / 二期盘后对账）
    按 [] 降级，不抛异常、不脏读。
    """
    gw = _make_gw_with_fake_loop(monkeypatch)
    gw._trader = _FakeTraderOrders(None, None)
    gw._account = object()
    gw._connected = True
    assert asyncio.run(gw.query_orders()) == []
    assert asyncio.run(gw.query_trades()) == []


def test_query_orders_locked_returns_empty(monkeypatch):
    """网关锁定（断线保护）→ query_orders 返 []（与 query_asset 同口径防脏读）。"""
    gw = _make_gw_with_fake_loop(monkeypatch)
    gw._trader = _FakeTraderOrders([_FakeOrder()], None)
    gw._account = object()
    gw._connected = True
    gw._lock_down = True
    assert asyncio.run(gw.query_orders()) == []


def test_query_trades_normalizes(monkeypatch):
    """query_stock_trades 返 list[XtTrade] → 标准化 list[dict]（traded_amount 等字段 float 防 None/NaN）。"""
    gw = _make_gw_with_fake_loop(monkeypatch)
    gw._trader = _FakeTraderOrders(None, [_FakeTrade()])
    gw._account = object()
    gw._connected = True
    result = asyncio.run(gw.query_trades())
    assert len(result) == 1
    t = result[0]
    assert t["order_id"] == 100
    assert t["stock_code"] == "600000.SH"
    assert t["traded_volume"] == 1000
    assert t["traded_amount"] == 10500.0


def test_query_trades_locked_returns_empty(monkeypatch):
    """网关锁定 → query_trades 返 []（与 query_orders 同口径防脏读）。"""
    gw = _make_gw_with_fake_loop(monkeypatch)
    gw._trader = _FakeTraderOrders(None, [_FakeTrade()])
    gw._account = object()
    gw._connected = True
    gw._lock_down = True
    assert asyncio.run(gw.query_trades()) == []


# === T5: subscribe 失败惰性查询兜底 =============================================
# 场景：connect 时 subscribe 返 -1，连接本身成功（socket 通）但拿不到 on_stock_order
# 主推，订单状态进入「盲区」。对策：①connect 时标记 _main_push_available=False；
# ②上层 engine 在触发点前调 _sync_orders_if_stale 主动 query_orders 补全 _orders。
# 本组测试覆盖：connect 标记 / True no-op / False 同步 _orders。

def test_connect_subscribe_fail_marks_main_push_unavailable(monkeypatch):
    """subscribe 返 -1 → _main_push_available=False（不再只 warning）。

    Why 单列标志：subscribe 失败时 connect 仍可能成功（socket 通），但拿不到
    on_stock_order 主推，订单状态盲区——上层须靠 _sync_orders_if_stale 在触发点
    前主动 query_orders 补全 _orders。若仅 warning 不留标志位，上层无法区分
    「主推正常」与「主推不可用需兜底」，惰性同步会误触或漏触。
    """
    monkeypatch.setenv("QMT_USERDATA_PATH", "D:\\fake")
    monkeypatch.setenv("QMT_ACCOUNT_ID", "1000000365")
    # mock xtquant 可用 + connect/subscribe 行为
    monkeypatch.setattr(qmt_gateway, "_XTQUANT_AVAILABLE", True)

    class _FakeTrader:
        """start/connect/subscribe/register_callback 同步调用（connect 内部投线程池）。"""
        def start(self):  # 真实 start 同步阻塞，测试里直接 no-op
            pass
        def register_callback(self, cb):  # connect 必调；测试里无主推可不记
            pass
        def connect(self):  # 连接成功
            return 0
        def subscribe(self, account):  # 订阅失败：主推不可用
            return -1

    monkeypatch.setattr(qmt_gateway, "XtQuantTrader", lambda path, sid: _FakeTrader())
    monkeypatch.setattr(qmt_gateway, "StockAccount", lambda acc: object())
    gw = QmtExecutionGateway()
    asyncio.run(gw.connect())
    assert gw._main_push_available is False
    assert gw._connected is True  # 连接成功，只是主推不可用


def test_sync_orders_if_stale_calls_query_orders_when_unavailable(monkeypatch):
    """_main_push_available=False → _sync_orders_if_stale 调 query_orders 补 _orders。

    核心契约：query_orders 返回的 state 已是 OrderState 枚举（T4 fix），本方法
    直接透传 merge 进 _orders，不做类型转换——与 _process_order_update 写的 _orders
    结构兼容，circuit_breaker._TERMINAL（frozenset[OrderState]）判定安全。
    """
    gw = _make_gw_with_fake_loop(monkeypatch)
    gw._main_push_available = False
    gw._account = object()
    gw._connected = True
    called = {"query_orders": 0}

    async def fake_query_orders(cancelable_only=False):
        called["query_orders"] += 1
        # state 故意用 OrderState 枚举（对齐 T4 真实返值，非字符串）
        return [{"order_id": 100, "stock_code": "600000.SH",
                 "state": OrderState.FILLED, "order_status": 56,
                 "order_volume": 1000, "traded_volume": 1000,
                 "traded_price": 10.5, "price": 10.5, "status_msg": "",
                 "order_remark": "", "order_type": 23}]

    gw.query_orders = fake_query_orders
    n = asyncio.run(gw._sync_orders_if_stale())
    assert called["query_orders"] == 1
    assert n == 1
    assert gw._orders.get("100") is not None  # 同步进 _orders
    # state 透传，未做类型转换（仍是 OrderState 枚举）
    assert gw._orders["100"]["state"] == OrderState.FILLED


def test_sync_orders_if_stale_noop_when_push_available(monkeypatch):
    """_main_push_available=True → 不查（主推正常，无需兜底）。

    Why no-op：主推正常时 _orders 已被 on_stock_order 回调实时推进，主动查询
    只会增加柜台无谓负担（可能撞限频）；惰性同步仅在「主推不可用」时触发。
    """
    gw = _make_gw_with_fake_loop(monkeypatch)
    gw._main_push_available = True
    called = {"query_orders": 0}

    async def fake_query_orders(cancelable_only=False):
        called["query_orders"] += 1
        return []

    gw.query_orders = fake_query_orders
    n = asyncio.run(gw._sync_orders_if_stale())
    assert called["query_orders"] == 0
    assert n == 0  # no-op 返 0
