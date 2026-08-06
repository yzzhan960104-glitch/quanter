# -*- coding: utf-8 -*-
"""QmtExecutionGateway 补全单测（on_account_status / query_asset / query_orders / 兜底 / polish / 持仓扩展）。"""
import asyncio
import pytest

# Layer2 阶段3：真身迁 broker.qmt（原 trading.qmt_gateway）。
# patch 内部全局（_alert_account_status/_XTQUANT_AVAILABLE/XtQuantTrader/xtconstant/
# _QMT_* 字面量等）须指真身模块，trading.qmt_gateway 垫片的 re-export 副本与真身
# 非同一对象，patch 垫片无效。
from broker import qmt as qmt_gateway
from trading.types.order_state import OrderState  # Layer2 follow-up #4c：改指 types 真身
from broker.qmt import QmtExecutionGateway


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


# =========================================================================
# 2026-08-03 根治：connect -1 自锁（stop-before-recreate + 残留清理重试）
# =========================================================================
# 系统性实验结论：
#   1) 同 sid 的 XtQuantTrader 未 stop 即被替换/进程退出 → 会话残留共享内存，
#      此后同 sid connect 恒返 -1（含本进程重试自锁）；
#   2) 干净 stop() 后同 sid 可复用（rc=0）；
#   3) 进程强杀（不调 stop）后，新进程同 sid connect 必返 -1，清队列文件即恢复。
# 本组用例锁定修复契约：重连前必 stop 旧实例；-1 时清本 sid 残留文件重试一次。

class _FakeTraderReconnect:
    """可记录 start/stop/connect 的假 trader（connect 结果可编排）。"""
    instances: list = []
    results: list = []          # 跨实例共享的结果序列（每次 connect 取队首）

    def __init__(self):
        self.stopped = False
        _FakeTraderReconnect.instances.append(self)

    def register_callback(self, cb):
        pass

    def start(self):
        pass

    def stop(self):
        self.stopped = True

    def connect(self):
        if _FakeTraderReconnect.results:
            return _FakeTraderReconnect.results.pop(0)
        return 0

    def subscribe(self, account):
        return 0


def _install_reconnect_fake(monkeypatch, results):
    _FakeTraderReconnect.instances = []
    _FakeTraderReconnect.results = list(results)
    monkeypatch.setenv("QMT_USERDATA_PATH", "D:\\fake")
    monkeypatch.setenv("QMT_ACCOUNT_ID", "1000000365")
    monkeypatch.setattr(qmt_gateway, "_XTQUANT_AVAILABLE", True)
    monkeypatch.setattr(qmt_gateway, "XtQuantTrader",
                        lambda path, sid: _FakeTraderReconnect())
    monkeypatch.setattr(qmt_gateway, "StockAccount", lambda acc: object())
    return _FakeTraderReconnect.instances


def test_connect_stops_previous_trader_before_recreate(monkeypatch):
    """根治契约①：重连前必须 stop 旧实例（旧实例未停 = 同 sid 自锁 -1 的根因）。"""
    instances = _install_reconnect_fake(monkeypatch, [0])
    gw = QmtExecutionGateway()
    asyncio.run(gw.connect())
    asyncio.run(gw.connect())
    assert len(instances) == 2
    assert instances[0].stopped is True    # 旧实例在创建新实例前已 stop
    assert instances[1].stopped is False   # 当前实例仍在使用
    assert gw._connected is True


def test_connect_minus_one_cleans_stale_session_and_retries(monkeypatch):
    """根治契约②：-1（死进程残留会话）→ 停本次实例 + 清本 sid 队列文件 → 重试成功。

    W1.3 后：_cleanup_session_files 现被调 2 次（前置 1 + -1 兜底 1），都是本 sid。
    """
    instances = _install_reconnect_fake(monkeypatch, [-1, 0])
    cleaned = []
    monkeypatch.setattr(
        qmt_gateway, "_cleanup_session_files",
        lambda path, sid: cleaned.append(sid) or ["down_queue_win_123458"])
    gw = QmtExecutionGateway()
    asyncio.run(gw.connect())
    assert gw._connected is True
    # W1.3 后清理被调 2 次（前置预防 + -1 兜底），都是本 sid
    assert cleaned.count(gw._session_id) == 2
    assert len(instances) == 2
    assert instances[0].stopped is True    # 失败实例已 stop 释放 sid
    assert instances[1].stopped is False


