# -*- coding: utf-8 -*-
"""引擎编排单测（Task 9 · 核心调度逻辑，不真起 APScheduler）。

测试边界（控制器 scope #5）：
- 绝不真起 APScheduler（plan 红线）——只测 4 个 async 触发函数 + TradingEngine
  的 cron 注册（实例化即装配 4 job，不 start）；
- 绝不真发钉钉 / 真下单：monkeypatch ``trading_plan.push_plan_to_dingtalk``
  （网络副作用）+ ``engine._submit``（真单副作用）；
- stop_loss qty 不得硬编码（live 安全红线）：monkeypatch gw._fetch_broker_positions
  返回真实持仓 dict，断言卖出 qty 源自该 dict 而非魔法数 100。
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime

import pytest

from trading import engine, trading_plan


# ----------------------------------------------------------------------------
# 公共 fixture：每个 case 独立 TRADE_PLAN_DIR（防交叉污染），dry_run 默认。
# ----------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _isolate_plan_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("TRADE_PLAN_DIR", str(tmp_path / "plans"))
    monkeypatch.setenv("AUTO_TRADE_MODE", "dry_run")  # 影子模式默认，防测试真下单
    # state_store 表初始化（state-store-redesign 后 _handle_order_update 等查 state_store，
    # 无表 → no such table。autouse 确保所有 test 都有 state_store 可查）
    from trading import state_store, position_book
    _db = str(tmp_path / "engine_state.db")
    monkeypatch.setattr(position_book, "_DEFAULT_DB", _db)
    monkeypatch.setattr(state_store, "_DEFAULT_DB", _db)
    position_book.init_db()
    state_store.init_store()
    # Task 8（C-2 S3）：重置模块级 _ACTIVE_ENGINE 单例——pre_open 入口的 gate 经它调用
    # 实例方法，若不重置会跨测试泄漏（前序构造 TradingEngine 的测试残留），让本文件里
    # 非 gate 焦点的 pre_open 测试（expired_positions / alerts）被 gate 拦截早返。
    # 重置为 None 后 pre_open 走防御性分支跳过 gate，保留这些测试原本验证的下游逻辑。
    monkeypatch.setattr(engine, "_ACTIVE_ENGINE", None)


# ============================================================================
# 1. eod_plan：影子模式不真下单，落嵌套 orders，推钉钉被 monkeypatch 拦截
# ============================================================================
def test_eod_plan_dry_run_no_real_order(monkeypatch):
    """影子模式 eod_plan：空信号 → 不挂单、不真发钉钉、落盘 confirmed=False。"""
    # 防真单：_submit 若被调即抛（本 case 信号为空，不应触达）
    async def _no_submit(order, **kw):
        raise AssertionError("影子模式 eod_plan 绝不应调 _submit（计划阶段本就不下单）")

    monkeypatch.setattr(engine, "_submit", _no_submit)

    # 防真发钉钉（scope #5）：monkeypatch trading_plan.push_plan_to_dingtalk
    pushed = {"n": 0}

    def _fake_push(date, orders, **kw):
        pushed["n"] += 1
        pushed["orders"] = orders
        return True

    monkeypatch.setattr(trading_plan, "push_plan_to_dingtalk", _fake_push)

    result = asyncio.run(
        engine.eod_plan("2099-01-01", signals=[], atr_map={}, capital=1_000_000)
    )

    assert result["n_orders"] == 0
    assert result["mode"] == "dry_run"
    assert pushed["n"] == 1            # 调了一次 push（mock 拦截，未真发）
    # 落盘计划应是 confirmed=False（待人工确认）
    plan = trading_plan.load_plan("2099-01-01")
    assert plan is not None
    assert plan["confirmed"] is False


def test_eod_plan_produces_nested_orders(monkeypatch):
    """scope #1：eod_plan 生产的 orders 必须是嵌套结构（与 Task8 push 一致）。"""
    from strategies.neckline.signal import Signal

    monkeypatch.setattr(engine, "_submit", _no_op_submit)
    monkeypatch.setattr(trading_plan, "push_plan_to_dingtalk", lambda d, o, **kw: True)

    # Layer2 阶段1：signals 改为 list[Signal]（frozen dataclass）
    signal = [Signal(
        symbol="600000.SH", entry_price=10.0,
        neckline=10.5, bottom=9.5,
    )]
    asyncio.run(
        engine.eod_plan("2099-01-01", signals=signal,
                        atr_map={"600000.SH": 0.2}, capital=1_000_000)
    )
    plan = trading_plan.load_plan("2099-01-01")
    assert plan and plan["orders"]
    o = plan["orders"][0]
    # 嵌套结构硬约束：order + stop_price + take_profit 三键齐全（Task8 契约）
    assert set(o.keys()) >= {"order", "stop_price", "take_profit"}
    assert set(o["order"].keys()) >= {"symbol", "qty", "side", "price"}


# ----------------------------------------------------------------------------
# T6（state-store-redesign）：eod_plan 落 trade_event(SIGNAL+CONFIRMED) + veto 保护
# ----------------------------------------------------------------------------
@pytest.fixture
def _state_db(tmp_path, monkeypatch):
    """隔离 state_store DB（eod_plan 落 trade_event 用）。"""
    from trading import state_store
    db_path = str(tmp_path / "state.db")
    monkeypatch.setattr(state_store, "_DEFAULT_DB", db_path)
    state_store.init_store()
    return db_path


def _seed_breaker_baseline(today: str, total_asset: float = 1_000_000.0) -> None:
    """预置日内熔断 start_equity 基线（W4+C-1 后统一从 account_daily 读）。

    物理（C-1 收口）：原 breaker 测试预置基线调 ``position_book.snapshot_start_equity``
    写 daily_equity 表——但 W4 后 pre_open 写口已迁 account_daily，post_close 熔断读口也已
    迁 ``state_store.get_start_equity`` 读 account_daily。基线必须与生产同口径（同表 + 同
    account_id），否则测试假 PASS（读到旧 daily_equity 或返 None 跳过熔断）。

    本 helper 用 ``engine._resolve_account_id()``（与 pre_open/post_close 同口径）+ upsert
    default account（FK 满足）+ 写 account_daily.start，让熔断读口真正命中基线。
    """
    from trading import state_store
    account_id = engine._resolve_account_id()
    # upsert account：account_daily FK REFERENCES account(account_id)，无 account 行写快照
    # 会 IntegrityError（与 pre_open 生产路径同兜底，见 _ensure_account）。
    state_store.upsert_account(account_id, broker="qmt")
    state_store.snapshot_start_equity(account_id, today, total_asset)


def _signal_600000():
    from strategies.neckline.signal import Signal
    return [Signal(symbol="600000.SH", entry_price=10.0, neckline=10.5, bottom=9.5)]


def test_eod_plan_inserts_signal_event(monkeypatch, _state_db):
    """eod_plan 后 trade_event 有 SIGNAL 行（meta 含计划参数 stop_price/take_profit）。"""
    import sqlite3
    from trading import state_store
    monkeypatch.setattr(engine, "_submit", _no_op_submit)
    monkeypatch.setattr(trading_plan, "push_plan_to_dingtalk", lambda d, o, **kw: True)
    asyncio.run(engine.eod_plan("2099-01-01", signals=_signal_600000(),
                                atr_map={"600000.SH": 0.2}, capital=1_000_000))
    account_id = engine._resolve_account_id()  # 跟随 env，不硬编码默认账户
    plan_meta = state_store.get_trade_plan(f"{account_id}_600000.SH_2099-01-01")
    assert plan_meta is not None
    assert "stop_price" in plan_meta
    assert "take_profit" in plan_meta


def test_eod_plan_auto_confirm_event(monkeypatch, _state_db):
    """AUTO_CONFIRM_PLAN=true → trade_event 有 CONFIRMED 行。"""
    from trading import state_store
    monkeypatch.setattr(engine, "_submit", _no_op_submit)
    monkeypatch.setattr(trading_plan, "push_plan_to_dingtalk", lambda d, o, **kw: True)
    monkeypatch.setenv("AUTO_CONFIRM_PLAN", "true")
    asyncio.run(engine.eod_plan("2099-01-01", signals=_signal_600000(),
                                atr_map={"600000.SH": 0.2}, capital=1_000_000))
    account_id = engine._resolve_account_id()
    trade_id = f"{account_id}_600000.SH_2099-01-01"
    assert state_store.get_latest_action(trade_id) == "CONFIRMED"


def test_eod_plan_signal_idempotent(monkeypatch, _state_db):
    """重跑 eod_plan → SIGNAL 已存在 → 跳过（UNIQUE 幂等，不重复记）。"""
    import sqlite3
    from trading import state_store
    monkeypatch.setattr(engine, "_submit", _no_op_submit)
    monkeypatch.setattr(trading_plan, "push_plan_to_dingtalk", lambda d, o, **kw: True)
    asyncio.run(engine.eod_plan("2099-01-01", signals=_signal_600000(),
                                atr_map={"600000.SH": 0.2}, capital=1_000_000))
    asyncio.run(engine.eod_plan("2099-01-01", signals=_signal_600000(),
                                atr_map={"600000.SH": 0.2}, capital=1_000_000))
    # SIGNAL 行仍只有 1 条（幂等去重）
    with sqlite3.connect(_state_db) as con:
        n = con.execute(
            "SELECT COUNT(*) FROM trade_event WHERE action='SIGNAL' AND symbol='600000.SH'"
        ).fetchone()[0]
    assert n == 1


def test_eod_plan_veto_protection(monkeypatch, _state_db):
    """trade_event VETOED 后重跑 eod_plan → 不重写为 CONFIRMED（veto 保护）。"""
    from trading import state_store
    monkeypatch.setattr(engine, "_submit", _no_op_submit)
    monkeypatch.setattr(trading_plan, "push_plan_to_dingtalk", lambda d, o, **kw: True)
    monkeypatch.setenv("AUTO_CONFIRM_PLAN", "true")
    asyncio.run(engine.eod_plan("2099-01-01", signals=_signal_600000(),
                                atr_map={"600000.SH": 0.2}, capital=1_000_000))
    # 研究员 veto：写 VETOED 事件
    account_id = engine._resolve_account_id()
    trade_id = f"{account_id}_600000.SH_2099-01-01"
    state_store.insert_trade_event(account_id, trade_id, "600000.SH", "VETOED")
    # 重跑 eod_plan（auto_confirm 仍 true）→ 最新 action 应仍是 VETOED（不被 CONFIRMED 覆盖）
    asyncio.run(engine.eod_plan("2099-01-01", signals=_signal_600000(),
                                atr_map={"600000.SH": 0.2}, capital=1_000_000))
    assert state_store.get_latest_action(trade_id) == "VETOED"


# ----------------------------------------------------------------------------
# T7（state-store-redesign）：cancel_all_open_orders 改查柜台（不依赖 gw._orders 内存）
# ----------------------------------------------------------------------------
def test_cancel_uses_query_orders_not_memory():
    """撤单查柜台：mock gw.query_orders 返 2 笔可撤单 + gw._orders 空 → 撤 2 笔。

    物理意图（P0-4 根因修复）：旧路径遍历 gw._orders 内存，新连接/重启后内存空 → 漏撤柜台
    旧单。改走 query_orders(cancelable_only=True) 查柜台全量，撤单不再依赖内存。
    """
    from trading.io import breaker
    from unittest.mock import AsyncMock, MagicMock
    from trading.types import OrderState

    gw = MagicMock()
    gw._orders = {}  # 内存空（模拟新连接/重启）
    gw._confirm_cancelled = AsyncMock(return_value=True)
    # query_orders 返 2 笔柜台可撤单（order_id=柜台真实单号）
    async def _query_orders(cancelable_only=False):
        return [
            {"order_id": 1001, "state": OrderState.SUBMITTED, "stock_code": "600000.SH"},
            {"order_id": 1002, "state": OrderState.SUBMITTED, "stock_code": "600001.SH"},
        ]
    gw.query_orders = _query_orders
    cancelled_oids = []
    async def _cancel_by_broker_oid(broker_oid):
        cancelled_oids.append(broker_oid)
        from broker.base import OrderResult
        return OrderResult(order_id=str(broker_oid), state=OrderState.CANCELLED, message="ok")
    gw.cancel_order_by_broker_oid = _cancel_by_broker_oid

    res = asyncio.run(breaker.cancel_all_open_orders(gw))
    assert res["cancelled"] == 2
    assert set(cancelled_oids) == {1001, 1002}  # 用柜台单号撤，不依赖 _orders 内存


def test_cancel_updates_order_state_db(monkeypatch, tmp_path):
    """撤单后 state_store order.state=CANCELLED（回写 DB，对账一致）。"""
    from trading.io import breaker
    from trading import state_store
    from unittest.mock import AsyncMock, MagicMock
    from trading.types import OrderState
    from broker.base import OrderResult

    db_path = str(tmp_path / "state.db")
    monkeypatch.setattr(state_store, "_DEFAULT_DB", db_path)
    state_store.init_store()
    state_store.upsert_account("ACC1", broker="qmt")
    # 预置一笔 SUBMITTED 委托（模拟昨日挂单）
    state_store.insert_order("o1", "ACC1_X_2099", "ACC1", "2099-01-02", "600000.SH",
                             "buy", "OPEN", 100, 10.0, broker_oid="1001", state="SUBMITTED")
    gw = MagicMock()
    gw._orders = {}
    async def _query_orders(cancelable_only=False):
        return [{"order_id": 1001, "state": OrderState.SUBMITTED, "stock_code": "600000.SH"}]
    gw.query_orders = _query_orders
    gw._confirm_cancelled = AsyncMock(return_value=True)
    async def _cancel_by_broker_oid(broker_oid):
        return OrderResult(order_id=str(broker_oid), state=OrderState.CANCELLED, message="ok")
    gw.cancel_order_by_broker_oid = _cancel_by_broker_oid

    asyncio.run(breaker.cancel_all_open_orders(gw, account_id="ACC1"))
    # DB order.state 回写 CANCELLED
    assert state_store.get_pending_orders("ACC1") == []  # SUBMITTED 已变 CANCELLED，不在 pending


