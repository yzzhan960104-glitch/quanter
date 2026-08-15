# -*- coding: utf-8 -*-
"""W2-H2 回调体 Ports 化契约测试：handle_order_update 副作用依赖经 ports 显式注入。

物理意图（master design §5.2 · W2-H2）：
    T1 Task 3 把 broker 订单回调三分支迁 trading.order_state 后，free function 仍以
    engine 实例为依赖载体（副作用依赖散在函数体：state_store 落账 + ``engine._gw``
    网关反查）。本波把「副作用依赖」收敛为显式 ``ports`` 参数：

    - ``ports.state_store`` —— fill/position/trade_event 落账唯一通道。**模块对象风格**
      （default_factory 绑 ``trading.state_store`` 模块对象本身，而非其函数属性）：
      函数体 ``ports.state_store.insert_fill(...)`` 调用时才读模块属性 →
      ``patch("trading.state_store.insert_fill")`` 仍命中，monkeypatch 语义与原顶部
      直接 import 完全等价（W1-B gateway lazy 顶部化同款范式）。
    - ``ports.gateway`` —— 网关句柄（原 ``engine._gw`` 反查口；engine 侧薄 wrapper
      调用时快照对齐）。T13（W2-H1 broker 分层）前的过渡可见性锚点。

契约断言（区别于 test_engine_order_update_handler.py 的业务断言——那套走 engine 薄
wrapper + 真 state_store；本文件走 fake ports 直调 free function，验证**依赖方向**）：
    1) fake ports（SimpleNamespace 风格 + 记录列表）注入 → insert_fill /
       apply_fill_to_position / insert_trade_event / has_order 全部落在 fake 上
       （副作用经 ports 走，而非函数体直接模块引用）；
    2) 真模块 ``trading.state_store.insert_fill`` 被 patch 成 boom——若函数体仍直接读
       模块引用即爆（反证 ports 通道是唯一通道，防回退）；
    3) 重放（insert_fill 返 False）→ apply_fill_to_position / FILLED 事件 / 钉钉全跳过
       （08-04「1 笔成交记 24 次」幂等红线在 ports 化后逐行保形）；
    4) EnginePorts 默认装配：不传 state_store 的既有构造（engine :413 与全部测试）
       自动绑真模块对象，且 patch 真模块属性经 ports 属性访问仍命中。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from trading import order_state, state_store
from trading.ports import EnginePorts
from trading.types.order_state import OrderState


class _RecordingStore:
    """记录式 fake state_store：按名记录每次调用 + 可编排返回值。

    口子清单与真身签名对齐（只覆盖回调三分支实际触达的 8 个）：
    get_order_by_broker_oid / build_trade_id / get_account / upsert_account /
    insert_trade_event / insert_fill / apply_fill_to_position / has_order /
    update_order_state_by_broker_oid。
    """

    def __init__(self, insert_fill_ret: bool = True, has_order_ret: bool = False):
        self.calls: list[tuple] = []
        self._insert_fill_ret = insert_fill_ret
        self._has_order_ret = has_order_ret

    def get_order_by_broker_oid(self, oid):
        # "777" 预置 side=buy 行（order_direction DB 反查路径）；其余 miss 走内存兜底。
        self.calls.append(("get_order_by_broker_oid", oid))
        if str(oid) == "777":
            return {"side": "buy", "broker_oid": "777", "state": "SUBMITTED",
                    "symbol": "300001.SZ"}
        return None

    def build_trade_id(self, account_id, symbol, trade_date):
        self.calls.append(("build_trade_id", account_id, symbol, trade_date))
        return "TEST_TRADE_ID"

    def get_account(self, account_id):
        self.calls.append(("get_account", account_id))
        return {"account_id": account_id}  # 非 None → 不触发 upsert_account 兜底

    def upsert_account(self, account_id, broker="qmt"):
        self.calls.append(("upsert_account", account_id, broker))

    def insert_trade_event(self, account_id, trade_id, symbol, action, **kw):
        self.calls.append(("insert_trade_event", account_id, trade_id, symbol, action))

    def insert_fill(self, *args, **kw):
        # 返回值编排契约：True=首次入账（走镜像），False=UNIQUE 命中重放（全跳过）
        self.calls.append(("insert_fill",) + args)
        return self._insert_fill_ret

    def apply_fill_to_position(self, *args, **kw):
        self.calls.append(("apply_fill_to_position",) + args)

    def has_order(self, account_id, trade_date, symbol, purpose):
        self.calls.append(("has_order", account_id, trade_date, symbol, purpose))
        return self._has_order_ret

    def update_order_state_by_broker_oid(self, *args, **kw):
        self.calls.append(("update_order_state_by_broker_oid",) + args)
        return 1


def _buy_trade_update() -> dict:
    """BUY 成交回报（on_stock_trade 推送契约：kind=trade + 量价齐全 + DB side=buy）。"""
    return {
        "kind": "trade",
        "order_id": "777",
        "stock_code": "300001.SZ",
        "traded_volume": 100,
        "traded_price": 10.5,
        "traded_amount": 1050.0,
        "traded_time": 20260815,
        "state": "FILLED",
    }


def _isolate_order_state_env(monkeypatch, tmp_path, store: _RecordingStore) -> None:
    """公共环境隔离：回调体外围依赖全部拦在测试边界内（不触真库/真归因/真止盈）。

    - 真模块 state_store.insert_fill / apply_fill_to_position patch 成 boom——契约反证
      守卫（函数体若直接模块引用即爆）；_DEFAULT_DB 指到 tmp（红期旧代码走真模块时
      也不触生产账本 logs/trading_state.db）。
    - account 反查走 order_state 本地绑定（W1-A/T2 迁移后的 patch 物理路径范式）。
    - 归因 / 止盈挂单 / 钉钉通知均为 BUY 成交链路的外围副作用，recorder/mock 拦截。
    """
    def _boom(*a, **k):
        raise AssertionError("副作用走了 trading.state_store 模块直连，而非 ports.state_store")

    monkeypatch.setattr(state_store, "insert_fill", _boom)
    monkeypatch.setattr(state_store, "apply_fill_to_position", _boom)
    monkeypatch.setattr(state_store, "_DEFAULT_DB", str(tmp_path / "guard.db"))
    monkeypatch.setattr(order_state, "_resolve_account_id", lambda: "ACC_TEST")
    monkeypatch.setattr("trading.gateway_service.record_position_attribution",
                        lambda *a, **k: None)
    monkeypatch.setattr("trading.phases.exit.place_take_profit", AsyncMock())


def test_trade_branch_side_effects_flow_through_ports(monkeypatch, tmp_path):
    """契约 1+2：trade 分支落账链 insert_fill → apply_fill_to_position → FILLED 事件
    → has_order(TP1) 全部经 ports.state_store 走（fake 记录到 + 真模块 boom 反证）。"""
    _isolate_order_state_env(monkeypatch, tmp_path, None)
    store = _RecordingStore(insert_fill_ret=True)
    ports = SimpleNamespace(state_store=store, gateway=None)  # fake ports（无 engine 载体）
    notify_mgr = MagicMock()
    notify_mgr.notify_trade_event = AsyncMock(return_value=[])
    with patch("infra.notifier.NotificationManager") as NM:
        NM.get_default.return_value = notify_mgr
        asyncio.run(order_state.handle_order_update(ports, _buy_trade_update()))
    names = [c[0] for c in store.calls]
    # 三口子全在 fake 上（缺一即函数体仍走模块直连）
    assert "insert_fill" in names, "insert_fill 未经 ports.state_store"
    assert "apply_fill_to_position" in names, "apply_fill_to_position 未经 ports.state_store"
    assert "insert_trade_event" in names, "insert_trade_event(FILLED) 未经 ports.state_store"
    assert "has_order" in names, "has_order(TP1) 幂等查询未经 ports.state_store"
    # 落账顺序契约（08-04 红线）：先真相源 insert_fill，再 position 累加，再 FILLED 事件，
    # 最后 TP 幂等查询——顺序错位即账账不符窗口。
    assert names.index("insert_fill") < names.index("apply_fill_to_position"), \
        "apply_fill_to_position 必须在 insert_fill（真相源）之后"
    assert names.index("apply_fill_to_position") < names.index("has_order")
    # account 经 ports 链路透传（insert_fill 第 2 位置参数）
    fill_args = next(c for c in store.calls if c[0] == "insert_fill")
    assert fill_args[2] == "ACC_TEST", "account_id 未经回调链路透传到 fill 落账"
    # BUY + has_order=False → 挂止盈经 lazy import patch 点被调
    import trading.phases.exit as _exit
    _exit.place_take_profit.assert_awaited_once()
    # 钉钉成交通知与 fill 真相源同判定点（首次才推）
    notify_mgr.notify_trade_event.assert_called_once()


def test_replay_skips_mirrors_via_ports(monkeypatch, tmp_path):
    """契约 3：重放（insert_fill 返 False）→ position/FILLED 事件/钉钉全跳过（幂等红线保形）。"""
    _isolate_order_state_env(monkeypatch, tmp_path, None)
    # has_order=True 对齐真实重放态：首次通过后 TP1 已在库（TP 幂等独立于 fill 幂等）
    store = _RecordingStore(insert_fill_ret=False, has_order_ret=True)
    ports = SimpleNamespace(state_store=store, gateway=None)
    notify_mgr = MagicMock()
    notify_mgr.notify_trade_event = AsyncMock(return_value=[])
    with patch("infra.notifier.NotificationManager") as NM:
        NM.get_default.return_value = notify_mgr
        asyncio.run(order_state.handle_order_update(ports, _buy_trade_update()))
    names = [c[0] for c in store.calls]
    assert "insert_fill" in names, "重放仍应触达真相源判定（返 False 即重放语义）"
    assert "apply_fill_to_position" not in names, "重放不得重复累加 position（08-04 红线）"
    assert "insert_trade_event" not in names, "重放不得追加 FILLED 事件（事件流与 fill 表 1:1）"
    notify_mgr.notify_trade_event.assert_not_called(), "重放不得重复推钉钉（轰炸根因）"
    import trading.phases.exit as _exit
    _exit.place_take_profit.assert_not_awaited(), "TP 已挂（has_order=True）不得重挂（超卖红线）"


def test_async_response_branch_writes_via_ports(monkeypatch, tmp_path):
    """契约 1（async_response 分支）：seq→real 回填 broker_oid 经 ports.state_store 走。"""
    _isolate_order_state_env(monkeypatch, tmp_path, None)
    store = _RecordingStore()
    ports = SimpleNamespace(state_store=store, gateway=None)
    asyncio.run(order_state.handle_order_update(
        ports, {"kind": "async_response", "seq": 5, "order_id": 777}))
    assert [c[0] for c in store.calls] == ["update_order_state_by_broker_oid"], \
        "async_response 分支的 broker_oid 回填必须经 ports.state_store"


def test_order_branch_advances_via_ports(monkeypatch, tmp_path):
    """契约 1（order 分支）：柜台状态推进（get → update_order_state_by_broker_oid）经 ports 走。"""
    _isolate_order_state_env(monkeypatch, tmp_path, None)
    store = _RecordingStore()
    ports = SimpleNamespace(state_store=store, gateway=None)
    order_state.advance_order_state_from_status(
        ports, {"kind": "order", "order_id": "777", "state": OrderState.FILLED,
                "traded_volume": 100, "traded_price": 10.5})
    names = [c[0] for c in store.calls]
    assert names.count("get_order_by_broker_oid") == 1
    assert "update_order_state_by_broker_oid" in names, \
        "order 分支的状态推进写必须经 ports.state_store"


def test_order_direction_reads_gateway_via_ports():
    """契约 1（网关依赖）：方向反查经 ports.gateway（DB miss → 内存 _orders 兜底）。"""
    store = _RecordingStore()  # "555" 非 "777" → DB miss → 走内存兜底
    gw = SimpleNamespace(_orders={"555": {"order_type": 23}})  # 23=STOCK_BUY
    ports = SimpleNamespace(state_store=store, gateway=gw)
    assert order_state.order_direction(ports, "555") == "BUY"
    # 网关缺位（None）不爆：dry_run 影子模式回调链路 gw=None 的兜底语义保形
    ports_none = SimpleNamespace(state_store=store, gateway=None)
    assert order_state.order_direction(ports_none, "555") is None


def test_default_ports_state_store_is_module_and_patch_hits(monkeypatch):
    """契约 4：EnginePorts 默认装配绑真模块对象——生产 engine 不传该字段走真身，
    且 patch 真模块属性经 ports 属性访问仍命中（monkeypatch 语义零变更守卫）。"""
    ports = EnginePorts(
        gate=AsyncMock(),
        whitelist_add=lambda syms: None,
        whitelist_clear=lambda: None,
    )
    # default_factory 绑「模块对象」本身（身份同一），而非 import 时拷贝函数引用
    assert ports.state_store is state_store, \
        "默认 state_store 必须是 trading.state_store 模块对象（保 patch 语义）"
    assert ports.gateway is None, "网关默认 None（__init__ 时未装配，运行时快照对齐）"
    # 调用时属性访问读模块属性 → patch 真模块函数后经 ports 通道拿到 mock
    monkeypatch.setattr(state_store, "insert_fill", lambda *a, **k: "PATCHED")
    assert ports.state_store.insert_fill(None, None, None, None, None, 0, 0.0) == "PATCHED", \
        "patch('trading.state_store.insert_fill') 必须经 ports.state_store 命中"