def test_connect_minus_one_twice_still_raises(monkeypatch):
    """L2：重试仍 -1 且 sid 轮换耗尽 → 抛 ConnectionError + 置锁，不无限重试。

    W1.3 后：_cleanup_session_files 被调 2 次（前置 1 + 首轮 -1 兜底 1，
    第二轮 -1 不再清直接 break）。L2 轮换被 mock 为耗尽（返 None）→ 走 L3 失败路径。
    """
    instances = _install_reconnect_fake(monkeypatch, [-1, -1])
    cleaned = []
    monkeypatch.setattr(qmt_gateway, "_cleanup_session_files",
                        lambda p, s: cleaned.append(s) or [])

    async def _no_rotate(self):
        return None

    monkeypatch.setattr(QmtExecutionGateway, "_try_rotate_session", _no_rotate)
    gw = QmtExecutionGateway()
    with pytest.raises(ConnectionError, match="session"):
        asyncio.run(gw.connect())
    assert gw._lock_down is True
    # W1.3：前置 1 + 首轮 -1 兜底 1 = 2（第二轮 -1 不再清，直接 break 抛错）
    assert len(cleaned) == 2
    assert len(instances) == 2             # attempt 轮数锁定 2（不无限重试）
    assert all(t.stopped for t in instances)


def test_connect_rotates_sid_after_two_minus_one(monkeypatch):
    """L2：首选 sid -1 两轮 → 自动轮换未占用 sid 重连成功（自愈，不抛错）。"""
    instances = _install_reconnect_fake(monkeypatch, [-1, -1, 0])
    written = {}
    monkeypatch.setattr(qmt_gateway, "_write_runtime_session",
                        lambda p, a: written.update(preferred=p, actual=a))
    gw = QmtExecutionGateway()
    preferred = gw._session_id
    asyncio.run(gw.connect())
    assert gw._connected is True
    assert gw._session_id == preferred + 1   # preferred → +1 轮换
    assert written == {"preferred": preferred, "actual": preferred + 1}
    assert len(instances) == 3            # 首选 2 轮 + 轮换 1 次