# ============================================================================
# 2. pre_open：未确认不挂 / 确认后挂 / 撤昨日单 / submit raise 兜底
# ============================================================================
def test_pre_open_blocks_unconfirmed_plan():
    """pre_open：计划未确认 → 不挂单，reason 含「未确认」。"""
    trading_plan.save_plan("2099-01-02", [])  # confirmed=False
    result = asyncio.run(engine.pre_open("2099-01-02"))
    assert result["submitted"] == 0
    assert "未确认" in result["reason"]


# ----------------------------------------------------------------------------
# T8（state-store-redesign）：pre_open DB 幂等挂单
# ----------------------------------------------------------------------------
def _confirmed_plan_one_order(date="2099-01-02"):
    """落一份已确认的单标的计划（pre_open 挂单测试共用种子）。"""
    orders_nested = [
        {"order": {"symbol": "600000.SH", "qty": 100.0, "side": "buy", "price": 10.0},
         "stop_price": 9.5, "take_profit": 11.0, "formed_at": date, "max_wait": 5},
    ]
    trading_plan.save_plan(date, orders_nested)
    trading_plan.confirm_plan(date)


def test_pre_open_inserts_order_and_event(monkeypatch, _state_db):
    """挂单后 order 表有 OPEN 行 + trade_event 有 ORDERED 行。"""
    import sqlite3
    from trading import state_store
    _confirmed_plan_one_order()
    monkeypatch.setattr(engine, "get_gateway", lambda: None)

    async def _dry_submit(order, **kw):
        return {"order_id": "seq1", "state": "DRY_RUN", "message": "影子"}
    monkeypatch.setattr(engine, "_submit", _dry_submit)
    asyncio.run(engine.pre_open("2099-01-02"))
    account_id = engine._resolve_account_id()
    assert state_store.has_order(account_id, "2099-01-02", "600000.SH", "OPEN") is True
    trade_id = f"{account_id}_600000.SH_2099-01-02"
    assert state_store.get_latest_action(trade_id) == "ORDERED"


def test_pre_open_idempotent(monkeypatch, _state_db):
    """同日 pre_open 调两次 → 只挂一次（has_order OPEN 第二次跳过，submit 只调 1 次）。"""
    _confirmed_plan_one_order()
    monkeypatch.setattr(engine, "get_gateway", lambda: None)
    submit_calls = {"n": 0}
    async def _counting_submit(order, **kw):
        submit_calls["n"] += 1
        return {"order_id": "x", "state": "DRY_RUN", "message": "影子"}
    monkeypatch.setattr(engine, "_submit", _counting_submit)
    asyncio.run(engine.pre_open("2099-01-02"))
    asyncio.run(engine.pre_open("2099-01-02"))  # 第二次：has_order OPEN → 跳过
    assert submit_calls["n"] == 1  # 只挂一次（DB 幂等）


def test_pre_open_skips_vetoed(monkeypatch, _state_db):
    """trade_event 最新 action=VETOED → 跳过该标的（不挂单）。"""
    from trading import state_store
    _confirmed_plan_one_order()
    # 研究员 veto 该标的（eod_plan 已落 SIGNAL，再写 VETOED）
    account_id = engine._resolve_account_id()
    trade_id = f"{account_id}_600000.SH_2099-01-02"
    state_store.upsert_account(account_id, broker="qmt")
    state_store.insert_trade_event(account_id, trade_id, "600000.SH", "SIGNAL")
    state_store.insert_trade_event(account_id, trade_id, "600000.SH", "VETOED")
    monkeypatch.setattr(engine, "get_gateway", lambda: None)
    submit_calls = {"n": 0}
    async def _counting_submit(order, **kw):
        submit_calls["n"] += 1
        return {"order_id": "x", "state": "DRY_RUN", "message": "影子"}
    monkeypatch.setattr(engine, "_submit", _counting_submit)
    asyncio.run(engine.pre_open("2099-01-02"))
    assert submit_calls["n"] == 0  # vetoed 不挂单
    assert state_store.has_order(account_id, "2099-01-02", "600000.SH", "OPEN") is False


def test_pre_open_blocks_when_no_plan():
    """pre_open：无计划文件 → 不挂单，reason 含「无计划」。"""
    result = asyncio.run(engine.pre_open("2099-12-31"))
    assert result["submitted"] == 0
    assert "无计划" in result["reason"]


# ----------------------------------------------------------------------------
# T12（state-store-redesign）：废弃 _tp_placed 内存（DB has_order 为唯一真相源）
# ----------------------------------------------------------------------------
def test_no_tp_placed_memory():
    """engine.py 不再依赖 _tp_placed 内存态（grep 无 _tp_placed 赋值/读写）。

    物理意图（spec §3.6）：_tp_placed 是进程内存，engine 重启清空 → 重连重推 →
    重复挂止盈超卖（P0-1）。已由 state_store.has_order(TP1) DB 查询替代（跨重启持久）。
    本测试断言 engine.py 源码无 _tp_placed 的赋值语句（= 彻底废弃，非保留兼容）。
    """
    import pathlib
    src = pathlib.Path(engine.__file__).read_text(encoding="utf-8")
    # 不应再有 _tp_placed 的赋值（self._tp_placed = ... 或 .add(...)）
    assert "self._tp_placed" not in src, "engine.py 仍含 _tp_placed 实例属性（应已废弃，改用 DB has_order）"
    assert "_tp_placed.add" not in src, "engine.py 仍写 _tp_placed.add（应已废弃，改用 DB insert_order）"


def test_pre_open_cancels_yesterday_open_orders(monkeypatch):
    """scope #2：pre_open 开头必须调 cancel_all_open_orders 撤昨日未成交单。"""
    # 准备一份已确认但 orders 空的计划（聚焦撤单断言，不挂单）
    trading_plan.save_plan("2099-01-02", [])
    assert trading_plan.confirm_plan("2099-01-02")

    cancelled = {"n": 0}

    class _FakeGw:
        async def _fetch_broker_positions(self):
            return {}

    # U5：pre_open 现调 _cancel_all_open_orders(gw, account_id=...) 激活 CANCELLED 回写，
    # 测试桩须兼容 account_id 关键字参数（不真撤单）。
    async def _fake_cancel(gw, account_id=None):
        cancelled["n"] += 1
        return 0

    monkeypatch.setattr(engine, "_cancel_all_open_orders", _fake_cancel)
    monkeypatch.setattr(engine, "get_gateway", lambda: _FakeGw())

    asyncio.run(engine.pre_open("2099-01-02"))
    assert cancelled["n"] == 1, "pre_open 必须在挂单前撤昨日未成交单（scope #2）"


def test_pre_open_skip_cancel_when_no_gateway(monkeypatch):
    """scope #2：gw=None 时跳过撤单（logger.warning），不抛。"""
    trading_plan.save_plan("2099-01-02", [])
    trading_plan.confirm_plan("2099-01-02")

    cancelled = {"n": 0}

    async def _fake_cancel(gw):
        cancelled["n"] += 1
        return 0

    monkeypatch.setattr(engine, "_cancel_all_open_orders", _fake_cancel)
    monkeypatch.setattr(engine, "get_gateway", lambda: None)  # 网关未装配

    result = asyncio.run(engine.pre_open("2099-01-02"))  # 不应抛
    assert cancelled["n"] == 0   # gw=None 没调撤单


def test_pre_open_submit_raise_continues(monkeypatch):
    """scope #7：单标的 submit_order raise（挡板命中）不炸整批，继续挂下一只。"""
    orders_nested = [
        {"order": {"symbol": "A.SH", "qty": 100.0, "side": "buy", "price": 10.0},
         "stop_price": 9.5, "take_profit": 11.0},
        {"order": {"symbol": "B.SH", "qty": 100.0, "side": "buy", "price": 20.0},
         "stop_price": 19.0, "take_profit": 22.0},
    ]
    trading_plan.save_plan("2099-01-02", orders_nested)
    trading_plan.confirm_plan("2099-01-02")

    monkeypatch.setattr(engine, "get_gateway", lambda: object())
    monkeypatch.setattr(engine, "_cancel_all_open_orders",
                        _no_op_cancel)

    calls = []

    async def _flaky_submit(order, **kw):
        calls.append(order.symbol)
        if order.symbol == "A.SH":
            raise RuntimeError("挡板命中：资金不足")  # 模拟挡板 raise
        return {"order_id": "seq1", "state": "SUBMITTED", "message": "ok"}

    monkeypatch.setattr(engine, "_submit", _flaky_submit)

    result = asyncio.run(engine.pre_open("2099-01-02"))
    # 两只都被尝试（A 抛、B 成），submitted=1（仅 B 成功）
    assert calls == ["A.SH", "B.SH"]
    assert result["submitted"] == 1


# ============================================================================
# 3. stop_loss_monitor：非盘中不操作 / dry_run 不真卖 / qty 来自 gw 持仓 / 现价走 qmt_market_data
# ============================================================================
def test_stop_loss_monitor_off_session_no_op(monkeypatch):
    """scope #3：非盘中时段 → reason 含「非盘中」，不调 gw、不调 _submit。"""
    # 强制非盘中：monkeypatch calendar.is_intraday_session 返 False
    monkeypatch.setattr(engine.calendar, "is_intraday_session", lambda now: False)

    submitted = {"n": 0}

    async def _no_submit(order, **kw):
        submitted["n"] += 1
        return {"state": "DRY_RUN"}

    monkeypatch.setattr(engine, "_submit", _no_submit)

    result = asyncio.run(engine.stop_loss_monitor())
    assert "非盘中" in result["reason"]
    assert submitted["n"] == 0


def test_stop_loss_monitor_dry_run_no_real_sell(monkeypatch):
    """scope #3 + #5：盘中时段 dry_run 不真卖，qty 来自 gw._fetch_broker_positions。

    现价源走 ``qmt_market_data.get_quotes``（C1 fix + T3 批量优化）：monkeypatch 引擎内
    ``qmt_market_data`` 的 ``get_quotes`` 返 ``{symbol: {last_price: <值>} 或 None}`` 批量快照，
    构造「跌破 / 未跌破 / 现价缺失」三类场景，断言：①dry_run 不真下单（_submit 返 DRY_RUN）；
    ②只跌破的标的走 _submit；③现价缺失的标的不发盲单（C1 红线）；
    ④批量调用 1 次（T3 核心：N 次 get_quote → 1 次 get_quotes）。
    """
    monkeypatch.setattr(engine.calendar, "is_intraday_session", lambda now: True)

    # gw 持仓：A 跌破止损 / B 未跌破 / C 现价缺失
    class _FakeGw:
        async def _fetch_broker_positions(self):
            return {"A.SH": 300.0, "B.SH": 200.0, "C.SH": 150.0}

    monkeypatch.setattr(engine, "get_gateway", lambda: _FakeGw())

    # 现价快照（C1 fix + T3 批量后现价源）：A=9.0（跌破 9.5）/ B=21.0（未跌破 19.0）/ C=None（缺失）
    quote_map = {
        "A.SH": {"last_price": 9.0, "high_limit": 11.0, "low_limit": 8.0},
        "B.SH": {"last_price": 21.0, "high_limit": 23.0, "low_limit": 19.0},
        "C.SH": None,  # 现价缺失：get_quotes 返 None（如 xtdata 不可用 / EMT 无行情源）
    }
    quotes_calls = {"n": 0, "symbols": None}

    async def _fake_get_quotes(symbols):
        # T3 批量断言：一次传入全部持仓 symbol（N→1 核心优化点）
        quotes_calls["n"] += 1
        quotes_calls["symbols"] = list(symbols)
        return {s: quote_map.get(s) for s in symbols}

    # monkeypatch 引擎里 import 的 qmt_market_data.get_quotes 引用（T3 批量后的现价入口）
    monkeypatch.setattr(engine.qmt_market_data, "get_quotes", _fake_get_quotes)

    submitted = []

    async def _no_op_submit_dry(order, **kw):
        # dry_run 据 _mode，不应真下单
        submitted.append((order.symbol, order.qty, order.side))
        return {"state": "DRY_RUN"}

    monkeypatch.setattr(engine, "_submit", _no_op_submit_dry)

    # 止损价 map：A=9.5（跌破 9.0）/ B=19.0（未跌破 21.0）/ C=9.0（但现价 None 无法判）
    result = asyncio.run(
        engine.stop_loss_monitor(stop_prices={"A.SH": 9.5, "B.SH": 19.0, "C.SH": 9.0})
    )
    assert result["mode"] == "dry_run"
    assert result["stop_triggered"] == 1           # 只 A 触发
    # qty 必须来自持仓（300），不是魔法数 100（scope #3 live 安全红线）
    assert submitted == [("A.SH", 300.0, "sell")]
    # checked=2（A+B 有价），C 现价缺失不发盲单（C1 红线：无价不判跌破）
    assert result["checked"] == 2
    # T3 批量断言：get_quotes 只调 1 次（N→1 核心优化点），且传入全部持仓 symbol
    assert quotes_calls["n"] == 1
    assert set(quotes_calls["symbols"]) == {"A.SH", "B.SH", "C.SH"}


def test_stop_loss_monitor_nan_price_skipped(monkeypatch):
    """C1 红线补充：last_price=NaN 视作现价缺失，跳过该标的（不发盲单）。"""
    monkeypatch.setattr(engine.calendar, "is_intraday_session", lambda now: True)

    class _FakeGw:
        async def _fetch_broker_positions(self):
            return {"X.SH": 100.0}

    monkeypatch.setattr(engine, "get_gateway", lambda: _FakeGw())

    async def _nan_quotes(symbols):
        # last_price 为 NaN（脏数据）：price != price 判定为 NaN，应跳过
        return {s: {"last_price": float("nan")} for s in symbols}

    monkeypatch.setattr(engine.qmt_market_data, "get_quotes", _nan_quotes)

    submitted = {"n": 0}

    async def _no_submit(order, **kw):
        submitted["n"] += 1
        return {"state": "DRY_RUN"}

    monkeypatch.setattr(engine, "_submit", _no_submit)

    result = asyncio.run(
        engine.stop_loss_monitor(stop_prices={"X.SH": 10.0})
    )
    assert submitted["n"] == 0     # NaN 不发盲单
    assert result["checked"] == 0  # NaN 不计入 checked