def test_connect_cleans_session_files_before_first_attempt(monkeypatch, tmp_path):
    """W1.3：connect 进入 attempt 循环前预防性清队列，不等 -1 补救。

    Why 前置（08-04 根治）：旧 down_queue_win_{sid} 残留（事故机 75MB）会在连上
    瞬间被客户端重放，把历史成交回报再推一遍 → CSV 重复（W3 幂等是第二道防线，
    这里是第一道）。即便首次 connect 就返 0，残留队列也会被客户端在 subscribe
    之后立即推送——故必须在「构造 XtQuantTrader 之前」清掉。

    断言契约（双保险）：
    1. 顺序契约：_cleanup_session_files 必须在首次 XtQuantTrader(path, sid)
       构造之前被调一次（用全局序号计数器比对：cleanup_seq < first_trader_seq）。
    2. 物理契约：本 sid 残留 down_queue_win_{sid} 文件确实被删。
    3. 非兜底契约：本次 connect 首轮即返 0（不走 -1 补救分支），证明清理不是
       由 attempt==1/-1 兜底触发。
    """
    userdata = str(tmp_path / "userdata_mini")
    import os
    os.makedirs(userdata, exist_ok=True)
    sid = 123458
    # 构造残留会话队列（事故机同款命名）
    stale = os.path.join(userdata, f"down_queue_win_{sid}")
    open(stale, "w").write("stale 75MB payload")
    stale_lock = os.path.join(userdata, f"lock_down_queue_win_{sid}")
    open(stale_lock, "w").write("lock")

    # 序号计数器：记录 _cleanup_session_files 与 XtQuantTrader 构造的全局先后
    counter = {"seq": 0, "cleanup_at": None, "first_trader_at": None}

    def fake_xttrader(path, sess):
        if counter["first_trader_at"] is None:
            counter["first_trader_at"] = counter["seq"]
            counter["seq"] += 1
        return _FakeTraderReconnect()

    monkeypatch.setattr(qmt_gateway, "_XTQUANT_AVAILABLE", True)
    _FakeTraderReconnect.instances = []
    _FakeTraderReconnect.results = [0]   # 首轮即返 0（证明不走 -1 补救）
    monkeypatch.setattr(qmt_gateway, "XtQuantTrader", fake_xttrader)
    monkeypatch.setattr(qmt_gateway, "StockAccount", lambda acc: object())

    real_cleanup = qmt_gateway._cleanup_session_files
    def spy_cleanup(path, session_id):
        if counter["cleanup_at"] is None:
            counter["cleanup_at"] = counter["seq"]
            counter["seq"] += 1
        return real_cleanup(path, session_id)
    monkeypatch.setattr(qmt_gateway, "_cleanup_session_files", spy_cleanup)

    # 显式传 userdata/session_id，与残留文件 sid 严格对齐（不用 env 默认 123456）
    gw = QmtExecutionGateway(userdata_path=userdata, account_id="t", session_id=sid)
    asyncio.run(gw.connect())

    # 契约 1：清理发生在首次 XtQuantTrader 构造之前
    assert counter["cleanup_at"] is not None, "前置清理未被调用"
    assert counter["first_trader_at"] is not None, "未构造 XtQuantTrader"
    assert counter["cleanup_at"] < counter["first_trader_at"], (
        f"前置清理(seq={counter['cleanup_at']}) 未在首次 XtQuantTrader 构造"
        f"(seq={counter['first_trader_at']}) 之前——防旧队列重放失效")
    # 契约 2：本 sid 残留文件确实被删
    assert not os.path.exists(stale), "残留 down_queue_win_{sid} 未被前置清理删除"
    assert not os.path.exists(stale_lock), "残留 lock_down_queue_win_{sid} 未被前置清理删除"
    # 契约 3：本次 connect 首轮即成功（-1 补救分支未触发，证明清理是前置而非兜底）
    assert len(_FakeTraderReconnect.instances) == 1, (
        "首轮未成功（触发了 -1 重试），无法证明清理是前置预防而非 -1 补救")
    assert gw._connected is True


def test_connect_pre_cleanup_tolerates_exception(monkeypatch, tmp_path):
    """W1.3 软降级：前置清理异常（如文件被占用）不阻断 connect，仅 warning。

    Why 软降级：清理是「防患于未然」的第一道防线，但不是 connect 的必要条件——
    即便残留没清掉，connect 仍可能成功（柜台侧未必每次都重放）；若清理异常
    直接抛出，反而把「可恢复的文件系统问题」升级成「connect 全失败」，
    违反 fail-safe 原则。attempt 循环内的 -1 兜底是第二道防线。
    """
    userdata = str(tmp_path / "ud")
    import os
    os.makedirs(userdata, exist_ok=True)

    monkeypatch.setattr(qmt_gateway, "_XTQUANT_AVAILABLE", True)
    _FakeTraderReconnect.instances = []
    _FakeTraderReconnect.results = [0]
    monkeypatch.setattr(qmt_gateway, "XtQuantTrader",
                        lambda path, sid: _FakeTraderReconnect())
    monkeypatch.setattr(qmt_gateway, "StockAccount", lambda acc: object())
    # 前置清理抛异常（模拟文件被占用/OSError）——connect 必须吞掉继续
    def boom(path, sid):
        raise OSError("文件被其他进程占用")
    monkeypatch.setattr(qmt_gateway, "_cleanup_session_files", boom)

    gw = QmtExecutionGateway(userdata_path=userdata, account_id="t", session_id=123458)
    asyncio.run(gw.connect())   # 不应抛
    assert gw._connected is True


def test_cleanup_session_files_only_removes_own_sid(tmp_path):
    """残留清理只删本 sid 队列文件，绝不误删客户端/其他 sid 队列。"""
    d = tmp_path / "ud"
    d.mkdir()
    for name in ("down_queue_win_123458", "down_queue_win_123458__mutex",
                 "lock_down_queue_win_123458", "down_queue_win_123459",
                 "lock_down_queue_win_123456", "down_queue_xtmodel-0",
                 "up_queue_xtquant", "down_queue_xtmodel-0__mutex"):
        (d / name).write_bytes(b"x")

    removed = qmt_gateway._cleanup_session_files(str(d), 123458)
    assert sorted(removed) == ["down_queue_win_123458",
                               "down_queue_win_123458__mutex",
                               "lock_down_queue_win_123458"]
    remain = sorted(p.name for p in d.iterdir())
    assert remain == ["down_queue_win_123459", "down_queue_xtmodel-0",
                      "down_queue_xtmodel-0__mutex", "lock_down_queue_win_123456",
                      "up_queue_xtquant"]


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


# =============================================================================
# T6: _assert_status_contract 补全 11 态 + ACC 校验 + cancel_order 非终态 message
# =============================================================================

def test_assert_status_contract_validates_all_11_order_states(monkeypatch):
    """_assert_status_contract 应覆盖 11 个 order 状态字面量（不止现有 7 个）。

    Why 必要：T1/T2 后 order 状态扩展到 11 个（+PARTSUCC_CANCEL=52/REPORTED_CANCEL=51
    /WAIT_REPORTING=49/UNKNOWN=255），但现有契约只校验 7 个——缺校验的 4 个一旦在
    xtquant 升级中漂移，_map_qmt_status 会静默错判状态（致命）。本用例构造假
    xtconstant 全 11 态一致 → 通过；再故意漂移 PART_CANCEL → fail-fast RuntimeError。
    """
    monkeypatch.setattr(qmt_gateway, "_XTQUANT_AVAILABLE", True)

    class _FakeXtconst:
        # 全 11 态与模块字面量一致
        ORDER_JUNK = qmt_gateway._QMT_ORDER_JUNK
        ORDER_SUCCEEDED = qmt_gateway._QMT_ORDER_SUCCEEDED
        ORDER_PART_SUCC = qmt_gateway._QMT_ORDER_PART_SUCC
        ORDER_CANCELED = qmt_gateway._QMT_ORDER_CANCELED
        ORDER_PART_CANCEL = qmt_gateway._QMT_ORDER_PART_CANCEL
        ORDER_PARTSUCC_CANCEL = qmt_gateway._QMT_ORDER_PARTSUCC_CANCEL
        ORDER_REPORTED_CANCEL = qmt_gateway._QMT_ORDER_REPORTED_CANCEL
        ORDER_REPORTED = qmt_gateway._QMT_ORDER_REPORTED
        ORDER_WAIT_REPORTING = qmt_gateway._QMT_ORDER_WAIT_REPORTING
        ORDER_UNREPORTED = qmt_gateway._QMT_ORDER_UNREPORTED
        ORDER_UNKNOWN = qmt_gateway._QMT_ORDER_UNKNOWN
        # ACC 态也需齐备（ACC 校验同一调用，否则 AttributeError）
        ACCOUNT_STATUS_INVALID = qmt_gateway._QMT_ACC_INVALID
        ACCOUNT_STATUS_OK = qmt_gateway._QMT_ACC_OK
        ACCOUNT_STATUS_WAITING_LOGIN = qmt_gateway._QMT_ACC_WAITING_LOGIN
        ACCOUNT_STATUSING = qmt_gateway._QMT_ACC_LOGINING  # 注意 STATUSING 命名差异
        ACCOUNT_STATUS_FAIL = qmt_gateway._QMT_ACC_FAIL
        ACCOUNT_STATUS_INITING = qmt_gateway._QMT_ACC_INITING
        ACCOUNT_STATUS_CORRECTING = qmt_gateway._QMT_ACC_CORRECTING
        ACCOUNT_STATUS_CLOSED = qmt_gateway._QMT_ACC_CLOSED
        ACCOUNT_STATUS_ASSIS_FAIL = qmt_gateway._QMT_ACC_ASSIS_FAIL
        ACCOUNT_STATUS_DISABLEBYSYS = qmt_gateway._QMT_ACC_DISABLE_BYSYS  # 无下划线 BYSYS
        ACCOUNT_STATUS_DISABLEBYUSER = qmt_gateway._QMT_ACC_DISABLE_BYUSER

    monkeypatch.setattr(qmt_gateway, "xtconstant", _FakeXtconst)
    # 全 11 态一致 → 不抛
    qmt_gateway._assert_status_contract()
    # 故意漂移 ORDER_PART_CANCEL → fail-fast
    _FakeXtconst.ORDER_PART_CANCEL = 999
    with pytest.raises(RuntimeError, match="xtconstant 枚举契约漂移"):
        qmt_gateway._assert_status_contract()