def test_stop_loss_monitor_no_gateway_logs_and_skips(monkeypatch):
    """scope #3：盘中 gw=None → 不抛，记日志跳过（无法查持仓即无法决策）。"""
    monkeypatch.setattr(engine.calendar, "is_intraday_session", lambda now: True)
    monkeypatch.setattr(engine, "get_gateway", lambda: None)

    submitted = {"n": 0}

    async def _no_submit(order, **kw):
        submitted["n"] += 1
        return {"state": "DRY_RUN"}

    monkeypatch.setattr(engine, "_submit", _no_submit)

    result = asyncio.run(engine.stop_loss_monitor(stop_prices={"A.SH": 9.5}))
    assert submitted["n"] == 0
    assert result["checked"] == 0
    assert "网关" in result.get("reason", "") or result.get("stop_triggered", -1) == 0


# ----------------------------------------------------------------------------
# T10（state-store-redesign）：stop_loss DB 幂等（has_order STOP 未终态跳过）
# ----------------------------------------------------------------------------
def test_stop_loss_idempotent(monkeypatch, tmp_path):
    """跌破止损 + has_order(STOP) 已存在 → 跳过（不重复发卖，DB 幂等）。

    物理意图（P0-x 重复止损）：stop_loss_monitor 每 30s 巡检，跌破止损价后若不幂等，
    每轮都发卖单 → 同一持仓被卖 N 次。改 DB has_order(STOP) 检查：已有 STOP 委托则跳过。
    """
    from trading import state_store
    db_path = str(tmp_path / "state.db")
    monkeypatch.setattr(state_store, "_DEFAULT_DB", db_path)
    state_store.init_store()
    account_id = engine._resolve_account_id()
    state_store.upsert_account(account_id, broker="qmt")
    today = datetime.now().strftime("%Y-%m-%d")
    # 预置已挂的 STOP 委托（模拟上一轮已发止损单）
    state_store.insert_order(
        f"{today}_A.SH_STOP_1", f"{account_id}_A.SH_{today}", account_id,
        today, "A.SH", "sell", "STOP", 300, 9.0, state="SUBMITTED")

    monkeypatch.setattr(engine.calendar, "is_intraday_session", lambda now: True)

    class _FakeGw:
        async def _fetch_broker_positions(self):
            return {"A.SH": 300.0}

    monkeypatch.setattr(engine, "get_gateway", lambda: _FakeGw())

    async def _fake_get_quotes(symbols):
        return {"A.SH": {"last_price": 9.0}}  # 跌破 9.5

    monkeypatch.setattr(engine.qmt_market_data, "get_quotes", _fake_get_quotes)

    submit_calls = {"n": 0}

    async def _counting_submit(order, **kw):
        submit_calls["n"] += 1
        return {"state": "DRY_RUN"}

    monkeypatch.setattr(engine, "_submit", _counting_submit)

    asyncio.run(engine.stop_loss_monitor(stop_prices={"A.SH": 9.5}))
    assert submit_calls["n"] == 0  # 已有 STOP 委托 → 跳过（DB 幂等）


def test_stop_loss_reads_plan_from_db(monkeypatch, tmp_path):
    """从 trade_event SIGNAL meta 读 stop_price（不依赖 plan JSON）。

    物理意图（spec §3.3）：stop_price 真相源改 DB（get_trade_plan），plan JSON 仅人看。
    本测试验证 get_trade_plan 能从 SIGNAL meta 读出 stop_price（T5 已覆盖，此处串联 engine 链路）。
    """
    from trading import state_store
    db_path = str(tmp_path / "state.db")
    monkeypatch.setattr(state_store, "_DEFAULT_DB", db_path)
    state_store.init_store()
    account_id = engine._resolve_account_id()
    state_store.upsert_account(account_id, broker="qmt")
    today = datetime.now().strftime("%Y-%m-%d")
    trade_id = f"{account_id}_A.SH_{today}"
    # SIGNAL meta 存计划参数（含 stop_price）
    import json as _json
    state_store.insert_trade_event(
        account_id, trade_id, "A.SH", "SIGNAL",
        meta=_json.dumps({"stop_price": 9.5, "take_profit": 11.0}))
    plan = state_store.get_trade_plan(trade_id)
    assert plan is not None
    assert plan["stop_price"] == 9.5  # 从 DB SIGNAL meta 读出


# ============================================================================
# 4. post_close：对账照做，熔断显式留 TODO（本 task 不实现）
# ============================================================================
def test_post_close_runs_reconcile(monkeypatch):
    """post_close：传 gw + local_positions 时调 run_reconcile，返 drift 标志。"""
    from trading.compute.reconcile import ReconciliationResult  # Layer2 阶段6 follow-up #4b：execution_gateway 垫片已删，直指 compute.reconcile 真身

    class _FakeGw:
        async def _fetch_broker_positions(self):
            return {"A.SH": 100.0}

    fake_rec = ReconciliationResult(
        matched=[], drifted=[], only_local=[], only_broker=[],
        max_abs_drift=0.0, is_ok=True,
    )

    async def _fake_run_rec(gw, local, tolerance=0.0):
        return fake_rec

    monkeypatch.setattr(engine.reconcile_job, "run_reconcile", _fake_run_rec)

    result = asyncio.run(
        engine.post_close("2099-01-02", gw=_FakeGw(),
                          local_positions={"A.SH": 100.0})
    )
    assert result["date"] == "2099-01-02"
    assert result["drift"] is False  # is_ok=True → 无漂移


def test_post_close_no_gw_is_noop():
    """post_close：gw=None → 仅返日期，不抛（无对账数据则跳过）。"""
    result = asyncio.run(engine.post_close("2099-01-02"))
    assert result["date"] == "2099-01-02"
    assert "drift" not in result   # 未对账就无 drift 字段


# ----------------------------------------------------------------------------
# T11（state-store-redesign）：post_close trade_event(CLOSED) + account_daily
# ----------------------------------------------------------------------------
def test_post_close_snapshot_close_equity(monkeypatch, tmp_path):
    """account_daily 写 close_total_asset + daily_pnl（收盘快照 + 盈亏）。"""
    from trading import state_store
    db_path = str(tmp_path / "state.db")
    monkeypatch.setattr(state_store, "_DEFAULT_DB", db_path)
    state_store.init_store()
    account_id = engine._resolve_account_id()
    state_store.upsert_account(account_id, broker="qmt")
    today = datetime.now().strftime("%Y-%m-%d")
    # 预置 start 快照（pre_open 写的基线）
    state_store.snapshot_start_equity(account_id, today, 1_000_000.0, 500_000.0)

    class _FakeGw:
        async def query_asset(self):
            return {"total_asset": 1_020_000.0, "cash": 510_000.0, "market_value": 510_000.0}
    monkeypatch.setattr(engine, "get_gateway", lambda: _FakeGw())
    asyncio.run(engine.post_close(today, gw=_FakeGw(), local_positions={}))
    # account_daily 有 close 字段 + daily_pnl=20000
    import sqlite3
    with sqlite3.connect(db_path) as con:
        row = con.execute(
            "SELECT close_total_asset, daily_pnl FROM account_daily"
            " WHERE account_id=? AND date=?", (account_id, today)).fetchone()
    assert row is not None
    assert row[0] == 1_020_000.0
    assert row[1] == 20_000.0  # 1020000 - 1000000


def test_post_close_inserts_closed_event(monkeypatch, tmp_path):
    """持仓归零 → trade_event(CLOSED, realized_pnl)。

    物理意图（spec §3.2 post_close）：盘后查 position 归零的 trade → insert
    trade_event(CLOSED) 标记该 trade 生命周期结束（不再算 active）。
    """
    from trading import state_store
    db_path = str(tmp_path / "state.db")
    monkeypatch.setattr(state_store, "_DEFAULT_DB", db_path)
    state_store.init_store()
    account_id = engine._resolve_account_id()
    state_store.upsert_account(account_id, broker="qmt")
    today = datetime.now().strftime("%Y-%m-%d")
    trade_id = f"{account_id}_A.SH_{today}"
    # 建 SIGNAL + FILLED 事件，且 position 已归零（卖出平仓后 apply_fill_to_position 删除行）
    state_store.insert_trade_event(account_id, trade_id, "A.SH", "SIGNAL")
    state_store.insert_trade_event(account_id, trade_id, "A.SH", "FILLED")
    # position 无该行（已平仓归零）→ post_close 应标 CLOSED
    monkeypatch.setattr(engine, "get_gateway", lambda: None)
    asyncio.run(engine.post_close(today, gw=None, local_positions=None))
    assert state_store.get_latest_action(trade_id) == "CLOSED"


def test_post_close_tp1_filled_event(monkeypatch, tmp_path):
    """止盈成交 → trade_event(TP1_FILLED, realized_pnl)。"""
    from trading import state_store
    db_path = str(tmp_path / "state.db")
    monkeypatch.setattr(state_store, "_DEFAULT_DB", db_path)
    state_store.init_store()
    account_id = engine._resolve_account_id()
    state_store.upsert_account(account_id, broker="qmt")
    today = datetime.now().strftime("%Y-%m-%d")
    trade_id = f"{account_id}_A.SH_{today}"
    state_store.insert_trade_event(account_id, trade_id, "A.SH", "SIGNAL")
    state_store.insert_trade_event(account_id, trade_id, "A.SH", "FILLED")
    # TP1 委托已 FILLED（成交）
    state_store.insert_order(
        f"{today}_A.SH_TP1_1", trade_id, account_id, today, "A.SH", "sell", "TP1",
        100, 11.0, state="FILLED", filled_qty=100, filled_price=11.0)
    monkeypatch.setattr(engine, "get_gateway", lambda: None)
    asyncio.run(engine.post_close(today, gw=None, local_positions=None))
    actions = []
    import sqlite3
    with sqlite3.connect(db_path) as con:
        rows = con.execute(
            "SELECT action FROM trade_event WHERE trade_id=? ORDER BY event_id", (trade_id,)).fetchall()
    actions = [r[0] for r in rows]
    assert "TP1_FILLED" in actions


# ============================================================================
# 4.7 Task 10（R-2 日内熔断）：pre_open 快照 + post_close 三步串联
# ============================================================================
def test_pre_open_snapshot_start_equity(monkeypatch):
    """pre_open：确认闸通过后调 query_asset → snapshot_start_equity 写 account_daily。

    物理意图（W4 · 08-04 断链根治）：
        pre_open 抓日内熔断基线改调 ``_state_store.snapshot_start_equity(account_id,
        date, total, cash)`` 写 **account_daily** 表（与 post_close 的
        ``snapshot_close_equity`` 同表），让 ``daily_pnl = close - start`` 闭合。
        原调 ``_position_book.snapshot_start_equity`` 写 daily_equity 表，两表断链
        致 post_close 读 account_daily.start_total_asset 恒为 NULL → daily_pnl 恒 NULL
        （memory 发现 2：start 在 daily_equity、close 在 account_daily，无法相减）。
        - query_asset 返 {}（未连接/锁定/异常）→ 跳过快照 + WARN（不拿 0 误触发熔断）
        - 重入幂等：snapshot_start_equity 用 INSERT ... ON CONFLICT UPDATE，重启安全
    """
    from trading import state_store

    # 已确认计划
    orders = [{
        "order": {"symbol": "300001.SZ", "qty": 100, "side": "buy", "price": 10.0},
        "stop_price": 9.0, "take_profit": 11.0,
    }]
    trading_plan.save_plan("2099-01-02", orders)
    trading_plan.confirm_plan("2099-01-02")

    # 假 gw：query_asset 返 total_asset=1_000_000 + cash=500_000
    class _FakeGw:
        async def query_asset(self):
            return {"total_asset": 1_000_000.0, "cash": 500_000.0,
                    "market_value": 500_000.0, "account_id": "test"}
    monkeypatch.setattr(engine, "get_gateway", lambda: _FakeGw())
    monkeypatch.setattr(engine, "_cancel_all_open_orders", _no_op_cancel)

    submitted = []
    async def _fake_submit(order, **kw):
        submitted.append(order.symbol)
        return {"state": "SUBMITTED"}
    monkeypatch.setattr(engine, "_submit", _fake_submit)

    asyncio.run(engine.pre_open("2099-01-02"))

    # 验证 account_daily 已写入 start_total_asset + start_cash（W4 新口径）
    # C-6 V2：pre_open 内部 today_eq 改用传入 date 参数（入口缓存传递），故快照 date=2099-01-02
    # （与 _pre_open 入口 clock.today 传 pre_open(date) 同口径），不再用 datetime.now() 当日。
    account_id = engine._resolve_account_id()
    import sqlite3
    db_path = state_store._DEFAULT_DB
    with sqlite3.connect(db_path) as con:
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT start_total_asset, start_cash FROM account_daily"
            " WHERE account_id=? AND date=?", (account_id, "2099-01-02")).fetchone()
    assert row is not None, "account_daily 未写入 start 快照（W4 断链未修复）"
    assert row["start_total_asset"] == 1_000_000.0
    assert row["start_cash"] == 500_000.0