def test_assert_status_contract_validates_all_11_account_states(monkeypatch):
    """_assert_status_contract 应同步校验 11 个 ACCOUNT_STATUS 字面量（T1 新增防漂移）。

    Why 必要：T1 新增 _QMT_ACC_* 10+1=11 个字面量，与 order 状态同受版本漂移风险
    （账号状态若错乱，on_account_status 会误锁/漏锁网关，直接影响熔断与发单）。
    本用例构造假 xtconstant 全 11 态 ACC 一致 → 通过；再漂移 DISABLEBYSYS → fail-fast。
    重点覆盖命名差异：ACCOUNT_STATUSING（非 LOGIN）/ DISABLEBYSYS（非 DISABLE_BYSYS）。
    """
    monkeypatch.setattr(qmt_gateway, "_XTQUANT_AVAILABLE", True)

    class _FakeXtconst:
        # order 态齐备（同调用流程，防 AttributeError）
        ORDER_JUNK = qmt_gateway._QMT_ORDER_JUNK
        ORDER_SUCCEEDED = qmt_gateway._QMT_ORDER_SUCCEEDED
        ORDER_PART_SUCC = qmt_gateway._QMT_ORDER_PART_SUCC
        ORDER_CANCELED = qmt_gateway._QMT_ORDER_CANCELED
        ORDER_PART_CANCEL = qmt_gateway._QMT_ORDER_PART_CANCEL
        ORDER_PARTSUCC_CANCEL = qmt_gateway._QMT_ORDER_PARTSUCC_CANCEL
        ORDER_REPORTED_CANCEL = qmt_gateway._QMT_ORDER_REPORTED_CANCEL
        ORDER_REPORTED = qmt_gateway._QMT_ORDER_REPORTED
        ORDER_WAIT_REPORTING = qmt_gateway._QMT_ORDER_WAIT_REPORTING
        ORDER_UNREPORTED = qmt_gateway._QMT_ORDER_UNREPORTED
        ORDER_UNKNOWN = qmt_gateway._QMT_ORDER_UNKNOWN
        # 全 11 ACC 态与模块字面量一致（注意命名差异：STATUSING/DISABLEBYSYS）
        ACCOUNT_STATUS_INVALID = qmt_gateway._QMT_ACC_INVALID
        ACCOUNT_STATUS_OK = qmt_gateway._QMT_ACC_OK
        ACCOUNT_STATUS_WAITING_LOGIN = qmt_gateway._QMT_ACC_WAITING_LOGIN
        ACCOUNT_STATUSING = qmt_gateway._QMT_ACC_LOGINING
        ACCOUNT_STATUS_FAIL = qmt_gateway._QMT_ACC_FAIL
        ACCOUNT_STATUS_INITING = qmt_gateway._QMT_ACC_INITING
        ACCOUNT_STATUS_CORRECTING = qmt_gateway._QMT_ACC_CORRECTING
        ACCOUNT_STATUS_CLOSED = qmt_gateway._QMT_ACC_CLOSED
        ACCOUNT_STATUS_ASSIS_FAIL = qmt_gateway._QMT_ACC_ASSIS_FAIL
        ACCOUNT_STATUS_DISABLEBYSYS = qmt_gateway._QMT_ACC_DISABLE_BYSYS
        ACCOUNT_STATUS_DISABLEBYUSER = qmt_gateway._QMT_ACC_DISABLE_BYUSER

    monkeypatch.setattr(qmt_gateway, "xtconstant", _FakeXtconst)
    # 全 11 ACC 态一致 → 不抛
    qmt_gateway._assert_status_contract()
    # 故意漂移 ACCOUNT_STATUS_DISABLEBYSYS → fail-fast（T1 新增 fatal 状态，漂移最危险）
    _FakeXtconst.ACCOUNT_STATUS_DISABLEBYSYS = 999
    with pytest.raises(RuntimeError, match="xtconstant 枚举契约漂移"):
        qmt_gateway._assert_status_contract()