def test_pre_open_snapshot_calls_state_store_not_position_book(monkeypatch):
    """W4 调用点切换：pre_open 实际调 ``_state_store.snapshot_start_equity`` 而非
    ``_position_book.snapshot_start_equity``（spy 断言，非 mock 自欺）。

    物理意图（plan Step2 备注）：
        单测 snapshot_start/close 同表闭合本就 PASS（验证函数本身正确）；真正需断言的
        是「pre_open 实际调 state_store 版」——用 monkeypatch spy 包裹真函数，跑完
        pre_open 后断言 state_store 版被调 + position_book 版未被调。
    """
    from trading import state_store

    # 已确认计划
    trading_plan.save_plan("2099-01-02", [{
        "order": {"symbol": "300001.SZ", "qty": 100, "side": "buy", "price": 10.0},
        "stop_price": 9.0, "take_profit": 11.0,
    }])
    trading_plan.confirm_plan("2099-01-02")

    class _FakeGw:
        async def query_asset(self):
            return {"total_asset": 1_000_000.0, "cash": 500_000.0}
    monkeypatch.setattr(engine, "get_gateway", lambda: _FakeGw())
    monkeypatch.setattr(engine, "_cancel_all_open_orders", _no_op_cancel)
    monkeypatch.setattr(engine, "_submit", _no_op_submit_should_not_be_called_unused)

    # spy 包裹 state_store 版（记录被调参数，仍透传真函数写 DB）
    ss_calls = []
    _real_ss = state_store.snapshot_start_equity
    def _spy_ss(account_id, date, total_asset, cash=None, **kw):
        ss_calls.append((account_id, date, total_asset, cash))
        return _real_ss(account_id, date, total_asset, cash, **kw)
    monkeypatch.setattr(engine._state_store, "snapshot_start_equity", _spy_ss)

    # 注（B5）：原 position_book.snapshot_start_equity 已删（C-1/W4 读写口迁 account_daily），
    # 不再需要 spy 断言「position_book 版未被调」——该函数已不存在，调无可调。

    asyncio.run(engine.pre_open("2099-01-02"))

    # W4 断言：state_store 版被调（写 account_daily）。
    assert len(ss_calls) == 1, f"state_store.snapshot_start_equity 应被调 1 次，实际 {len(ss_calls)}"
    assert ss_calls[0][1] == "2099-01-02"           # date
    assert ss_calls[0][2] == 1_000_000.0            # total_asset
    assert ss_calls[0][3] == 500_000.0              # cash


def test_daily_pnl_closes_after_pre_open_and_post_close(monkeypatch):
    """W4 e2e 闭合：pre_open 写 start + post_close 写 close → account_daily 同 date
    有 start+close，daily_pnl 非空（= close - start）。

    物理意图（spec §5.2 daily_pnl 闭合）：
        两表断链根治验证——跑真 pre_open（写 account_daily.start）+ 真 post_close
        （写 account_daily.close 并算 daily_pnl），断言同 date 行 start/close/daily_pnl
        三字段齐全且 daily_pnl = close - start。原断链下 daily_pnl 恒 NULL
        （start 在 daily_equity 表，post_close 读 account_daily.start_total_asset 找不到）。
    """
    from trading import state_store

    # 用固定 date 让 pre_open/post_close 写同一行（post_close 用 clock.today，
    # 故 monkeypatch clock.today 返固定 date；pre_open 用传入 date 参数）
    fixed_date = "2099-01-02"
    from trading import clock
    monkeypatch.setattr(clock, "today", lambda: fixed_date)

    trading_plan.save_plan(fixed_date, [{
        "order": {"symbol": "300001.SZ", "qty": 100, "side": "buy", "price": 10.0},
        "stop_price": 9.0, "take_profit": 11.0,
    }])
    trading_plan.confirm_plan(fixed_date)

    # 假 gw：pre_open 抓 start=100w / post_close 抓 close=101.5w（+1.5% 不触发熔断）
    class _FakeGw:
        async def query_asset(self):
            return {"total_asset": 1_000_000.0, "cash": 500_000.0}
        async def _fetch_broker_positions(self):
            return {}
    # post_close 写 close 需要不同值——用可变总资产模拟盘中上涨
    class _FakeGwClose(_FakeGw):
        async def query_asset(self):
            return {"total_asset": 1_015_000.0, "cash": 510_000.0,
                    "market_value": 505_000.0}
    monkeypatch.setattr(engine, "get_gateway", lambda: _FakeGw())
    monkeypatch.setattr(engine, "_cancel_all_open_orders", _no_op_cancel)
    monkeypatch.setattr(engine, "_submit", _no_op_submit_should_not_be_called_unused)

    # pre_open 写 start（account_daily.start_total_asset=1_000_000）
    asyncio.run(engine.pre_open(fixed_date))

    # post_close 写 close + 算 daily_pnl（account_daily.close_total_asset=1_015_000）
    from trading.compute.reconcile import ReconciliationResult
    async def _fake_rec(gw, local, tolerance=0.0):
        return ReconciliationResult([], [], [], [], 0.0, True)
    monkeypatch.setattr(engine.reconcile_job, "run_reconcile", _fake_rec)
    asyncio.run(engine.post_close(fixed_date, gw=_FakeGwClose(), local_positions={}))

    # 验证 account_daily 同 date 有 start+close+daily_pnl（闭合）
    account_id = engine._resolve_account_id()
    import sqlite3
    db_path = state_store._DEFAULT_DB
    with sqlite3.connect(db_path) as con:
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT start_total_asset, close_total_asset, daily_pnl"
            " FROM account_daily WHERE account_id=? AND date=?",
            (account_id, fixed_date)).fetchone()
    assert row is not None, "account_daily 行不存在（pre_open/post_close 未同表写入）"
    assert row["start_total_asset"] == 1_000_000.0
    assert row["close_total_asset"] == 1_015_000.0
    # daily_pnl = close - start = 15000（W4 断链前恒 NULL，修复后非空）
    assert row["daily_pnl"] == 15_000.0, (
        f"daily_pnl 未闭合：期望 15000.0，实际 {row['daily_pnl']}（两表断链？）")


def test_pre_open_snapshot_skip_when_query_asset_empty(monkeypatch):
    """query_asset 返 {}（未连接）→ 跳过快照 + WARN（不拿 0 误触发熔断）。

    物理意图（边界 · spec §5.2）：
        gw 未连接 / 锁定 / 超时 → query_asset 返 {}。绝不能拿 0 当基线，
        否则 post_close check_daily_loss_limit(0, curr) 会因 start<=0 返 False
        反而永不熔断，或拿 None 参与除法抛 TypeError。正确行为：跳过快照 + 告警。
    """
    orders = [{
        "order": {"symbol": "300001.SZ", "qty": 100, "side": "buy", "price": 10.0},
        "stop_price": 9.0, "take_profit": 11.0,
    }]
    trading_plan.save_plan("2099-01-02", orders)
    trading_plan.confirm_plan("2099-01-02")

    class _FakeGw:
        async def query_asset(self):
            return {}  # 未连接/锁定/异常 → 空 dict
    monkeypatch.setattr(engine, "get_gateway", lambda: _FakeGw())
    monkeypatch.setattr(engine, "_cancel_all_open_orders", _no_op_cancel)
    monkeypatch.setattr(engine, "_submit", _no_op_submit_should_not_be_called_unused)

    asyncio.run(engine.pre_open("2099-01-02"))

    # 验证未写 account_daily（pre_open query_asset 返空 → 不写 start 基线）
    # C-1 收口后熔断读 state_store.get_start_equity（读 account_daily），故验它返 None。
    today = datetime.now().strftime("%Y-%m-%d")
    from trading import state_store
    assert state_store.get_start_equity(engine._resolve_account_id(), today) is None


async def _no_op_submit_should_not_be_called_unused(order, **kw):
    """占位（用于 test_pre_open_snapshot_skip_when_query_asset_empty，挂单会被调
    但本测试不验证挂单，故不抛错只返 SUBMITTED）。"""
    return {"state": "SUBMITTED"}


def test_post_close_circuit_breaker_triggers(monkeypatch):
    """post_close：start=100w/curr=96w（-4%）→ cancel_all + emergency_halt + ERROR 告警。

    物理意图（plan Task 10 Step 3 · 对齐缺口 R-2）：
        post_close 在 reconcile 之后串联三步：
          1) state_store.get_start_equity(account_id, today) → start_equity
             （W4+C-1 后熔断基线统一从 account_daily 读，与 pre_open 写口同表）
          2) query_asset → curr_equity
          3) check_daily_loss_limit(start, curr) → True 即 cancel_all + emergency_halt
        缺基线（start=None）→ 跳过 + WARN（不拿 0 误触发）。
    """
    # 写基线 100w（_seed_breaker_baseline：W4+C-1 后基线从 account_daily 读，与生产同口径）
    today = datetime.now().strftime("%Y-%m-%d")
    _seed_breaker_baseline(today, 1_000_000.0)

    class _FakeGw:
        async def query_asset(self):
            return {"total_asset": 960_000.0}  # -4% 触发熔断（限 -3%）
        async def _fetch_broker_positions(self):
            return {}
    fake_gw = _FakeGw()

    # 拦截副作用：cancel_all + emergency_halt
    # 注意：T3 fix I-1 后 post_close:1261 取 _cb_res["unconfirmed"]，要求返回
    # dict（对齐 cancel_all_open_orders 的 {"cancelled":int,"unconfirmed":int}
    # 契约）。早期版本返回 int 0 会让 critical 告警逻辑抛 TypeError 被 :1267
    # except 吞掉 → 测试假 PASS。此处统一改为 dict。
    cancel_calls = []
    async def _fake_cancel(gw):
        cancel_calls.append(gw)
        return {"cancelled": 0, "unconfirmed": 0}
    halt_calls = []
    def _fake_halt():
        halt_calls.append(True)
        return {"halted": True}
    monkeypatch.setattr(engine, "_cancel_all_open_orders", _fake_cancel)
    monkeypatch.setattr(
        "presentation.server.services.trading_service.emergency_halt", _fake_halt)

    # reconcile mock（返 ok，不干扰熔断路径）
    from trading.compute.reconcile import ReconciliationResult
    fake_rec = ReconciliationResult(
        matched=[], drifted=[], only_local=[], only_broker=[],
        max_abs_drift=0.0, is_ok=True)
    async def _fake_run_rec(gw, local, tolerance=0.0):
        return fake_rec
    monkeypatch.setattr(engine.reconcile_job, "run_reconcile", _fake_run_rec)

    result = asyncio.run(engine.post_close(
        today, gw=fake_gw, local_positions={}))

    assert result.get("circuit_breaker") is True
    assert len(cancel_calls) == 1     # cancel_all 被调
    assert len(halt_calls) == 1       # emergency_halt 被调


def test_post_close_circuit_breaker_warns_unconfirmed(monkeypatch, caplog):
    """post_close 熔断：cancel_all 返 unconfirmed>0 → critical 告警（T3 fix I-1 回归）。

    物理意图：
        熔断是实盘最致命路径（敞口已超阈值）。撤单若留未确认终态的单，意味着
        柜台可能仍有挂单活着——敞口无法闭环。必须 critical 告警，人工复核真实
        持仓。此测试断言该 critical 告警路径**真正执行**（非被 except 吞掉）。

    覆盖回归：
        早期 _fake_cancel 返 int 0 → engine.py:1261 _cb_res["unconfirmed"]
        抛 TypeError → :1267 except Exception 吞错 → critical 从未触发，测试假 PASS。
        现修 _fake_cancel 返 dict + 本用例直接断言 logger.critical 被调且消息含
        「未确认终态」/「敞口可能残留」。
    """
    import logging

    today = datetime.now().strftime("%Y-%m-%d")
    _seed_breaker_baseline(today, 1_000_000.0)

    class _FakeGw:
        async def query_asset(self):
            return {"total_asset": 960_000.0}  # -4% 触发熔断
        async def _fetch_broker_positions(self):
            return {}
    fake_gw = _FakeGw()

    # 撤单返回 unconfirmed=1（模拟柜台未确认终态）→ 必须触发 critical
    async def _fake_cancel(gw):
        return {"cancelled": 2, "unconfirmed": 1}
    def _fake_halt():
        return {"halted": True}
    monkeypatch.setattr(engine, "_cancel_all_open_orders", _fake_cancel)
    monkeypatch.setattr(
        "presentation.server.services.trading_service.emergency_halt", _fake_halt)

    # reconcile mock
    from trading.compute.reconcile import ReconciliationResult
    fake_rec = ReconciliationResult(
        matched=[], drifted=[], only_local=[], only_broker=[],
        max_abs_drift=0.0, is_ok=True)
    async def _fake_run_rec(gw, local, tolerance=0.0):
        return fake_rec
    monkeypatch.setattr(engine.reconcile_job, "run_reconcile", _fake_run_rec)

    with caplog.at_level(logging.CRITICAL, logger="trading.engine"):
        result = asyncio.run(engine.post_close(
            today, gw=fake_gw, local_positions={}))

    # 熔断已触发（前置）
    assert result.get("circuit_breaker") is True
    # 断言 critical 告警确实被调，且消息覆盖未确认/敞口语义
    critical_msgs = [r.getMessage() for r in caplog.records
                     if r.levelno >= logging.CRITICAL]
    assert any("未确认终态" in m or "敞口可能残留" in m for m in critical_msgs), (
        f"熔断 unconfirmed critical 告警未触发！caplog critical: {critical_msgs}")


def test_post_close_circuit_breaker_skip_when_within_limit(monkeypatch):
    """post_close：curr 只跌 2%（未触 -3%）→ 不熔断，cancel_all/halt 均不调。"""
    today = datetime.now().strftime("%Y-%m-%d")
    _seed_breaker_baseline(today, 1_000_000.0)

    class _FakeGw:
        async def query_asset(self):
            return {"total_asset": 980_000.0}  # -2% 不触发
        async def _fetch_broker_positions(self):
            return {}
    fake_gw = _FakeGw()

    cancel_calls = []
    async def _fake_cancel(gw):
        cancel_calls.append(gw)
        return 0
    halt_calls = []
    def _fake_halt():
        halt_calls.append(True)
        return {"halted": True}
    monkeypatch.setattr(engine, "_cancel_all_open_orders", _fake_cancel)
    monkeypatch.setattr(
        "presentation.server.services.trading_service.emergency_halt", _fake_halt)

    from trading.compute.reconcile import ReconciliationResult
    fake_rec = ReconciliationResult(
        matched=[], drifted=[], only_local=[], only_broker=[],
        max_abs_drift=0.0, is_ok=True)
    async def _fake_run_rec(gw, local, tolerance=0.0):
        return fake_rec
    monkeypatch.setattr(engine.reconcile_job, "run_reconcile", _fake_run_rec)

    result = asyncio.run(engine.post_close(today, gw=fake_gw, local_positions={}))

    assert result.get("circuit_breaker") is False
    assert cancel_calls == []
    assert halt_calls == []


def test_post_close_circuit_breaker_skip_when_no_baseline(monkeypatch):
    """post_close：无基线（start=None，未快照）→ 跳过 + WARN，不熔断。

    物理意图（边界 · spec §5.2）：
        pre_open 未抓到基线（query_asset 返空），post_close 无 start_equity。
        绝不拿 0 触发熔断（check_daily_loss_limit(0, X) 虽返 False，但语义模糊），
        显式跳过 + WARN 让研究员次日人工补基线。
    """
    from trading import position_book

    db_path = os.path.join(os.environ["TRADE_PLAN_DIR"], "..", "state.db")
    monkeypatch.setattr(position_book, "_DEFAULT_DB", db_path)
    position_book.init_db()
    # 不写基线（模拟 pre_open 未抓到）

    class _FakeGw:
        async def query_asset(self):
            return {"total_asset": 500_000.0}
        async def _fetch_broker_positions(self):
            return {}
    fake_gw = _FakeGw()

    cancel_calls = []
    async def _fake_cancel(gw):
        cancel_calls.append(gw)
        return 0
    halt_calls = []
    def _fake_halt():
        halt_calls.append(True)
        return {"halted": True}
    monkeypatch.setattr(engine, "_cancel_all_open_orders", _fake_cancel)
    monkeypatch.setattr(
        "presentation.server.services.trading_service.emergency_halt", _fake_halt)

    from trading.compute.reconcile import ReconciliationResult
    fake_rec = ReconciliationResult(
        matched=[], drifted=[], only_local=[], only_broker=[],
        max_abs_drift=0.0, is_ok=True)
    async def _fake_run_rec(gw, local, tolerance=0.0):
        return fake_rec
    monkeypatch.setattr(engine.reconcile_job, "run_reconcile", _fake_run_rec)

    today = datetime.now().strftime("%Y-%m-%d")
    result = asyncio.run(engine.post_close(today, gw=fake_gw, local_positions={}))

    # 无基线 → 跳过（breaker_skipped=True），不熔断
    assert result.get("circuit_breaker") is False
    assert result.get("breaker_skipped") is True
    assert cancel_calls == []
    assert halt_calls == []


# ============================================================================
# 4.5 Task 4（P1-6）：_trade_cfg 默认 stop_atr_mult=1.0（对齐回测 DEFAULTS）
# ============================================================================
def test_trade_cfg_stop_atr_mult_default_aligns_backtest(monkeypatch):
    """_trade_cfg 默认 stop_atr_mult=1.0（对齐回测 neckline/method_v0.DEFAULTS=1.0）。

    物理意图（plan Task 4 · 对齐缺口 P1-6）：
        回测 DEFAULTS["stop_atr_mult"]=1.0（strategies/neckline/method_v0.py:49），
        实盘老默认 env=2.0（engine.py:85）——回测冠军档套实盘时 stop 间隙不一致：
        - stop_atr_mult=1.0 → stop=颈线-1×ATR
        - stop_atr_mult=2.0 → stop=颈线-2×ATR（更宽，回测锚定 1.0 时实盘风险敞口翻倍）
        把默认值对齐回测，env 仍可显式覆盖（spec §4.6）。

    Why 简化版（用户 plan 指令）：
        plan Step 2 原写「_trade_cfg 加 active_experiments 参数，从主力实验 params 读」，
        本 task 简化为只改默认值（env 兜底），从实验 params 读标 follow-up 待 Task 14
        param_iter 重跑后落地（与 Task 4 单独跟 param_iter 冠军档绑定更稳）。
    """
    # 清掉 env，强制走默认（验证默认值=1.0，不是 2.0）
    monkeypatch.delenv("TRADE_STOP_ATR_MULT", raising=False)
    cfg = engine._trade_cfg()
    assert cfg["stop_atr_mult"] == 1.0  # 默认对齐回测 DEFAULTS


def test_trade_cfg_stop_atr_mult_env_overrides(monkeypatch):
    """env 显式设置仍可覆盖默认（向后兼容：实盘调参不改代码）。"""
    monkeypatch.setenv("TRADE_STOP_ATR_MULT", "1.5")
    cfg = engine._trade_cfg()
    assert cfg["stop_atr_mult"] == 1.5


# ============================================================================
# 4.6 Task 6（P0-2 max_wait 等待窗口）：pre_open 按 formed_at+max_wait 过滤超期信号
# ============================================================================
def test_pre_open_skip_expired_signal(monkeypatch):
    """pre_open：order.formed_at 距今 > max_wait 交易日 → 跳过；<= max_wait → 挂。

    物理意图（plan Task 6 · 对齐缺口 P0-2）：
        回测信号后 max_wait 天窗口等回踩，实盘只挂 1 天（次日 pre_open 撤昨日）。
        pre_open 按 ``_trading_days_between(formed_at, today) > max_wait`` 过滤超期。
    """
    # 已确认计划，单 order formed_at=10 交易日之前，max_wait=5 → 应跳过不挂
    orders = [{
        "order": {"symbol": "300001.SZ", "qty": 100, "side": "buy", "price": 10.0},
        "stop_price": 9.0, "take_profit": 11.0,
        "formed_at": "2026-07-01",   # 远早于 today（2026-07-22），距 > 5 交易日
        "max_wait": 5,
    }]
    trading_plan.save_plan("2099-01-02", orders)
    trading_plan.confirm_plan("2099-01-02")

    # monkeypatch _trading_days_between 返 10（> max_wait=5）→ 该单应被跳过
    monkeypatch.setattr(engine, "get_gateway", lambda: object())
    monkeypatch.setattr(engine, "_cancel_all_open_orders", _no_op_cancel)
    monkeypatch.setattr(engine, "_trading_days_between", lambda s, e: 10)

    submitted = []
    async def _fake_submit(order, **kw):
        submitted.append(order.symbol)
        return {"state": "SUBMITTED"}
    monkeypatch.setattr(engine, "_submit", _fake_submit)

    result = asyncio.run(engine.pre_open("2099-01-02"))
    assert submitted == [], "超期信号（formed_at 距今 > max_wait）应被跳过不挂"
    assert result["submitted"] == 0


def test_pre_open_within_max_wait_window_is_placed(monkeypatch):
    """pre_open：order.formed_at 距今 <= max_wait → 挂单（窗口内每日可挂）。"""
    orders = [{
        "order": {"symbol": "300001.SZ", "qty": 100, "side": "buy", "price": 10.0},
        "stop_price": 9.0, "take_profit": 11.0,
        "formed_at": "2026-07-18",   # 3 交易日之前
        "max_wait": 5,                # <= max_wait → 应挂
    }]
    trading_plan.save_plan("2099-01-02", orders)
    trading_plan.confirm_plan("2099-01-02")

    monkeypatch.setattr(engine, "get_gateway", lambda: object())
    monkeypatch.setattr(engine, "_cancel_all_open_orders", _no_op_cancel)
    monkeypatch.setattr(engine, "_trading_days_between", lambda s, e: 3)

    submitted = []
    async def _fake_submit(order, **kw):
        submitted.append(order.symbol)
        return {"state": "SUBMITTED"}
    monkeypatch.setattr(engine, "_submit", _fake_submit)

    result = asyncio.run(engine.pre_open("2099-01-02"))
    assert submitted == ["300001.SZ"]
    assert result["submitted"] == 1


def test_pre_open_formed_at_missing_fallback_places(monkeypatch):
    """pre_open：order 缺 formed_at → days=0 视作窗口内，挂单（向后兼容老 plan）。"""
    orders = [{
        "order": {"symbol": "300001.SZ", "qty": 100, "side": "buy", "price": 10.0},
        "stop_price": 9.0, "take_profit": 11.0,
        # 无 formed_at（老 plan，Task 2 前落盘的）
        "max_wait": 5,
    }]
    trading_plan.save_plan("2099-01-02", orders)
    trading_plan.confirm_plan("2099-01-02")

    monkeypatch.setattr(engine, "get_gateway", lambda: object())
    monkeypatch.setattr(engine, "_cancel_all_open_orders", _no_op_cancel)
    monkeypatch.setattr(engine, "_trading_days_between", lambda s, e: 0)

    submitted = []
    async def _fake_submit(order, **kw):
        submitted.append(order.symbol)
        return {"state": "SUBMITTED"}
    monkeypatch.setattr(engine, "_submit", _fake_submit)

    result = asyncio.run(engine.pre_open("2099-01-02"))
    assert submitted == ["300001.SZ"]
    assert result["submitted"] == 1


# ============================================================================
# 5. TradingEngine 装配：实例化即注册 4 cron job（不 start，不真起 scheduler）
# ============================================================================
def test_engine_registers_four_cron_jobs():
    """TradingEngine 实例化 → AsyncIOScheduler 装 4 个 job（pipeline_then_eod/pre_open/stoploss/post_close）。

    不 start（plan 红线：不起 APScheduler 真调度），只验证 cron 注册成功。
    C-2 Task 9：原 ``eod_plan`` 19:00 cron 已由 ``pipeline_then_eod`` 事件链取代
    （采集→校验→eod→brief，args=[self]）。
    """
    eng = engine.TradingEngine()
    jobs = eng.sched.get_jobs()
    job_ids = {j.id for j in jobs}
    assert {"pipeline_then_eod", "pre_open", "stop_loss", "post_close"} <= job_ids


# ============================================================================
# 公共测试辅助
# ============================================================================
async def _no_op_submit(order, **kw):
    """占位 _submit：不应被调（调用即 fail）。"""
    raise AssertionError(f"_submit 不应被调（本 case 信号/计划为空）: {order}")


async def _no_op_cancel(gw):
    """占位 cancel_all_open_orders：no-op。"""
    return 0


# ============================================================================
# gap4：position_book 接线（_post_close 读账本 + _handle_order_update 写账本）
# ============================================================================
def test_post_close_reads_position_book(monkeypatch):
    """_post_close：读 position_book 账本 → 注入 local_positions → 调 post_close 对账。"""
    from unittest.mock import MagicMock
    from trading import position_book, engine as eng_mod

    # 让 position_book 返非空（模拟有真实成交累计的持仓）
    monkeypatch.setattr(position_book, "get_local_positions",
                        lambda **kw: {"300001.SZ": 100.0})
    captured = {}

    async def _fake_post_close(date, *, gw=None, local_positions=None, tolerance=0.0):
        captured["local"] = local_positions
        captured["date"] = date
        return {"date": date}

    monkeypatch.setattr(eng_mod, "post_close", _fake_post_close)
    monkeypatch.setattr(eng_mod.calendar, "is_trading_day", lambda d: True)
    # C-5 V4：_post_close 入口先过 _gw_health_gate，须 patch get_gateway 返 connected+ready
    # gw 让 gate 放行，否则 gate skip 到不了 post_close 对账。
    _gw = MagicMock()
    _gw._connected = True
    _gw.is_client_ready.return_value = True
    monkeypatch.setattr(eng_mod, "get_gateway", lambda: _gw)

    eng = eng_mod.TradingEngine()
    asyncio.run(eng._post_close())
    assert captured["local"] == {"300001.SZ": 100.0}  # 账本读出注入 post_close


def test_post_close_empty_book_passes_empty_dict(monkeypatch):
    """_post_close：账本空 → 传 {}（非 None）——live 下 broker 有时能报 only_broker drift。"""
    from unittest.mock import MagicMock
    from trading import position_book, engine as eng_mod

    monkeypatch.setattr(position_book, "get_local_positions", lambda **kw: {})
    captured = {}

    async def _fake_post_close(date, *, gw=None, local_positions=None, tolerance=0.0):
        captured["local"] = local_positions
        return {"date": date}

    monkeypatch.setattr(eng_mod, "post_close", _fake_post_close)
    monkeypatch.setattr(eng_mod.calendar, "is_trading_day", lambda d: True)
    # C-5 V4：补 connected+ready gw 让 gate 放行（同上用例）。
    _gw = MagicMock()
    _gw._connected = True
    _gw.is_client_ready.return_value = True
    monkeypatch.setattr(eng_mod, "get_gateway", lambda: _gw)

    eng = eng_mod.TradingEngine()
    asyncio.run(eng._post_close())
    assert captured["local"] == {}  # 空 dict 直传，不转 None