def test_cancel_order_message_marks_non_terminal(monkeypatch):
    """cancel_order rc==0 的 message 应明示「最终态以 on_stock_order 推送 CANCELLED 为准」。

    Why 必要：rc==0 仅表「撤单指令已成功发出」，非订单终态——柜台可能因订单已成交 /
    已撤而撤单失败，最终态由 on_stock_order 回调推送 CANCELLED 才算数。原 message
    「等待回报确认」语义含糊，上层易误读为「撤单已成功」。新 message 显式标注非终态
    + 推送锚点，杜绝误读。
    """
    gw = _make_gw_with_fake_loop(monkeypatch)
    gw._connected = True
    gw._lock_down = False
    gw._account = object()

    class _FakeTrader:
        def cancel_order_stock(self, account, oid):
            return 0  # 撤单指令成功发出（非终态）

    gw._trader = _FakeTrader()
    gw._seq_to_real = {100: 999}  # seq(int)→真实 order_id 映射齐备（_seq_to_real 键为 int）
    result = asyncio.run(gw.cancel_order("100"))
    # rc==0 仍维持 CANCELLED state（主链路不变，只改 message 文案）
    assert result.state.name == "CANCELLED"
    # message 明示非终态 + 推送锚点 on_stock_order
    assert "on_stock_order" in result.message
    assert "非终态" in result.message


# =============================================================================
# T7: _fetch_broker_positions 扩展返回结构（成本价/开仓价/昨夜股）
# ============================================================================

class _FakePosition:
    """模拟 XtPosition（xttrader.md「持仓查询」返回结构）。

    字段对齐 xtquant.xttype.XtPosition：stock_code/volume/can_use_volume/open_price/
    avg_price/market_value/frozen_volume/on_road_volume/yesterday_volume。
    can_use_volume==0 表示 T+1 冻结 / 废弃仓，_fetch_broker_positions 须过滤。
    """
    def __init__(self, stock_code, volume, can_use, avg_price, open_price, yesterday):
        self.stock_code = stock_code
        self.volume = volume
        self.can_use_volume = can_use
        self.avg_price = avg_price
        self.open_price = open_price
        self.yesterday_volume = yesterday