def test_handle_order_update_writes_book(monkeypatch, tmp_path):
    """BUY 成交回报 → state_store.insert_fill 被调（方向 "BUY"）；order_id 缺失 _orders
    （direction=None）→ insert_fill 不调。

    state-store-redesign 后账本写入改 state_store.insert_fill（DB 真相源），不再调
    position_book.apply_fill（避免双写 fill 表）。双 case 诚实覆盖：
      - 正向：DB order.side=buy → direction "BUY" → insert_fill 被调一次、direction="BUY"；
      - 反向：DB 无行 + _orders 空 → direction None → insert_fill 零调用（不猜方向误记）。
    """
    from unittest.mock import MagicMock, AsyncMock, patch
    from trading import state_store
    from trading.engine import TradingEngine

    # A5：账本写入已是真相源主链路（失败升 _CriticalHalt），必须隔离 DB 并建表
    db_path = str(tmp_path / "state.db")
    monkeypatch.setattr(state_store, "_DEFAULT_DB", db_path)
    state_store.init_store()

    # ---- 公共前置：成交回报报文 + 引擎实例 ----
    eng = TradingEngine()
    update = {
        "kind": "trade", "order_id": "999", "stock_code": "300001.SZ",
        "traded_volume": 100, "traded_price": 10.5, "state": "FILLED",
    }
    fake_mgr = MagicMock()
    fake_mgr.notify_trade_event = AsyncMock(return_value=[])

    # ---- Case 1（正向）：DB order.side=buy → BUY → insert_fill 被调一次、direction "BUY" ----
    # #1 修复后方向以 DB 为准：主推路径 _orders 无 order_type，不再手塞内存态。
    eng._gw = MagicMock()
    eng._gw._orders = {}  # 主推路径真实形态：无 order_type，方向必须来自 DB
    state_store.upsert_account("direction_test", broker="qmt")
    state_store.insert_order("2026-08-01_300001.SZ_OPEN_1", "direction_test_300001.SZ_2026-08-01",
                             "direction_test", "2026-08-01", "300001.SZ", "buy", "OPEN", 100, 10.0,
                             broker_oid="999", state="SUBMITTED")

    with patch("infra.notifier.NotificationManager") as NM1:
        NM1.get_default.return_value = fake_mgr
        with patch.object(eng, "_place_take_profit", new=AsyncMock()), \
             patch.object(state_store, "insert_fill", return_value=True) as if_buy:
            asyncio.run(eng._handle_order_update(update))
    if_buy.assert_called_once()
    # insert_fill(order_id, account_id, traded_time, symbol, direction, qty, price)
    assert if_buy.call_args.args[4] == "BUY"  # direction 位置参

    # ---- Case 2（反向）：DB 无行 + _orders 空 → direction None → insert_fill 零调用 ----
    update = dict(update); update["order_id"] = "998"  # 无 DB 行（Case 1 的 999 已存在）
    eng._gw = MagicMock()
    eng._gw._orders = {}  # 清空 _orders：_order_direction 查无 order_type → None

    with patch("infra.notifier.NotificationManager") as NM2:
        NM2.get_default.return_value = fake_mgr
        with patch.object(eng, "_place_take_profit", new=AsyncMock()), \
             patch.object(state_store, "insert_fill", return_value=True) as if_none:
            asyncio.run(eng._handle_order_update(update))
    if_none.assert_not_called()  # 方向 None 守门拦截，账本不写（防误记买当卖/卖当买）


def test_handle_order_update_book_failure_raises_l1(monkeypatch, tmp_path):
    """账本写入（insert_fill）抛异常 → 升 _CriticalHalt 停调度（A5 · C-4 分级）。

    原实现软降级会让 fill/position 静默缺失，对账只能事后发现；A5 改为敞口真相
    失真 = L1 停调度（宁可停不可带病跑）。
    """
    import pytest
    from unittest.mock import MagicMock, AsyncMock, patch
    from trading import state_store
    from trading.engine import TradingEngine, _CriticalHalt

    db_path = str(tmp_path / "state.db")
    monkeypatch.setattr(state_store, "_DEFAULT_DB", db_path)
    state_store.init_store()

    eng = TradingEngine()
    update = {
        "kind": "trade", "order_id": "999", "stock_code": "300001.SZ",
        "traded_volume": 100, "traded_price": 10.5, "state": "FILLED",
    }
    # 方向来自 DB（#1 口径，不手塞 _orders）
    eng._gw = MagicMock()
    eng._gw._orders = {}
    state_store.upsert_account("direction_test", broker="qmt")
    state_store.insert_order("2026-08-01_300001.SZ_OPEN_1", "direction_test_300001.SZ_2026-08-01",
                             "direction_test", "2026-08-01", "300001.SZ", "buy", "OPEN", 100, 10.0,
                             broker_oid="999", state="SUBMITTED")

    tp_called = []
    async def _tp(*a, **kw):
        tp_called.append(True)

    fake_mgr = MagicMock()
    fake_mgr.notify_trade_event = AsyncMock(return_value=[])
    # state_store 账本写抛异常（state-store-redesign 后账本入口改 insert_fill）
    with patch("infra.notifier.NotificationManager") as NM:
        NM.get_default.return_value = fake_mgr
        with patch.object(state_store, "insert_fill", side_effect=RuntimeError("db locked")), \
             patch.object(eng, "_place_take_profit", new=_tp):
            with pytest.raises(_CriticalHalt):
                asyncio.run(eng._handle_order_update(update))
    assert tp_called == [], "账本失败升 L1，止盈不应再执行"


# ============================================================================
# 4.8 Task 8（P0-4 max_holding 超时平仓）：pre_open 现算超期 + 跌停价平仓
# （SSoT Phase B 断点-2 · B1：删 expired_positions.json 跨日传递，收口 pre_open 现算）
# ============================================================================
def test_scan_expired_positions_marks_over_holding(monkeypatch):
    """_scan_expired_positions：holding_days>max_holding 标记，窗口内不标。

    物理意图（plan Task 8 Step 2 · 对齐缺口 P0-4）：
        回测 MAX_HOLDING=15（成交后超时周期），实盘 post_close 用 entry_date 算
        holding_days（交易日口径，trading_days_between），>max_holding 即标超期，
        次日 pre_open 跌停价平仓释放资金（对齐回测「超时收盘卖剩余」）。
    """
    from trading import position_book
    # A 超期（holding_days=20>15）、B 窗口内（5<=15）
    monkeypatch.setattr(position_book, "get_entry_dates",
                        lambda **kw: {"A.SH": "2099-01-01", "B.SH": "2099-01-15"})
    monkeypatch.setattr(engine, "_trading_days_between",
                        lambda s, e: 20 if s == "2099-01-01" else 5)

    expired = engine._scan_expired_positions("2099-01-21", 15)

    assert len(expired) == 1
    assert expired[0]["symbol"] == "A.SH"
    assert expired[0]["holding_days"] == 20
    assert expired[0]["max_holding"] == 15
    assert expired[0]["entry_date"] == "2099-01-01"


def test_scan_expired_positions_empty_when_all_within(monkeypatch):
    """全部窗口内（holding_days<=max_holding）→ 返空列表（不标超期）。"""
    from trading import position_book
    monkeypatch.setattr(position_book, "get_entry_dates",
                        lambda **kw: {"A.SH": "2099-01-15"})
    monkeypatch.setattr(engine, "_trading_days_between", lambda s, e: 5)

    assert engine._scan_expired_positions("2099-01-21", 15) == []


def test_scan_expired_boundary_holding_days(monkeypatch):
    """holding_days == max_holding 不平、> max_holding 平（超期语义红线，I-4 `>` 非 `>=`）。

    物理意图（SSoT Phase B 断点-2 · B1 边界断言 + I-4 兜底设计）：
        _scan_expired_positions 用严格 `holding_days > max_holding` 标超期（第 max_holding+1
        日才标），与 monitor is_last 的 `holding_days >= max_holding`（第 max_holding 日市价
        强平）错位一日，防同日双卖（市价先平后跌停再挂=卖空风险）。若误写成 `>=` 会被本测试
        抓（不假绿）。

    构造方式（Grill Me · 测代码不测 mock）：
        - 用 position_book.apply_fill 插【真实持仓】（写真实 entry_date + qty!=0，对齐生产路径
          的 get_entry_dates 仅返 qty!=0 + entry_date NOT NULL）；
        - monkeypatch engine._trading_days_between 确定性返 15/16（避开真实交易日历漂移，
          断言直接锁定 `_scan_expired_positions` 内部 `>` 比较的两个临界侧）。
    """
    from trading import position_book
    from trading import clock
    # 锁定 entry_date（apply_fill 用 clock.today() 写建仓日；_scan 不再读 clock，故先冻结
    # 写入再恢复，确保 entry_date 确定性 = "2099-01-01"，与 holding_days 桩的 s 参数对齐）。
    monkeypatch.setattr(clock, "today", lambda: "2099-01-01")
    monkeypatch.setattr(engine, "clock", clock)  # engine.clock 是同对象，但显式锁定防漂移
    # 用真实持仓表（_isolate_plan_dir autouse fixture 已隔离 position_book._DEFAULT_DB）
    position_book.apply_fill(
        "order_boundary", "600001.SH", "BUY", 100, 10.0,
        "2099-01-01T09:30:00")
    position_book.apply_fill(
        "order_within", "600002.SH", "BUY", 100, 10.0,
        "2099-01-01T09:30:00")
    # 基准日（任意固定值，holding_days 由 _trading_days_between 桩确定性返回）
    asof = "2099-01-21"

    # 侧 A：holding_days == max_holding（15 == 15）→ 不标超期（窗口内，给足机会）
    monkeypatch.setattr(
        engine, "_trading_days_between",
        lambda s, e: 15 if s == "2099-01-01" else 15)
    expired_at_boundary = engine._scan_expired_positions(asof, 15)
    assert expired_at_boundary == [], (
        "holding_days == max_holding 不应标超期（I-4：`>` 严格大于，第 max_holding 日仍给足"
        "机会；若标了说明误用 `>=`）")

    # 侧 B：holding_days > max_holding（16 > 15）→ 标超期
    monkeypatch.setattr(
        engine, "_trading_days_between",
        lambda s, e: 16 if s == "2099-01-01" else 16)
    expired_over = engine._scan_expired_positions(asof, 15)
    assert len(expired_over) == 2, (
        "holding_days > max_holding 应标超期（16 > 15）")
    symbols = {e["symbol"] for e in expired_over}
    assert symbols == {"600001.SH", "600002.SH"}
    # holding_days/max_holding/entry_date 字段透传正确
    for e in expired_over:
        assert e["holding_days"] == 16
        assert e["max_holding"] == 15
        assert e["entry_date"] == "2099-01-01"


def test_post_close_no_longer_scans_max_holding(monkeypatch):
    """post_close ⑤ max_holding 段已废（SSoT Phase B 断点-2 · B1）。

    物理意图：
        expired_positions 改 pre_open 现算后，post_close 不再扫超期、不再写
        expired_positions.json。result["expired_positions"] 字段同步废（不再出现）。
        原熔断优先约束（if not circuit_breaker_triggered）已无意义——pre_open 现算时
        上一交易日已收盘，不存在「同日熔断善后冲突」场景。
    """
    from trading import position_book
    # 真实持仓（即便 post_close 误扫也会扫到，断言更严）
    position_book.apply_fill(
        "order_post_close", "600001.SH", "BUY", 100, 10.0,
        "2099-01-01T09:30:00")
    monkeypatch.setattr(engine, "_trading_days_between", lambda s, e: 20)  # 远超 max_holding
    today = datetime.now().strftime("%Y-%m-%d")
    _seed_breaker_baseline(today, 1_000_000.0)  # 0% 不熔断

    class _FakeGw:
        async def query_asset(self):
            return {"total_asset": 1_000_000.0}
        async def _fetch_broker_positions(self):
            return {}
    from trading.compute.reconcile import ReconciliationResult
    async def _fake_rec(gw, local, tolerance=0.0):
        return ReconciliationResult([], [], [], [], 0.0, True)
    monkeypatch.setattr(engine.reconcile_job, "run_reconcile", _fake_rec)
    monkeypatch.setattr(engine, "_cancel_all_open_orders", _no_op_cancel)

    result = asyncio.run(engine.post_close(today, gw=_FakeGw(), local_positions={}))

    # B1 红线：post_close 不再标 expired（即便有超期持仓 + 未熔断）
    assert "expired_positions" not in result, (
        "B1 后 post_close 不应再扫 max_holding；result['expired_positions'] 字段已废")


def test_pre_open_closes_expired_positions(monkeypatch):
    """pre_open：现算超期 → 查 gw 持仓 → 跌停价卖（SSoT Phase B 断点-2 · B1）。

    物理意图（plan Task 8 Step 3 · B1 改造）：
        pre_open 在撤昨日单后、挂新买单前，【现算】超期持仓（基准日=上一交易日）：
        - qty 来自 gw._fetch_broker_positions 真实持仓（红线，同 stop_loss_monitor）；
        - 挂跌停价卖单（保证成交，超时释放资金接受滑点）；
        - 防重：DB 幂等（EXPIRED_CLOSE UNIQUE）兜住「同日 pre_open 重入重复挂卖」（B1 后
          唯一手段，原「删文件」已废）。
    """
    from trading import position_book
    # 真实持仓（pre_open 现算的扫描对象）
    position_book.apply_fill(
        "order_expired_pre", "300001.SZ", "BUY", 200, 10.0,
        "2099-01-01T09:30:00")
    # 当日已确认计划（买单标的与超期标的不同，互不干扰）
    orders = [{"order": {"symbol": "300002.SZ", "qty": 100, "side": "buy", "price": 10.0},
               "stop_price": 9.0, "take_profit": 11.0}]
    trading_plan.save_plan("2099-01-02", orders)
    trading_plan.confirm_plan("2099-01-02")
    # monkeypatch 基准日 + holding_days（避开真实日历漂移，锁定现算→平仓链路）
    monkeypatch.setattr(engine.clock, "today", lambda: "2099-01-02")
    monkeypatch.setattr(engine.clock, "pretrade_date", lambda d: "2099-01-01")
    monkeypatch.setattr(engine, "_trading_days_between", lambda s, e: 20)  # 20>15 标超期

    class _FakeGw:
        async def query_asset(self):
            return {"total_asset": 1_000_000.0}
        async def _fetch_broker_positions(self):
            return {"300001.SZ": {"volume": 200, "avg_price": 10.0}}  # 真实持仓 200 股
    monkeypatch.setattr(engine, "get_gateway", lambda: _FakeGw())
    monkeypatch.setattr(engine, "_cancel_all_open_orders", _no_op_cancel)
    async def _fake_quotes(syms):
        return {sym: {"last_price": 9.0, "low_limit": 8.5} for sym in syms}
    monkeypatch.setattr(engine.qmt_market_data, "get_quotes", _fake_quotes)
    submitted = []
    async def _fake_submit(order, **kw):
        submitted.append((order.symbol, order.side, order.qty, order.price))
        return {"state": "SUBMITTED"}
    monkeypatch.setattr(engine, "_submit", _fake_submit)

    asyncio.run(engine.pre_open("2099-01-02"))

    # 验平仓卖单：300001.SZ sell 200 @8.5（跌停价）
    sell_orders = [s for s in submitted if s[1] == "sell"]
    assert len(sell_orders) == 1
    assert sell_orders[0] == ("300001.SZ", "sell", 200, 8.5)


def test_pre_open_close_expired_skip_when_no_price(monkeypatch):
    """pre_open：无跌停价/现价 → 跳过该标的（拒发盲单）。

    物理意图（边界 · Grill Me 风控 · B1 改造）：
        quote 缺 low_limit + last_price（停牌/行情源异常）→ 无价不能挂卖单（盲单=卖错价=
        致命）。跳过该标的，漏平由人工对账兜底（宁可漏平也不发盲单）。B1 后无「删文件」
        副作用——pre_open 现算是无状态的，下次重入仍会跳过（DB 幂等 EXPIRED_CLOSE 也未写
        因为本笔未挂单）。
    """
    from trading import position_book
    position_book.apply_fill(
        "order_expired_nopx", "300001.SZ", "BUY", 200, 10.0,
        "2099-01-01T09:30:00")
    orders = [{"order": {"symbol": "300002.SZ", "qty": 100, "side": "buy", "price": 10.0},
               "stop_price": 9.0, "take_profit": 11.0}]
    trading_plan.save_plan("2099-01-02", orders)
    trading_plan.confirm_plan("2099-01-02")
    monkeypatch.setattr(engine.clock, "today", lambda: "2099-01-02")
    monkeypatch.setattr(engine.clock, "pretrade_date", lambda d: "2099-01-01")
    monkeypatch.setattr(engine, "_trading_days_between", lambda s, e: 20)  # 标超期

    class _FakeGw:
        async def query_asset(self):
            return {"total_asset": 1_000_000.0}
        async def _fetch_broker_positions(self):
            return {"300001.SZ": {"volume": 200, "avg_price": 10.0}}
    monkeypatch.setattr(engine, "get_gateway", lambda: _FakeGw())
    monkeypatch.setattr(engine, "_cancel_all_open_orders", _no_op_cancel)
    # quote 空 dict（无 low_limit/last_price）
    async def _fake_quotes(syms):
        return {sym: {} for sym in syms}
    monkeypatch.setattr(engine.qmt_market_data, "get_quotes", _fake_quotes)
    submitted = []
    async def _fake_submit(order, **kw):
        submitted.append((order.symbol, order.side))
        return {"state": "SUBMITTED"}
    monkeypatch.setattr(engine, "_submit", _fake_submit)

    asyncio.run(engine.pre_open("2099-01-02"))

    # 无价 → 300001.SZ 不发卖单（submitted 只含买单 300002）
    assert ("300001.SZ", "sell") not in submitted


# ============================================================================
# 4.9 Task 9（R-3 trailing 盘后演进）：post_close 演进 plan stop（compute_stop_price 消费 grace/step/floor）
# ============================================================================
def test_evolve_trailing_stops_writeback(monkeypatch):
    """_evolve_trailing_stops：holding_days>grace → 收紧 stop 写回 round2。

    物理意图（plan Task 9 · spec §5.3）：
        compute_stop_price 已实现但实盘零调用（env 读 grace/step/floor 未消费）。盘后演进
        让 trailing 真正生效：对每个【已成交持仓】按 holding_days 重算 stop_price。
        holding_days=7, grace=5, step=0.1 → eff_mult=1.0-(7-5)*0.1=0.8 → stop=10-0.8×0.5=9.6。
    """
    orders = [{"order": {"symbol": "A.SH"}, "neckline": 10.0, "atr": 0.5, "stop_price": 9.0}]
    monkeypatch.setattr(engine, "_trading_days_between", lambda s, e: 7)
    cfg = {"stop_atr_mult": 1.0, "grace": 5, "step": 0.1, "floor": 0.5}
    n = engine._evolve_trailing_stops(orders, {"A.SH": "2099-01-01"}, "2099-01-10", cfg)

    assert n == 1
    assert orders[0]["stop_price"] == 9.6   # 10 - 0.8×0.5（grace 后收紧 0.2 mult）


def test_evolve_trailing_stops_holding_days_zero_base_stop(monkeypatch):
    """holding_days=0（今日成交 / 缺 entry_date）→ stop=base_stop 零回归（不收紧）。

    物理意图（边界 · spec §5.3）：holding_days=0 视作 grace 内，compute_stop_price 返
        base_stop（颈线-stop_atr_mult×ATR）。零回归保证 Task 9 上线日不改变今日新成交持仓的止损。
    """
    orders = [{"order": {"symbol": "A.SH"}, "neckline": 10.0, "atr": 0.5, "stop_price": 9.0}]
    monkeypatch.setattr(engine, "_trading_days_between", lambda s, e: 0)
    cfg = {"stop_atr_mult": 1.0, "grace": 5, "step": 0.1, "floor": 0.5}
    # entry_dates 空（未成交 / 缺 entry）→ holding_days=0
    engine._evolve_trailing_stops(orders, {}, "2099-01-10", cfg)
    # base_stop = 10 - 1.0×0.5 = 9.5（grace 内不收紧）
    assert orders[0]["stop_price"] == 9.5


def test_evolve_trailing_stops_skips_missing_neckline_atr():
    """缺 neckline/atr 的 order 跳过不改（无基准无法重算 stop）。"""
    orders = [
        {"order": {"symbol": "A.SH"}, "neckline": None, "atr": 0.5, "stop_price": 9.0},
        {"order": {"symbol": "B.SH"}, "neckline": 10.0, "atr": None, "stop_price": 9.0},
    ]
    n = engine._evolve_trailing_stops(
        orders, {}, "2099-01-10",
        {"stop_atr_mult": 1.0, "grace": 5, "step": 0.1, "floor": 0.5})
    assert n == 0
    assert orders[0]["stop_price"] == 9.0   # 未改
    assert orders[1]["stop_price"] == 9.0


def test_post_close_trailing_skipped_after_breaker(monkeypatch):
    """post_close：日内熔断触发 → 跳过 trailing 演进（熔断优先，不收紧 stop）。

    物理意图（plan Task 9 Step 4 · spec §5.3 边界）：
        熔断（-3%）已 emergency_halt + lock_down，次日人工接管——此时再演进 stop 收紧
        可能触发额外卖出与熔断善后冲突。熔断优先于 trailing（同 max_holding 约束）。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    _seed_breaker_baseline(today, 1_000_000.0)
    # plan 有持仓 order（若 trailing 跑会演进，验熔断时它不被调）
    trading_plan.save_plan(today, [
        {"order": {"symbol": "A.SH"}, "neckline": 10.0, "atr": 0.5, "stop_price": 9.0}])

    evolve_calls = []
    def _fake_evolve(orders, ed, t, cfg):
        evolve_calls.append(True)
        return 0
    monkeypatch.setattr(engine, "_evolve_trailing_stops", _fake_evolve)

    class _FakeGw:
        async def query_asset(self):
            return {"total_asset": 950_000.0}  # -5% 触发熔断（限 -3%）
        async def _fetch_broker_positions(self):
            return {}
    monkeypatch.setattr(engine, "_cancel_all_open_orders", _no_op_cancel)
    monkeypatch.setattr(
        "presentation.server.services.trading_service.emergency_halt",
        lambda: {"halted": True})
    from trading.compute.reconcile import ReconciliationResult
    async def _fake_rec(gw, local, tolerance=0.0):
        return ReconciliationResult([], [], [], [], 0.0, True)
    monkeypatch.setattr(engine.reconcile_job, "run_reconcile", _fake_rec)

    result = asyncio.run(engine.post_close(today, gw=_FakeGw(), local_positions={}))
    assert result.get("circuit_breaker") is True
    assert evolve_calls == []                # 熔断 → trailing 演进未跑
    assert "trailing_evolved" not in result


def test_post_close_trailing_evolves_plan_stop(monkeypatch):
    """post_close：未熔断 + plan 有成交持仓 → trailing 演进 stop 写回 plan（保留 confirmed）。

    物理意图（plan Task 9 Step 4）：post_close ④ 段串联 _evolve_trailing_stops ——
        load_plan(today) → entry_date 算 holding_days → compute_stop_price → 写回 plan.stop_price。
        次日 stop_loss 读演进后的 stop（盘中不调，spec「盘中不调整 stop」红线）。
    """
    from trading import position_book

    today = datetime.now().strftime("%Y-%m-%d")
    _seed_breaker_baseline(today, 1_000_000.0)
    # plan 有持仓 order（neckline/atr 齐全，可演进）
    orders = [{"order": {"symbol": "A.SH"}, "neckline": 10.0, "atr": 0.5, "stop_price": 9.0}]
    trading_plan.save_plan(today, orders)
    # A.SH 已成交（entry_dates 有）+ holding_days=7>grace=5 → 收紧
    monkeypatch.setattr(position_book, "get_entry_dates",
                        lambda **kw: {"A.SH": "2099-01-01"})
    monkeypatch.setattr(engine, "_trading_days_between", lambda s, e: 7)

    class _FakeGw:
        async def query_asset(self):
            return {"total_asset": 1_000_000.0}  # 0% 不熔断
        async def _fetch_broker_positions(self):
            return {}
    monkeypatch.setattr(engine, "_cancel_all_open_orders", _no_op_cancel)
    from trading.compute.reconcile import ReconciliationResult
    async def _fake_rec(gw, local, tolerance=0.0):
        return ReconciliationResult([], [], [], [], 0.0, True)
    monkeypatch.setattr(engine.reconcile_job, "run_reconcile", _fake_rec)

    result = asyncio.run(engine.post_close(today, gw=_FakeGw(), local_positions={}))
    assert result.get("trailing_evolved") == 1
    # 验 plan 已写回演进后的 stop（holding_days=7 → 9.6）
    plan = trading_plan.load_plan(today)
    assert plan["orders"][0]["stop_price"] == 9.6


# ============================================================================
# 4.10 W3.4（broker 权威对账）：post_close ① reconcile（broker 权威）+ ② CSV 归因展示
# ============================================================================
def test_post_close_query_trades_reconcile_drift(monkeypatch):
    """post_close ② CSV 归因展示：CSV 流水聚合 vs position_book 出入 → 仅日志展示，不重写账本。

    W3.4 物理意图（08-04 事故根因修复）：
        fill 表/CSV 空可能是「网关断线无回报」而非「真无成交」，post_close 不能用 fill/CSV
        重写 position（否则与柜台漂移）。broker query_stock_positions 才是持仓权威；fill/CSV
        只解释「今日变动归因」。故 ② 段降级为展示，drift 真相源唯一在 ① broker。
    """
    from trading import position_book
    # position_book 记 30（apply_fill 正确记账）；CSV 聚合返 100（疑似漏回报或重复）
    position_book.apply_fill("ord1", "A.SH", "BUY", 30, 10.0, "2099-01-01 10:00:00")
    today = datetime.now().strftime("%Y-%m-%d")
    _seed_breaker_baseline(today, 1_000_000.0)   # 熔断基线（0% 不触发）

    # mock service.aggregate_fills_by_symbol 返 100（与账本 30 不一致——归因展示用）
    def _fake_agg(start, end):
        return {"A.SH": 100.0}
    monkeypatch.setattr(
        "presentation.server.services.trading_service.aggregate_fills_by_symbol", _fake_agg)

    class _FakeGw:
        async def query_asset(self):
            return {"total_asset": 1_000_000.0}
        async def _fetch_broker_positions(self):
            return {}
    monkeypatch.setattr(engine, "_cancel_all_open_orders", _no_op_cancel)
    from trading.compute.reconcile import ReconciliationResult
    async def _fake_rec(gw, local, tolerance=0.0):
        return ReconciliationResult([], [], [], [], 0.0, True)
    monkeypatch.setattr(engine.reconcile_job, "run_reconcile", _fake_rec)

    result = asyncio.run(engine.post_close(today, gw=_FakeGw(), local_positions={}))

    # W3.4：CSV 归因展示计入 trades_attribution（不再叫 trades_reconciled）
    assert result.get("trades_attribution") == 1
    # W3.4 红线：position_book 维持 30，绝不被 CSV 重写成 100（CSV 不再是权威）
    assert position_book.get_local_positions().get("A.SH") == 30.0


def test_post_close_query_trades_no_drift_is_noop(monkeypatch):
    from trading.compute.reconcile import ReconciliationResult
    """post_close：CSV 聚合 == position_book → 无归因出入（trades_attribution 不设）。"""
    from trading import position_book
    db_path = os.path.join(os.environ["TRADE_PLAN_DIR"], "..", "state.db")
    monkeypatch.setattr(position_book, "_DEFAULT_DB", db_path)
    position_book.init_db()
    position_book.apply_fill("ord1", "A.SH", "BUY", 100, 10.0, "2099-01-01 10:00:00")
    today = datetime.now().strftime("%Y-%m-%d")
    # T9 M-1 收口（final review 建议）：基线预置迁 _seed_breaker_baseline，与生产同口径
    # （account_daily，C-1 后熔断读口）。原 position_book.snapshot_start_equity 写 daily_equity
    # 死表——该测试虽不测熔断（mock _fake_rec is_ok=True），但口径不一致，防未来复用假 PASS。
    _seed_breaker_baseline(today, 1_000_000.0)

    def _fake_agg(start, end):
        return {"A.SH": 100.0}
    monkeypatch.setattr(
        "presentation.server.services.trading_service.aggregate_fills_by_symbol", _fake_agg)
    class _FakeGw:
        async def query_asset(self):
            return {"total_asset": 1_000_000.0}
        async def _fetch_broker_positions(self):
            return {}
    monkeypatch.setattr(engine, "_cancel_all_open_orders", _no_op_cancel)
    async def _fake_rec(gw, local, tolerance=0.0):
        return ReconciliationResult([], [], [], [], 0.0, True)
    monkeypatch.setattr(engine.reconcile_job, "run_reconcile", _fake_rec)

    result = asyncio.run(engine.post_close(today, gw=_FakeGw(), local_positions={}))

    assert "trades_attribution" not in result      # 无出入不设归因
    assert position_book.get_local_positions().get("A.SH") == 100.0   # 账本未改


def test_post_close_query_trades_skipped_when_no_gw(monkeypatch):
    """post_close：gw=None（dry_run）→ 跳过 CSV 归因展示（无网关无成交可归因）。

    物理意图（边界）：dry_run 下 gw=None，无真实成交，CSV 归因展示无意义（且避免无网关时
        误读 CSV 老数据产生误导日志）。gw=None 跳过 ② 段，与 ① reconcile 同口径。
    """
    from trading import position_book
    # 隔离 position_book db（防读生产账本 + 误归零真实持仓——live 前影子数据亦不应被测试污染）
    db_path = os.path.join(os.environ["TRADE_PLAN_DIR"], "..", "state.db")
    monkeypatch.setattr(position_book, "_DEFAULT_DB", db_path)
    position_book.init_db()
    # 确保 gw 全程 None：post_close 内部 ``if gw is None: gw = get_gateway()`` 兜底也要返 None，
    # 模拟 dry_run 无网关（否则若测试环境残留 gw 单例会误触发 ② 段读 CSV 产误导日志）。
    monkeypatch.setattr(engine, "get_gateway", lambda: None)

    agg_calls = []

    def _fake_agg(*a, **kw):
        agg_calls.append(True)
        return {}

    monkeypatch.setattr(
        "presentation.server.services.trading_service.aggregate_fills_by_symbol", _fake_agg)

    today = datetime.now().strftime("%Y-%m-%d")
    result = asyncio.run(engine.post_close(today))   # gw=None → get_gateway 也 None

    assert agg_calls == []                             # gw=None → 未调 aggregate_fills
    assert "trades_reconciled" not in result
    # M-1（Task 8 review fix）：gw=None 跳过 ② 段整段，新 key trades_attribution 也不应存在
    # （旧测只断言旧 key trades_reconciled，W3.4 改名后新 key 漏断言 → 补齐锁死跳过语义）。
    assert "trades_attribution" not in result


# ============================================================================
# Task D1（live-mainchain-fixes）：止盈差额补挂（#4）
# ============================================================================
def test_partial_fill_tp_diff_no_oversell_no_gap(monkeypatch, tmp_path):
    """3 笔部分成交（300 股）：TP 差额补挂，总量=目标量，不超卖、无覆盖缺口（#4）。"""
    import asyncio
    from trading import state_store
    from trading.engine import place_take_profit

    monkeypatch.setattr(state_store, "_DEFAULT_DB", str(tmp_path / "state.db"))
    state_store.init_store()
        # 日期用 clock.today() 而非硬编码（2026-08-02 起硬编码 8/1 与生产 today 错位 → 幂等查不到）
    from trading import clock
    aid, today, sym = "TEST_ACC", clock.today(), "600000.SH"
    state_store.upsert_account(aid, broker="qmt")
    state_store.insert_order(f"{today}_{sym}_OPEN_7", f"{aid}_{sym}_{today}", aid, today, sym,
                             "buy", "OPEN", 300, 10.0, broker_oid="987654", state="SUBMITTED")
    monkeypatch.setattr("trading.engine.trading_plan.load_plan",
                        lambda d: {"orders": [{"order": {"symbol": sym},
                                               "take_profit": 12.0, "tp1": 11.0,
                                               "tp1_portion": 0.5}]})
    submit_calls = []
    async def _fake_submit(order, **kw):
        submit_calls.append((order.symbol, order.side, order.qty, order.price))
        return {"order_id": f"seq{len(submit_calls)}", "state": "SUBMITTED"}
    monkeypatch.setattr("trading.engine._submit", _fake_submit)
    # 分 3 笔成交：累计 100/200/300
    for filled in (100, 200, 300):
        state_store.update_order_state_by_broker_oid(
            "987654", state="PARTIAL" if filled < 300 else "FILLED",
            filled_qty=float(filled), filled_price=10.5)
        asyncio.run(place_take_profit(sym, float(filled), 10.5, "987654"))
    tp_sells = [q for _, side, q, _ in submit_calls if side == "sell"]
    assert sum(tp_sells) == 300, f"TP 卖单总量应=持仓 300，实际 {sum(tp_sells)}（{tp_sells}）"
    assert tp_sells == [100, 100, 100], f"差额补挂应逐笔补 100，实际 {tp_sells}"
    # 已挂量 = 目标量后再触发（重推）→ 零 submit
    before = len(submit_calls)
    asyncio.run(place_take_profit(sym, 300.0, 10.5, "987654"))
    assert len(submit_calls) == before, "已挂满后再触发不得重复 submit"