class _FakeTraderPositions:
    """模拟 self._trader，query_stock_positions 返注入的持仓列表。"""
    def __init__(self, positions):
        self._positions = positions

    def query_stock_positions(self, account):
        # 忽略 account（测试仅断言返值结构）
        return self._positions


def test_fetch_broker_positions_returns_extended_dict(monkeypatch):
    """返 {sym: {volume, avg_price, open_price, yesterday_volume}}（扩展字段）。

    Why 扩展：成本价/开仓价供浮盈对账增强，昨夜股供 T+1 判断强化——二期对账增强
    需要这些字段；原契约只返 volume 不够用（断口：浮盈计算无成本价 = 只能量敞口
    不能量盈亏）。返 dict-of-dict 是破坏性变更，所有消费者需迁移（见 sync_positions
    扁平化 + stop_loss_monitor qty 读取 + trading_service.get_positions）。
    """
    gw = _make_gw_with_fake_loop(monkeypatch)
    gw._trader = _FakeTraderPositions([
        _FakePosition("600000.SH", 1000, 1000, 10.0, 10.0, 1000),  # 可卖
        _FakePosition("000001.SZ", 500, 0, 15.0, 15.0, 0),         # T+1 冻结（过滤）
    ])
    gw._account = object()
    gw._connected = True
    result = asyncio.run(gw._fetch_broker_positions())
    # can_use_volume==0 过滤（口径不变），只剩 600000.SH
    assert "000001.SZ" not in result
    pos = result["600000.SH"]
    # 新契约：dict-of-dict（非 float）
    assert pos["volume"] == 1000
    assert pos["avg_price"] == 10.0
    assert pos["open_price"] == 10.0
    assert pos["yesterday_volume"] == 1000


def test_fetch_broker_positions_volume_is_primary(monkeypatch):
    """volume 仍是主可用量（can_use_volume==0 过滤不变）。

    Why 向后兼容断言：volume 是 sync_positions 扁平化 / stop_loss qty 读取的主键，
    扩展结构后必须保证 volume 仍可正确读到（破坏性变更不影响主对账/止损链路）。
    """
    gw = _make_gw_with_fake_loop(monkeypatch)
    gw._trader = _FakeTraderPositions([
        _FakePosition("600000.SH", 2000, 2000, 10.0, 10.0, 2000),
    ])
    gw._account = object()
    gw._connected = True
    result = asyncio.run(gw._fetch_broker_positions())
    # volume 子键仍是主可用量（消费者扁平化读这个键）
    assert result["600000.SH"]["volume"] == 2000


def test_fetch_broker_positions_full_includes_frozen(monkeypatch):
    """tradable_only=False → 全量持仓（含 can_use==0 的 T+1 冻结仓）。

    Why 固化：展示(get_positions)/对账(sync_positions)用全量口径看真实敞口；
    历史 bug 是这两处复用过滤口径，致 T+1 真实敞口被藏（研究员看「空仓」实则有
    T+1 仓）+ drift 失真。本测试钉死「默认过滤 / 全量含冻结」双口径契约，防回归。
    """
    gw = _make_gw_with_fake_loop(monkeypatch)
    gw._trader = _FakeTraderPositions([
        _FakePosition("600000.SH", 1000, 1000, 10.0, 10.0, 1000),  # 可卖
        _FakePosition("000001.SZ", 500, 0, 15.0, 15.0, 0),         # T+1 冻结
    ])
    gw._account = object()
    gw._connected = True
    # 默认 True（stop_loss 口径，向后兼容）：过滤 T+1 冻结仓
    tradable = asyncio.run(gw._fetch_broker_positions())
    assert set(tradable.keys()) == {"600000.SH"}
    # tradable_only=False（展示/对账口径）：全量，T+1 冻结仓保留
    full = asyncio.run(gw._fetch_broker_positions(tradable_only=False))
    assert set(full.keys()) == {"600000.SH", "000001.SZ"}
    assert full["000001.SZ"]["volume"] == 500   # T+1 冻结仓的真实敞口可见