def test_tp_single_leg_portion_zero_incremental(monkeypatch, tmp_path):
    """tp1_portion=0 退化单腿 TP2：分 3 笔补挂合计 300，不重复。"""
    import asyncio
    from trading import state_store
    from trading.engine import place_take_profit

    monkeypatch.setattr(state_store, "_DEFAULT_DB", str(tmp_path / "state.db"))
    state_store.init_store()
        # 日期用 clock.today() 而非硬编码（2026-08-02 起硬编码 8/1 与生产 today 错位 → 幂等查不到）
    from trading import clock
    aid, today, sym = "TEST_ACC", clock.today(), "600000.SH"
    state_store.upsert_account(aid, broker="qmt")
    state_store.insert_order(f"{today}_{sym}_OPEN_7", f"{aid}_{sym}_{today}", aid, today, sym,
                             "buy", "OPEN", 300, 10.0, broker_oid="987654", state="SUBMITTED")
    monkeypatch.setattr("trading.engine.trading_plan.load_plan",
                        lambda d: {"orders": [{"order": {"symbol": sym},
                                               "take_profit": 12.0, "tp1": None,
                                               "tp1_portion": 0.0}]})
    submit_calls = []
    async def _fake_submit(order, **kw):
        submit_calls.append(order.qty)
        return {"order_id": f"seq{len(submit_calls)}", "state": "SUBMITTED"}
    monkeypatch.setattr("trading.engine._submit", _fake_submit)
    for filled in (100, 200, 300):
        asyncio.run(place_take_profit(sym, float(filled), 10.5, "987654"))
    assert submit_calls == [100, 100, 100] and sum(submit_calls) == 300


# ============================================================================
# Task E3（live-mainchain-fixes）：超期平仓 DB 幂等防重（#7）
# ============================================================================
def test_close_expired_positions_skips_already_placed(monkeypatch, tmp_path):
    """已挂 EXPIRED_CLOSE 的 sym 不再重复 submit（#7 窄窗口卖超）。"""
    import asyncio
    from unittest.mock import AsyncMock
    from trading import state_store
    from trading.engine import _close_expired_positions

    monkeypatch.setattr(state_store, "_DEFAULT_DB", str(tmp_path / "state.db"))
    state_store.init_store()
        # 日期用 clock.today() 而非硬编码（2026-08-02 起硬编码 8/1 与生产 today 错位 → 幂等查不到）
    from trading import clock
    aid, today, sym = "TEST_ACC", clock.today(), "600000.SH"
    state_store.upsert_account(aid, broker="qmt")
    state_store.insert_order(f"{today}_{sym}_EXPIRED_CLOSE_1", f"{aid}_{sym}_{today}",
                             aid, today, sym, "sell", "EXPIRED_CLOSE", 100, 9.5,
                             state="SUBMITTED")
    gw = AsyncMock()
    gw._fetch_broker_positions = AsyncMock(return_value={sym: {"volume": 100, "avg_price": 10.0}})
    monkeypatch.setattr("trading.engine.qmt_market_data.get_quotes",
                        AsyncMock(return_value={sym: {"low_limit": 9.5, "last_price": 9.8}}))
    submit_calls = []
    async def _fake_submit(order, **kw):
        submit_calls.append(order.symbol)
        return {"order_id": "x", "state": "SUBMITTED"}
    monkeypatch.setattr("trading.engine._submit", _fake_submit)
    monkeypatch.setenv("QMT_ACCOUNT_ID", aid)  # _resolve_account_id 与幂等行同账户
    res = asyncio.run(_close_expired_positions(gw, [{"symbol": sym, "entry_date": "2026-06-15",
                                                     "holding_days": 20, "max_holding": 15}]))
    assert res["closed"] == 0
    assert submit_calls == [], "已挂 EXPIRED_CLOSE 不得重复 submit"


# ============================================================================
# SSoT Phase B · B2b：engine BUY 成交路径接线 record_position_attribution
# ============================================================================
# 物理意图（spec §5 B2）：
#   原 record_position_attribution 全仓无生产调用方（仅 trading_service 定义）。
#   B2 在 engine._handle_order_update 的 apply_fill_to_position 后接线：BUY 成交写归因。
#   不接线则「重启后归因不丢」验收无数据来源（apply_fill 只写 qty/avg_price，不写归因）。
#   SELL 不调 clear：apply_fill_to_position 归零删 position 行（state_store.py:676），
#   归因随行消失（Resolution：position 行删除即归因消失，非 clear 调用）。
def test_buy_fill_records_attribution(tmp_db, monkeypatch):
    """BUY 成交（首次 fill）→ record_position_attribution 落 position.strategy == "neckline"。

    验收：apply_fill_to_position 后接线 record_position_attribution，
    position 行 strategy 列 = "neckline"、entry_rationale 含"成交建仓@<traded_time>"。
    """
    from unittest.mock import MagicMock, AsyncMock, patch
    from trading import state_store
    from trading.engine import TradingEngine

    # tmp_db 已预置 ACC_TEST 账户（conftest fixture）
    monkeypatch.setenv("QMT_ACCOUNT_ID", "ACC_TEST")  # _resolve_account_id 返 ACC_TEST 与订单同账户
    eng = TradingEngine()
    update = {
        "kind": "trade", "order_id": "O_BUY_1", "stock_code": "600000.SH",
        "traded_volume": 100, "traded_price": 10.5, "traded_time": "20260805143025",
        "state": "FILLED",
    }
    eng._gw = MagicMock()
    eng._gw._orders = {}  # 方向来自 DB（#1 口径），不手塞 _orders
    state_store.insert_order(
        "2026-08-05_600000.SH_OPEN_1", "ACC_TEST_600000.SH_2026-08-05",
        "ACC_TEST", "2026-08-05", "600000.SH", "buy", "OPEN", 100, 10.0,
        broker_oid="O_BUY_1", state="SUBMITTED")

    fake_mgr = MagicMock()
    fake_mgr.notify_trade_event = AsyncMock(return_value=[])
    with patch("infra.notifier.NotificationManager") as NM:
        NM.get_default.return_value = fake_mgr
        with patch.object(eng, "_place_take_profit", new=AsyncMock()):
            asyncio.run(eng._handle_order_update(update))

    # 断言：position 行已建 + 归因已落（strategy/entry_rationale）
    row = state_store.get_position("ACC_TEST", "600000.SH")
    assert row is not None, "BUY 成交未建 position 行（apply_fill_to_position 未执行？）"
    assert row["strategy"] == "neckline", f"BUY 成交未落归因 strategy，实际 row={row}"
    assert "成交建仓" in (row["entry_rationale"] or ""), (
        f"entry_rationale 应含「成交建仓@<traded_time>」，实际 row={row}")


def test_buy_fill_attribution_failure_does_not_block(tmp_db, monkeypatch):
    """归因登记失败（trading_service 抛异常）→ 不阻断成交主路径（风控红线）。

    物理意图：成交是交易红线（必须落账），归因是审计（失败可补偿）。
    B2b 接线必须 try/except + logger.exception——归因异常不能让成交 handler 升 _CriticalHalt。
    """
    from unittest.mock import MagicMock, AsyncMock, patch
    from trading import state_store
    from trading.engine import TradingEngine

    eng = TradingEngine()
    update = {
        "kind": "trade", "order_id": "O_BUY_2", "stock_code": "600000.SH",
        "traded_volume": 100, "traded_price": 10.5, "traded_time": "20260805143100",
        "state": "FILLED",
    }
    eng._gw = MagicMock()
    eng._gw._orders = {}
    monkeypatch.setenv("QMT_ACCOUNT_ID", "ACC_TEST")  # _resolve_account_id 返 ACC_TEST 与订单同账户
    state_store.insert_order(
        "2026-08-05_600000.SH_OPEN_2", "ACC_TEST_600000.SH_2026-08-05",
        "ACC_TEST", "2026-08-05", "600000.SH", "buy", "OPEN", 100, 10.0,
        broker_oid="O_BUY_2", state="SUBMITTED")

    fake_mgr = MagicMock()
    fake_mgr.notify_trade_event = AsyncMock(return_value=[])

    # 让 record_position_attribution 抛异常（模拟归因 DB 写失败）
    def _boom(*a, **kw):
        raise RuntimeError("归因 DB 写失败模拟")
    with patch("infra.notifier.NotificationManager") as NM:
        NM.get_default.return_value = fake_mgr
        with patch.object(eng, "_place_take_profit", new=AsyncMock()), \
             patch("presentation.server.services.trading_service.record_position_attribution",
                   side_effect=_boom):
            # 不应抛异常（归因失败软降级，成交主路径继续）
            asyncio.run(eng._handle_order_update(update))

    # 成交主路径仍生效（position 行已建，只是归因未落）
    row = state_store.get_position("ACC_TEST", "600000.SH")
    assert row is not None, "归因失败不应阻断 apply_fill_to_position（成交红线优先）"
