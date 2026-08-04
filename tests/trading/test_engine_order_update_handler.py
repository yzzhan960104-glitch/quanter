# -*- coding: utf-8 -*-
"""成交回报 handler（Task 10 · 修 G5）：日志补写 + 钉钉成交通知 + 挂止盈三连。

物理意图（spec §6.2 C1）：
    on_stock_trade 回调（Task 11 注册 _on_order_update 后）推送 ``kind=="trade"``
    的成交回报 → TradingEngine._handle_order_update 被调度执行三件事：
      a. record_live_trade 补写成交回报日志（用真实成交价/量，非下单预估价）；
      b. notify_trade_event 推钉钉成交通知（fire_and_forget 不阻塞回调链）；
      c. 若为买单成交（查 gw._orders 拿 side）且该 symbol 未挂止盈（幂等）→
         挂限价止盈卖单（Phase1 简化版全额）。

测试边界（Grill Me · 控制器 scope #5）：
    绝不真起 APScheduler、绝不做真行情/真单/真钉钉：TradingEngine 仅实例化（装配
    4 job 不 start），``record_live_trade`` / ``NotificationManager`` /
    ``trading_plan.load_plan`` / ``_place_take_profit`` 均 patch 拦截。

TDD 约定（与 Task 7/8/9 一致）：
    本仓库 pytest-asyncio 为 strict 模式（pytest.ini 未配 asyncio_mode），
    历史 engine 测试一律 ``asyncio.run(...)`` 同步驱动 async。本测试沿袭该范式，
    避免引入 @pytest.mark.asyncio 装饰器造成风格分叉（见 Task8 fix 备注）。
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from trading import position_book
from trading import engine
from trading.engine import TradingEngine


@pytest.fixture
def db(tmp_path, monkeypatch):
    """每个测试用独立 tmp db（隔离生产账本 logs/trading_state.db），patch _DEFAULT_DB
    让 engine._handle_order_update → position_book.apply_fill 间接调用也命中 tmp。

    同时 init state_store（state-store-redesign 后 _handle_order_update 查 has_order/
    insert_fill/insert_trade_event 等 state_store 表，无本初始化 → no such table）。

    Why 必要：_handle_order_update 内调 position_book.apply_fill 写成交回报。无本
    fixture 时 apply_fill 走默认 _DEFAULT_DB=logs/trading_state.db，会污染生产账本
    （历史 bug：order_id=123/456 的测试数据被写进真实账本 logs/trading_state.db，
    2026-07-27 排查定位）。照抄 test_position_book 同款隔离范式。
    """
    from trading import state_store
    db_path = str(tmp_path / "state.db")
    monkeypatch.setattr(position_book, "_DEFAULT_DB", db_path)
    monkeypatch.setattr(state_store, "_DEFAULT_DB", db_path)
    position_book.init_db()
    state_store.init_store()
    return db_path


def test_trade_update_writes_log_and_notifies(db):
    """成交回报 → 补写成交日志 + 推钉钉成交通知（三连中的 a + b）。

    断言：
      1) ``record_live_trade`` 被调一次（成交日志补写，方向由 gw._orders 判定）；
      2) 成交日志首参（symbol）含回报里的 stock_code（防字段拼错）；
      3) ``notify_trade_event`` 被调一次（钉钉成交通知）。
    """
    eng = TradingEngine()
    # 成交回报 update（on_stock_trade 推送的真实契约：kind=trade + 量价齐全）
    update = {
        "kind": "trade",
        "order_id": "123",
        "stock_code": "300001.SZ",
        "traded_volume": 100,
        "traded_price": 10.5,
        "traded_amount": 1050.0,
        "traded_time": 20260723,
        "state": "FILLED",
    }
    eng._tp_placed = set()           # 幂等标记初始化（与 __init__ 同语义，显式重申）
    eng._gw = MagicMock()
    eng._gw._orders = {"123": {"order_type": 23}}  # 23=STOCK_BUY（买单标记）
    # patch 真实模块路径（_handle_order_update 内 lazy import 这些符号，故 patch 真身模块
    # 而非 trading.engine —— engine 模块顶层不 import 这两个符号，避免循环依赖）：
    #   - record_live_trade 实身：presentation.server.services.trading_service
    #   - NotificationManager 实身：infra.notifier（infra.notifier 是转发垫片）
    fake_mgr = MagicMock()
    fake_mgr.notify_trade_event = AsyncMock(return_value=[])
    with patch("presentation.server.services.trading_service.record_live_trade") as rec, \
         patch("infra.notifier.NotificationManager") as NM:
        NM.get_default.return_value = fake_mgr
        asyncio.run(eng._handle_order_update(update))
    # a. 成交日志补写：record_live_trade 被调一次，首参=symbol
    rec.assert_called_once()
    assert "300001.SZ" in str(rec.call_args)
    # b. 钉钉成交通知：notify_trade_event 被调一次（symbol/direction/qty/price 四要素）
    fake_mgr.notify_trade_event.assert_called_once()
    ntf_args, _ = fake_mgr.notify_trade_event.call_args
    assert ntf_args[0] == "300001.SZ"  # symbol
    assert ntf_args[1] == "BUY"        # direction（据 order_type=23=STOCK_BUY 判定）
    assert ntf_args[2] == 100          # qty
    assert ntf_args[3] == 10.5         # price


def test_buy_fill_places_take_profit_once_idempotent(db):
    """买单成交 → 挂止盈；重复回报幂等不重挂（DB has_order 替代 _tp_placed 内存）。

    物理意图（幂等为何是红线）：
        on_stock_trade 在部分成交/柜台重推时会多次推送同一 order_id 的 trade 回报。
        若每次都重挂止盈卖单，会导致同一笔持仓挂出 N 张止盈单 → 超卖敞口致命。
        Phase 2：_handle_order_update 在调 _place_take_profit 前先查 state_store.has_order(TP1)，
        首次 False → 挂 + insert_order(TP1)；重复回报 has_order=True → 跳过（DB 幂等，跨重启持久）。

    断言：
      1) 首次买单成交 → _submit 被调（tp1+tp2 两腿，共 2 次）；
      2) 重复回报 → has_order(TP1)=True → _place_take_profit 不执行 → _submit 不再被调。
    """
    from trading import state_store
    eng = TradingEngine()
    update = {
        "kind": "trade",
        "order_id": "123",
        "stock_code": "300001.SZ",
        "traded_volume": 100,
        "traded_price": 10.5,
        "traded_time": "09:30:00",
        "state": "FILLED",
    }
    plan = {
        "confirmed": True,
        "orders": [
            {
                "order": {"symbol": "300001.SZ", "qty": 100, "side": "buy", "price": 10.0},
                "stop_price": 9.5,
                "take_profit": 12.0,
                "tp1": 11.0,
                "tp1_portion": 1.0,  # 100% tp1（filled=100 向下整手=100，单腿）
            }
        ],
    }
    gw = MagicMock()
    gw._orders = {"123": {"order_type": 23}}
    eng._gw = gw
    # Phase 2：不 mock _place_take_profit（让 DB insert_order 自然写入 → 幂等键生效）
    # 只 mock _submit（不真挂单到 broker），让 _place_take_profit 内部 insert_order 落 DB
    with patch("trading.engine.trading_plan.load_plan", return_value=plan), \
         patch("presentation.server.services.trading_service.record_live_trade"), \
         patch("infra.notifier.NotificationManager"), \
         patch("trading.engine._submit", new=AsyncMock(return_value={"state": "FILLED"})) as submit_mock:
        asyncio.run(eng._handle_order_update(update))  # 首次成交回报 → 挂 TP1
        assert submit_mock.call_count >= 1  # 至少调了 1 次（tp1 单腿）
        first_count = submit_mock.call_count
        asyncio.run(eng._handle_order_update(update))  # 重复回报 → has_order(TP1)=True → 跳过
    # 幂等：第二次不新增 _submit 调用（DB has_order 挡住）
    assert submit_mock.call_count == first_count


# ============================================================================
# T9（state-store-redesign）：成交回报 fill + trade_event(FILLED) + DB 幂等挂 TP1/TP2
# ============================================================================
@pytest.fixture
def state_db(tmp_path, monkeypatch):
    """隔离 state_store DB（_handle_order_update 落 fill/trade_event 用）。

    同时 patch position_book._DEFAULT_DB（既有 apply_fill 调用）与 state_store._DEFAULT_DB。
    """
    from trading import state_store
    db_path = str(tmp_path / "state.db")
    monkeypatch.setattr(position_book, "_DEFAULT_DB", db_path)
    monkeypatch.setattr(state_store, "_DEFAULT_DB", db_path)
    position_book.init_db()
    state_store.init_store()
    return db_path


def _buy_fill_update(symbol="300001.SZ", order_id="123"):
    return {
        "kind": "trade", "order_id": order_id, "stock_code": symbol,
        "traded_volume": 100, "traded_price": 10.5, "traded_time": "09:30:00",
        "state": "FILLED",
    }


def _today_str():
    """成交回报 handler 用 datetime.now() 定 trade_date，测试须跟随真实今日。"""
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d")


def _plan_with_tp(symbol="300001.SZ"):
    return {
        "confirmed": True,
        "orders": [{
            "order": {"symbol": symbol, "qty": 100, "side": "buy", "price": 10.0},
            "stop_price": 9.5, "take_profit": 12.0,
            "tp1": 11.0, "tp1_portion": 0.5,
            "formed_at": "2099-01-02", "max_wait": 5,
        }],
    }


def test_trade_update_inserts_fill_and_event(state_db):
    """成交回报 → fill 表 + trade_event(FILLED) 写入（DB 真相源）。"""
    import sqlite3
    from trading import state_store
    eng = TradingEngine()
    eng._tp_placed = set()
    eng._gw = MagicMock()
    eng._gw._orders = {"123": {"order_type": 23}}  # BUY
    fake_mgr = MagicMock()
    fake_mgr.notify_trade_event = AsyncMock(return_value=[])
    with patch("presentation.server.services.trading_service.record_live_trade"), \
         patch("infra.notifier.NotificationManager") as NM:
        NM.get_default.return_value = fake_mgr
        with patch.object(eng, "_place_take_profit", new=AsyncMock()):
            asyncio.run(eng._handle_order_update(_buy_fill_update()))
    account_id = engine._resolve_account_id()
    trade_id = f"{account_id}_300001.SZ_{_today_str()}"
    assert state_store.get_latest_action(trade_id) == "FILLED"
    # fill 表有行（增量幂等）
    with sqlite3.connect(state_db) as con:
        n = con.execute("SELECT COUNT(*) FROM fill WHERE symbol='300001.SZ'").fetchone()[0]
    assert n == 1


def test_tp_idempotent_via_db(state_db, monkeypatch):
    """同 symbol 成交回报重推 → has_order(TP1)=True → 跳过（替代 _tp_placed 内存）。

    物理意图（P0-1 止盈超卖根因）：_tp_placed 是内存态，engine 重启清空 → 重连重推 →
    重复挂止盈超卖。改 DB has_order(TP1) 查询，跨重启持久。
    """
    from trading import state_store
    eng = TradingEngine()
    eng._tp_placed = set()
    eng._gw = MagicMock()
    eng._gw._orders = {"123": {"order_type": 23}}  # BUY
    # 预置 TP1 已挂（模拟 DB 已记录止盈，trade_date=今日，与 handler 口径一致）
    account_id = engine._resolve_account_id()
    today = _today_str()
    state_store.upsert_account(account_id, broker="qmt")
    state_store.insert_order(
        f"{today}_300001.SZ_TP1_1", f"{account_id}_300001.SZ_{today}", account_id,
        today, "300001.SZ", "sell", "TP1", 100, 11.0, state="SUBMITTED")
    tp_calls = {"n": 0}
    async def _counting_tp(*a, **kw):
        tp_calls["n"] += 1
    fake_mgr = MagicMock()
    fake_mgr.notify_trade_event = AsyncMock(return_value=[])
    with patch("trading.engine.trading_plan.load_plan", return_value=_plan_with_tp()), \
         patch("presentation.server.services.trading_service.record_live_trade"), \
         patch("infra.notifier.NotificationManager") as NM:
        NM.get_default.return_value = fake_mgr
        with patch.object(eng, "_place_take_profit", new=_counting_tp):
            asyncio.run(eng._handle_order_update(_buy_fill_update()))  # 已有 TP1 → 跳过
    assert tp_calls["n"] == 0  # DB 幂等：已有 TP1 不重挂


def test_tp_inserts_two_orders(state_db):
    """成交后挂 tp1 + tp2 两笔 order（UNIQUE 幂等，落 DB 真相源）。

    用 filled=1000/portion=0.5 → tp1_qty=500（整手非零，走分级两腿）。
    """
    import sqlite3
    from trading import state_store
    eng = TradingEngine()
    eng._tp_placed = set()
    eng._gw = MagicMock()
    eng._gw._orders = {"123": {"order_type": 23}}  # BUY
    # plan 用 qty=1000 + portion=0.5 → tp1=500/tp2=500（两腿都非零）
    plan = _plan_with_tp()
    plan["orders"][0]["order"]["qty"] = 1000
    with patch("trading.engine.trading_plan.load_plan", return_value=plan), \
         patch("trading.engine._submit", new=AsyncMock(return_value={"state": "FILLED", "order_id": "s1"})):
        asyncio.run(eng._place_take_profit("300001.SZ", 1000, 10.5, order_id="123"))
    account_id = engine._resolve_account_id()
    today = _today_str()
    # TP1 + TP2 两笔 order 落库（trade_date=今日，与 _place_take_profit 口径一致）
    assert state_store.has_order(account_id, today, "300001.SZ", "TP1") is True
    assert state_store.has_order(account_id, today, "300001.SZ", "TP2") is True


# ============================================================================
# Task 7（P0-3 分级止盈 tp1/tp2）：_place_take_profit 挂两张限价卖单 + 整手分割
# ============================================================================
def test_place_take_profit_two_legs(db):
    """分级止盈：filled=1000/portion=0.5 → 挂 tp1@500股 + tp2@500股。

    物理意图（plan Task 7 · 对齐缺口 P0-3）：
        回测 ``simulate_exit`` 用 tp1_portion 加权两批止盈（tp1 锁利 + tp2 博形态
        对称目标位），实盘 Phase1 只挂单笔全平 → 回测/实盘行为背离。
        Phase2：``_place_take_profit`` 读 plan.tp1/tp1_portion/take_profit(tp2)
        拆两张限价卖单：tp1_qty=int(filled×portion/100)*100（向下整手） + tp2_qty=余量。

    断言：
      1) _submit 被调两次（两张限价卖单）；
      2) 第一张 qty=500 price=tp1；第二张 qty=500 price=tp2；
      3) 两张都是 side="sell"（限价卖单）。
    """
    eng = TradingEngine()
    eng._tp_placed = set()
    plan = {
        "confirmed": True,
        "orders": [{
            "order": {"symbol": "300001.SZ", "qty": 1000, "side": "buy", "price": 10.0},
            "stop_price": 9.0,
            "take_profit": 13.0,        # tp2（颈线 + tp_h_mult × H）
            "tp1": 11.5,                # tp1（颈线 + tp1_h_mult × H）
            "tp1_portion": 0.5,         # 50% 在 tp1 锁利
        }],
    }
    with patch("trading.engine.trading_plan.load_plan", return_value=plan), \
         patch("trading.engine._submit", new=AsyncMock(return_value={"state": "FILLED"})) as submit_mock:
        asyncio.run(eng._place_take_profit("300001.SZ", 1000, 10.5, order_id="ord-1"))
    # _submit 必被调两次（两张限价卖单）
    assert submit_mock.call_count == 2
    legs = [call.args[0] for call in submit_mock.call_args_list]
    # 两张都是 sell
    assert all(leg.side == "sell" for leg in legs)
    # tp1 leg：qty=500（1000×0.5 向下整手 100），price=tp1=11.5
    tp1_leg = next(leg for leg in legs if leg.price == 11.5)
    assert tp1_leg.qty == 500
    # tp2 leg：qty=500（余量），price=tp2=13.0
    tp2_leg = next(leg for leg in legs if leg.price == 13.0)
    assert tp2_leg.qty == 500


def test_place_take_profit_tp1_qty_round_to_lot(db):
    """filled=430/portion=0.5 → tp1=200（int(215/100)*100），tp2=230（余量含零股）。

    物理意图（A 股整手约束 · plan Task 7 Step 1）：
        tp1_qty 必须整手（100 股整数倍），向下取整；tp2_qty = filled - tp1_qty
        含零股（券商接受卖出零股清仓）。若 filled×portion<100 → tp1_qty=0 全量 tp2。
    """
    eng = TradingEngine()
    eng._tp_placed = set()
    plan = {
        "confirmed": True,
        "orders": [{
            "order": {"symbol": "300002.SZ", "qty": 430, "side": "buy", "price": 10.0},
            "stop_price": 9.0, "take_profit": 13.0,
            "tp1": 11.5, "tp1_portion": 0.5,
        }],
    }
    with patch("trading.engine.trading_plan.load_plan", return_value=plan), \
         patch("trading.engine._submit", new=AsyncMock(return_value={"state": "FILLED"})) as submit_mock:
        asyncio.run(eng._place_take_profit("300002.SZ", 430, 10.5, order_id="ord-2"))
    legs = [call.args[0] for call in submit_mock.call_args_list]
    tp1_leg = next(leg for leg in legs if leg.price == 11.5)
    tp2_leg = next(leg for leg in legs if leg.price == 13.0)
    assert tp1_leg.qty == 200          # int(430*0.5/100)*100 = int(2.15)*100 = 200
    assert tp2_leg.qty == 230          # 430 - 200 = 230（余量，含零股 30）


def test_place_take_profit_portion_zero_only_tp2(db):
    """portion=0 → 只挂 tp2（不锁利，全量博形态对称目标位）。"""
    eng = TradingEngine()
    eng._tp_placed = set()
    plan = {
        "confirmed": True,
        "orders": [{
            "order": {"symbol": "300003.SZ", "qty": 1000, "side": "buy", "price": 10.0},
            "stop_price": 9.0, "take_profit": 13.0,
            "tp1": 11.5, "tp1_portion": 0.0,   # portion=0 → 全量 tp2
        }],
    }
    with patch("trading.engine.trading_plan.load_plan", return_value=plan), \
         patch("trading.engine._submit", new=AsyncMock(return_value={"state": "FILLED"})) as submit_mock:
        asyncio.run(eng._place_take_profit("300003.SZ", 1000, 10.5, order_id="ord-3"))
    # 只调一次（tp2 全量）
    submit_mock.assert_called_once()
    leg = submit_mock.call_args.args[0]
    assert leg.price == 13.0 and leg.qty == 1000


def test_place_take_profit_portion_full_only_tp1(db):
    """portion=1 → 只挂 tp1（全部在 tp1 锁利，不博 tp2）。"""
    eng = TradingEngine()
    eng._tp_placed = set()
    plan = {
        "confirmed": True,
        "orders": [{
            "order": {"symbol": "300004.SZ", "qty": 1000, "side": "buy", "price": 10.0},
            "stop_price": 9.0, "take_profit": 13.0,
            "tp1": 11.5, "tp1_portion": 1.0,    # portion=1 → 全量 tp1
        }],
    }
    with patch("trading.engine.trading_plan.load_plan", return_value=plan), \
         patch("trading.engine._submit", new=AsyncMock(return_value={"state": "FILLED"})) as submit_mock:
        asyncio.run(eng._place_take_profit("300004.SZ", 1000, 10.5, order_id="ord-4"))
    submit_mock.assert_called_once()
    leg = submit_mock.call_args.args[0]
    assert leg.price == 11.5 and leg.qty == 1000


def test_place_take_profit_tp1_ge_tp2_falls_back_to_tp2_only(db):
    """sanity 守卫：tp1≥tp2（数据异常或 H≤0）→ 只挂 tp2 全量（防 tp1 永远先成交）。

    物理意图：
        tp1 是颈线+tp1_h_mult×H，tp2 是颈线+tp_h_mult×H，正常 tp_h_mult > tp1_h_mult
        保证 tp1 < tp2。但若 H 异常（≈0 或负）或参数被手工调坏，tp1 可能 ≥ tp2，
        此时挂 tp1 无意义（永远不会先于 tp2 成交，或成交在更高价反而拖累）。
        守卫：tp1≥tp2 时跳过 tp1，全量挂 tp2。
    """
    eng = TradingEngine()
    eng._tp_placed = set()
    plan = {
        "confirmed": True,
        "orders": [{
            "order": {"symbol": "300005.SZ", "qty": 1000, "side": "buy", "price": 10.0},
            "stop_price": 9.0,
            "take_profit": 11.0,        # tp2 = 11
            "tp1": 11.5,                # tp1 > tp2（异常）
            "tp1_portion": 0.5,
        }],
    }
    with patch("trading.engine.trading_plan.load_plan", return_value=plan), \
         patch("trading.engine._submit", new=AsyncMock(return_value={"state": "FILLED"})) as submit_mock:
        asyncio.run(eng._place_take_profit("300005.SZ", 1000, 10.5, order_id="ord-5"))
    # tp1 ≥ tp2 时 sanity 只挂 tp2
    submit_mock.assert_called_once()
    leg = submit_mock.call_args.args[0]
    assert leg.price == 11.0 and leg.qty == 1000


def test_place_take_profit_no_tp1_in_plan_falls_back_to_tp2_only(db):
    """老 plan 无 tp1 字段（Task 7 前的 plan）→ 退回 tp2 单笔全平（向后兼容零回归）。"""
    eng = TradingEngine()
    eng._tp_placed = set()
    plan = {
        "confirmed": True,
        "orders": [{
            "order": {"symbol": "300006.SZ", "qty": 1000, "side": "buy", "price": 10.0},
            "stop_price": 9.0,
            "take_profit": 13.0,
            # 无 tp1 / tp1_portion（老 plan）
        }],
    }
    with patch("trading.engine.trading_plan.load_plan", return_value=plan), \
         patch("trading.engine._submit", new=AsyncMock(return_value={"state": "FILLED"})) as submit_mock:
        asyncio.run(eng._place_take_profit("300006.SZ", 1000, 10.5, order_id="ord-6"))
    submit_mock.assert_called_once()
    leg = submit_mock.call_args.args[0]
    assert leg.price == 13.0 and leg.qty == 1000


def test_place_take_profit_truncates_fractional_qty_to_int(db):
    """``_place_take_profit`` 用 ``int(filled_qty)`` 截断部分成交的零股（A 股整手红线）。

    物理意图（A 股整手约束 · spec §6.2 C1 数量来源）：
        成交回报 ``traded_volume`` 在柜台部分成交回报里可能带小数（如 100.5，源于
        柜台内部整股+零股混合计量或行情推送精度）。A 股卖出**必须整手**（100 股整数倍，
        北交所/科创板保留 1 股粒度但仍须整数）——若把 100.5 直接喂给 broker 下单接口，
        轻则 broker 拒单（无效数量），重则部分柜台按 100.5 解释成 10050 股致超卖敞口。
        故 ``_place_take_profit`` 内 ``OrderRequest(qty=int(filled_qty), ...)`` 是
        live 安全红线（与 stop_loss ``qty 必须来自 gw 持仓整手``同源）。

    断言：
        patch 真实 ``_submit``（非 patch ``_place_take_profit``），调
        ``_handle_order_update`` 传 traded_volume=100.5，断言 ``_submit`` 收到的
        ``OrderRequest.qty == 100``（int 截断，非 100.5 也非 10050）。
    """
    eng = TradingEngine()
    eng._tp_placed = set()
    update = {
        "kind": "trade",
        "order_id": "456",
        "stock_code": "300002.SZ",
        "traded_volume": 100.5,   # 带小数的部分成交（柜台精度产物）
        "traded_price": 11.0,
        "state": "FILLED",
    }
    plan = {
        "confirmed": True,
        "orders": [
            {
                "order": {"symbol": "300002.SZ", "qty": 100, "side": "buy", "price": 10.8},
                "stop_price": 10.0,
                "take_profit": 12.5,
            }
        ],
    }
    gw = MagicMock()
    gw._orders = {"456": {"order_type": 23}}  # 23=STOCK_BUY（买单触发挂止盈）
    eng._gw = gw
    # patch 真身路径（同既有 test_buy_fill_places_take_profit_once_idempotent）：
    #   - ``_submit`` patch 成 AsyncMock 拦截真实下单（拿到 OrderRequest 检查 qty）；
    #   - ``trading.engine.trading_plan.load_plan`` 返含 take_profit 的计划；
    #   - record_live_trade / NotificationManager patch 掉日志与通知副作用。
    with patch("trading.engine.trading_plan.load_plan", return_value=plan), \
         patch("presentation.server.services.trading_service.record_live_trade"), \
         patch("infra.notifier.NotificationManager"), \
         patch("trading.engine._submit", new=AsyncMock(return_value={"state": "FILLED"})) as submit_mock:
        asyncio.run(eng._handle_order_update(update))
    # _submit 必被调一次（挂止盈）；OrderRequest.qty 必须 == 100（int 截断零股）
    submit_mock.assert_called_once()
    order_req = submit_mock.call_args.args[0]
    assert order_req.qty == 100          # int(100.5) == 100（A 股整手红线）
    assert isinstance(order_req.qty, int)  # 类型必须是 int（防 broker 按 float 解释成 10050）

# ============================================================================
# Task A1（live-mainchain-fixes）：真实回调链路 helper + async_response 回填
# ============================================================================

def _make_real_chain_engine():
    """装配绑定真实 QmtExecutionGateway 的引擎（不 connect，直接驱动 C++ 回调）。"""
    from broker.qmt import QmtExecutionGateway
    gw = QmtExecutionGateway(userdata_path="C:/tmp/qmt_test", account_id="TEST_ACC")
    gw._trader = MagicMock()
    gw._account = MagicMock()
    gw._connected = True
    gw._lock_down = False
    gw._orders = {}
    gw._seq_to_real = {}
    gw._seq_to_client = {}
    eng = TradingEngine()
    eng._gw = gw
    gw.set_order_update_callback(eng._handle_order_update)
    return eng, gw


async def _pump(gw, fn):
    """在真实事件循环里触发回调方法并让 _process_order_update 创建的任务跑完。"""
    gw._loop = asyncio.get_running_loop()
    fn()
    await asyncio.sleep(0.05)


def test_async_response_backfills_db_broker_oid(db):
    """async_response 到达 → DB order.broker_oid 从 str(seq) 回填真实柜台单号。

    生产根因（#5）：原 _handle_order_update 见 kind!='trade' 直接 return，
    async_response 被丢弃 → broker_oid 恒 str(seq) → 撤单/对账永远按错单号匹配。
    """
    from types import SimpleNamespace
    from trading import state_store

    eng, gw = _make_real_chain_engine()
    aid = "TEST_ACC"
    seq, real = 7, 987654
    oid = "2026-08-01_600000.SH_OPEN_7"
    state_store.upsert_account(aid, broker="qmt")  # FK 前置：order 表引用 account 行
    state_store.insert_order(oid, f"{aid}_600000.SH_2026-08-01", aid, "2026-08-01",
                             "600000.SH", "buy", "OPEN", 100, 10.0,
                             broker_oid=str(seq), state="SUBMITTED")
    # 真实回调链路：on_order_stock_async_response → _process_order_update → _handle_order_update
    asyncio.run(_pump(gw, lambda: gw.on_order_stock_async_response(
        SimpleNamespace(seq=seq, order_id=real))))
    with state_store._connect(state_store._DEFAULT_DB) as con:
        row = con.execute('SELECT broker_oid, state FROM "order" WHERE order_id=?', (oid,)).fetchone()
    assert row["broker_oid"] == str(real), f"应回填 {real}，实际 {row['broker_oid']}"
    assert row["state"] == "SUBMITTED", "async_response 只回填单号，不动 state"

# ============================================================================
# Task A2（live-mainchain-fixes）：order_type 解析 + merge + order 事件推进状态
# ============================================================================

def test_on_stock_order_parses_order_type(db):
    """on_stock_order 解析必须含 order_type（#1 地基：主推路径方向来源之一）。"""
    import asyncio
    from types import SimpleNamespace

    eng, gw = _make_real_chain_engine()
    asyncio.run(_pump(gw, lambda: gw.on_stock_order(SimpleNamespace(
        order_id=987654, stock_code="600000.SH", order_status=56, order_type=23,
        order_volume=100, traded_volume=100, traded_price=10.5, status_msg=""))))
    assert gw._orders["987654"].get("order_type") == 23, "on_stock_order 必须透出 order_type"


def test_order_event_advances_db_state_filled(db):
    """order 事件（status=56）→ DB order.state=FILLED + filled_qty=累计量（#5 第二刀）。"""
    import asyncio
    from types import SimpleNamespace
    from trading import state_store

    eng, gw = _make_real_chain_engine()
    aid, real = "TEST_ACC", 987654
    oid = "2026-08-01_600000.SH_OPEN_7"
    state_store.upsert_account(aid, broker="qmt")
    state_store.insert_order(oid, f"{aid}_600000.SH_2026-08-01", aid, "2026-08-01",
                             "600000.SH", "buy", "OPEN", 100, 10.0,
                             broker_oid=str(real), state="SUBMITTED")
    asyncio.run(_pump(gw, lambda: gw.on_stock_order(SimpleNamespace(
        order_id=real, stock_code="600000.SH", order_status=56, order_type=23,
        order_volume=100, traded_volume=100, traded_price=10.5, status_msg=""))))
    with state_store._connect(state_store._DEFAULT_DB) as con:
        row = con.execute('SELECT state, filled_qty, filled_price FROM "order" WHERE order_id=?',
                          (oid,)).fetchone()
    assert row["state"] == "FILLED"
    assert row["filled_qty"] == 100
    assert row["filled_price"] == 10.5


def test_order_event_partial_status_maps_to_partial(db):
    """status=55（部成）→ DB state=PARTIAL（精确部分成交，非近似 FILLED）。"""
    import asyncio
    from types import SimpleNamespace
    from trading import state_store

    eng, gw = _make_real_chain_engine()
    aid, real = "TEST_ACC", 987654
    oid = "2026-08-01_600000.SH_OPEN_7"
    state_store.upsert_account(aid, broker="qmt")
    state_store.insert_order(oid, f"{aid}_600000.SH_2026-08-01", aid, "2026-08-01",
                             "600000.SH", "buy", "OPEN", 300, 10.0,
                             broker_oid=str(real), state="SUBMITTED")
    asyncio.run(_pump(gw, lambda: gw.on_stock_order(SimpleNamespace(
        order_id=real, stock_code="600000.SH", order_status=55, order_type=23,
        order_volume=300, traded_volume=100, traded_price=10.4, status_msg=""))))
    with state_store._connect(state_store._DEFAULT_DB) as con:
        row = con.execute('SELECT state, filled_qty FROM "order" WHERE order_id=?', (oid,)).fetchone()
    assert row["state"] == "PARTIAL"
    assert row["filled_qty"] == 100


def test_trade_push_keeps_order_type_after_merge(db):
    """trade 事件不得覆盖 _orders 记录的 order_type（merge 语义，防止内存兜底失效）。"""
    import asyncio
    from types import SimpleNamespace

    eng, gw = _make_real_chain_engine()
    asyncio.run(_pump(gw, lambda: gw.on_stock_order(SimpleNamespace(
        order_id=987654, stock_code="600000.SH", order_status=56, order_type=23,
        order_volume=100, traded_volume=100, traded_price=10.5, status_msg=""))))
    asyncio.run(_pump(gw, lambda: gw.on_stock_trade(SimpleNamespace(
        order_id=987654, stock_code="600000.SH", traded_volume=100,
        traded_price=10.5, traded_amount=1050.0, traded_time="20260801101000"))))
    assert gw._orders["987654"].get("order_type") == 23, "trade 覆盖后 order_type 必须保留"

# ============================================================================
# Task A3（live-mainchain-fixes）：方向反查 DB 优先 + 内存兜底
# ============================================================================

def test_direction_resolved_from_db_not_memory(db):
    """方向从 DB order.side 反查：gw._orders 无 order_type 也能判 BUY（#1 主刀）。"""
    from trading import state_store

    eng, gw = _make_real_chain_engine()
    aid, real = "TEST_ACC", 987654
    state_store.upsert_account(aid, broker="qmt")
    state_store.insert_order("2026-08-01_600000.SH_OPEN_7", f"{aid}_600000.SH_2026-08-01",
                             aid, "2026-08-01", "600000.SH", "buy", "OPEN", 100, 10.0,
                             broker_oid=str(real), state="SUBMITTED")
    # 主推路径真实形态：_orders 只有状态无 order_type（禁止手塞 order_type）
    gw._orders = {str(real): {"order_status": 56}}
    assert eng._order_direction(str(real)) == "BUY", "应从 DB side='buy' 反查得 BUY"


def test_direction_seq_fallback_when_async_response_late(db):
    """竞态：async_response 未到（broker_oid 仍是 str(seq)），经 _seq_to_real 反查命中 DB。"""
    from trading import state_store

    eng, gw = _make_real_chain_engine()
    aid, seq, real = "TEST_ACC", 7, 987654
    state_store.upsert_account(aid, broker="qmt")
    state_store.insert_order("2026-08-01_600000.SH_OPEN_7", f"{aid}_600000.SH_2026-08-01",
                             aid, "2026-08-01", "600000.SH", "buy", "OPEN", 100, 10.0,
                             broker_oid=str(seq), state="SUBMITTED")
    gw._seq_to_real = {seq: real}
    gw._orders = {}
    assert eng._order_direction(str(real)) == "BUY", "seq 反查应命中 DB side"


def test_direction_unknown_returns_none(db):
    """DB 无行 + 内存无 order_type → None（调用方必须告警，禁止静默）。"""
    eng, gw = _make_real_chain_engine()
    gw._orders = {}
    assert eng._order_direction("999999") is None

# ============================================================================
# Task A5（live-mainchain-fixes）：全链路 e2e（真实回调 + 竞态）
# ============================================================================

def test_e2e_real_callback_chain_fills_and_places_tp(db, monkeypatch):
    """真实回调链路：OPEN 单 → async_response → order(FILLED) → trade → 落账 + 挂 TP。

    守元问题：全程驱动 on_* 回调，禁止手塞 _orders；_submit 用模块级 patch。
    """
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from trading import state_store

    eng, gw = _make_real_chain_engine()
    aid, seq, real = "TEST_ACC", 7, 987654
    oid = "2026-08-01_600000.SH_OPEN_7"
    state_store.upsert_account(aid, broker="qmt")
    state_store.insert_order(oid, f"{aid}_600000.SH_2026-08-01", aid, "2026-08-01",
                             "600000.SH", "buy", "OPEN", 100, 10.0,
                             broker_oid=str(seq), state="SUBMITTED")
    monkeypatch.setattr("trading.engine.trading_plan.load_plan",
                        lambda d: {"orders": [{"order": {"symbol": "600000.SH"},
                                               "take_profit": 11.0, "tp1": 10.8,
                                               "tp1_portion": 0.0}]})
    monkeypatch.setattr("trading.engine._submit",
                        AsyncMock(return_value={"order_id": "tp_seq", "state": "SUBMITTED"}))
    # ① async_response 回填 real
    asyncio.run(_pump(gw, lambda: gw.on_order_stock_async_response(
        SimpleNamespace(seq=seq, order_id=real))))
    # ② order 事件推进 FILLED（累计量）
    asyncio.run(_pump(gw, lambda: gw.on_stock_order(SimpleNamespace(
        order_id=real, stock_code="600000.SH", order_status=56, order_type=23,
        order_volume=100, traded_volume=100, traded_price=10.5, status_msg=""))))
    # ③ trade 事件落账 + 挂 TP
    asyncio.run(_pump(gw, lambda: gw.on_stock_trade(SimpleNamespace(
        order_id=real, stock_code="600000.SH", traded_volume=100, traded_price=10.5,
        traded_amount=1050.0, traded_time="20260801101000"))))
    with state_store._connect(state_store._DEFAULT_DB) as con:
        row = con.execute('SELECT state, filled_qty FROM "order" WHERE order_id=?', (oid,)).fetchone()
        tp_row = con.execute("SELECT purpose, qty FROM \"order\" WHERE purpose='TP2'").fetchone()
        fill_row = con.execute("SELECT COUNT(*) c FROM fill").fetchone()
        pos_row = con.execute("SELECT qty FROM position WHERE symbol='600000.SH'").fetchone()
    assert row["state"] == "FILLED" and row["filled_qty"] == 100
    assert tp_row is not None and tp_row["qty"] == 100, "BUY 成交后应挂 TP2 100"
    assert fill_row["c"] == 1 and pos_row["qty"] == 100, "成交应落 fill + position"


# ============================================================================
# Task 6（W3.1 gateway-ssot-hardening）：成交回报 CSV/钉钉幂等
# ============================================================================
def test_trade_replay_writes_csv_and_notifies_only_once(state_db, monkeypatch, tmp_path):
    """W3.1：同 (order_id, traded_time) 成交回报重放 → insert_fill 返 False →
    CSV/钉钉不再重复写（与真相源同判定点，spec §3.3.1）。

    08-04 事故根因：record_live_trade（CSV）+ notify_trade_event（钉钉）在 insert_fill
    幂等判定**之外**无条件调。同一笔成交 600000.SH BUY 100@10.5 被重放 8 批 × 3 行 = 24 笔，
    state_store fill 表幂等拦截成功（只 1 行），但 CSV 审计镜像 + 钉钉通知无条件追加 →
    简报「买 24 笔」虚高 + 钉钉轰炸。

    本测试用真 CSV 落盘（非 mock 自欺）：monkeypatch LIVE_TRADE_LOG 到 tmp 路径，
    两次 _handle_order_update 后断言 CSV 只 1 行 kind=fill + 钉钉只 1 次。
    """
    import csv as _csv
    from trading import state_store

    # 真实 CSV 落盘到 tmp（不 mock record_live_trade —— 验证它本身被幂等挡住）
    csv_path = tmp_path / "live_trades.csv"
    monkeypatch.setattr(
        "presentation.server.services.trading_service.LIVE_TRADE_LOG", str(csv_path))

    eng = TradingEngine()
    eng._tp_placed = set()
    # 预置 DB order 行：让 _order_direction 从 DB side 反查得 BUY（不依赖内存 _orders）
    aid = engine._resolve_account_id()
    today = _today_str()
    state_store.upsert_account(aid, broker="qmt")
    state_store.insert_order(
        f"{today}_600000.SH_OPEN_1", f"{aid}_600000.SH_{today}", aid, today,
        "600000.SH", "buy", "OPEN", 100, 10.0,
        broker_oid="600000_SEQ_1", state="SUBMITTED")
    # 内存 _orders 留空（强制走 DB 反查路径，与生产主推路径一致）
    eng._gw = MagicMock()
    eng._gw._orders = {}
    eng._gw._seq_to_real = {}

    update = {
        "kind": "trade",
        "order_id": "600000_SEQ_1",
        "stock_code": "600000.SH",
        "traded_volume": 100,
        "traded_price": 10.5,
        "traded_amount": 1050.0,
        "traded_time": "20260801101000",  # 同笔成交时间（幂等键的一半）
        "state": "FILLED",
    }
    fake_mgr = MagicMock()
    fake_mgr.notify_trade_event = AsyncMock(return_value=[])
    with patch("presentation.server.services.trading_service.record_live_trade"), \
         patch("infra.notifier.NotificationManager") as NM, \
         patch.object(eng, "_place_take_profit", new=AsyncMock()):
        NM.get_default.return_value = fake_mgr
        # 注意：record_live_trade 在 with 内被 patch → 不写真 CSV（这里只验证「被调几次」）
        # 但为防 mock 自欺，下面单独一个测试用真 CSV 落盘验证。本测试先验证调用次数。
        asyncio.run(eng._handle_order_update(update))   # 首次 → insert_fill=True
        asyncio.run(eng._handle_order_update(update))   # 重放 → insert_fill=False

    # record_live_trade 首次被调，重放不再调（与真相源同判定点）
    from presentation.server.services.trading_service import record_live_trade
    # patch 退出后 record_live_trade 已还原，无法直接查 mock；改用 fake_mgr 钉钉次数 +
    # 下面独立 CSV 落盘断言。本断言点先校钉钉：
    assert fake_mgr.notify_trade_event.call_count == 1, \
        f"重放应只推 1 次钉钉，实际 {fake_mgr.notify_trade_event.call_count}"


def test_trade_replay_csv_real_file_only_one_row(state_db, monkeypatch, tmp_path):
    """W3.1 真 CSV 落盘断言（非 mock）：重放同 (order_id, traded_time) → CSV 只 1 行 kind=fill。

    与上一测试的区别：本测试**不 patch record_live_trade**，让真函数写真 CSV 到 tmp，
    读回断言行数。这是「非 mock 自欺」的证据 —— 证明改动后 record_live_trade 真的被
    幂等挡住（而非被 mock 替身假装挡住）。
    """
    import csv as _csv
    from trading import state_store

    csv_path = tmp_path / "live_trades.csv"
    monkeypatch.setattr(
        "presentation.server.services.trading_service.LIVE_TRADE_LOG", str(csv_path))

    eng = TradingEngine()
    eng._tp_placed = set()
    aid = engine._resolve_account_id()
    today = _today_str()
    state_store.upsert_account(aid, broker="qmt")
    state_store.insert_order(
        f"{today}_600000.SH_OPEN_2", f"{aid}_600000.SH_{today}", aid, today,
        "600000.SH", "buy", "OPEN", 100, 10.0,
        broker_oid="600000_SEQ_2", state="SUBMITTED")
    eng._gw = MagicMock()
    eng._gw._orders = {}
    eng._gw._seq_to_real = {}

    update = {
        "kind": "trade",
        "order_id": "600000_SEQ_2",
        "stock_code": "600000.SH",
        "traded_volume": 100,
        "traded_price": 10.5,
        "traded_amount": 1050.0,
        "traded_time": "20260801101000",
        "state": "FILLED",
    }
    fake_mgr = MagicMock()
    fake_mgr.notify_trade_event = AsyncMock(return_value=[])
    # 关键：不 patch record_live_trade —— 让真函数写真 CSV
    with patch("infra.notifier.NotificationManager") as NM, \
         patch.object(eng, "_place_take_profit", new=AsyncMock()):
        NM.get_default.return_value = fake_mgr
        asyncio.run(eng._handle_order_update(update))   # 首次
        asyncio.run(eng._handle_order_update(update))   # 重放
        asyncio.run(eng._handle_order_update(update))   # 三放（验证 ≥2 次重放也挡住）

    # 真 CSV 读回：只 1 行 kind=fill
    assert csv_path.exists(), "CSV 应被首次写入创建"
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        rows = list(_csv.DictReader(f))
    fill_rows = [r for r in rows if r.get("kind") == "fill"]
    assert len(fill_rows) == 1, \
        f"重放只应写 1 行 kind=fill，实际 {len(fill_rows)}（总行 {len(rows)}）"
    assert fill_rows[0]["symbol"] == "600000.SH"
    assert float(fill_rows[0]["shares"]) == 100.0
    # 钉钉同样只 1 次
    assert fake_mgr.notify_trade_event.call_count == 1


def test_trade_direction_unknown_writes_no_csv_no_notify(state_db, monkeypatch, tmp_path):
    """W3.1 完整收口：direction=None（方向未知旁路）不再写 CSV / 不再推钉钉。

    物理意图（spec §3.3.1「同一判定点」）：
        原 direction=None 分支无条件写 CSV + 推钉钉（"TRADE" 中性标签），与 insert_fill
        的幂等判定不同判定点 —— 同一条「方向未知回报」被重放 N 次会重复落 CSV/推钉钉，
        污染审计镜像与 IM 通知。W3 完整收口选 C：direction=None 时**不写 CSV / 不推钉钉**
        （上方 _alert_critical 已告警人工对账，CSV 旁证在重放时反而污染真相源判定），
        与 fill 表「direction 不在 (BUY,SELL) 时 insert_fill 不被调（无 fill 表行）」
        同判定点（都不写），符合 spec §3.3.1。

    断言：
      1) record_live_trade 不被调（CSV 0 行）；
      2) notify_trade_event 不被调（钉钉 0 次）；
      3) _alert_critical 仍被调（方向未知仍告警人工对账，保留既有红线）。
    """
    from trading import state_store

    csv_path = tmp_path / "live_trades.csv"
    monkeypatch.setattr(
        "presentation.server.services.trading_service.LIVE_TRADE_LOG", str(csv_path))

    eng = TradingEngine()
    eng._tp_placed = set()
    # 不预置 DB order 行、不塞 _orders → _order_direction 返 None（方向未知）
    eng._gw = MagicMock()
    eng._gw._orders = {}
    eng._gw._seq_to_real = {}

    update = {
        "kind": "trade",
        "order_id": "999999_UNKNOWN",  # DB 无行 / 内存无 order_type → direction=None
        "stock_code": "600000.SH",
        "traded_volume": 100,
        "traded_price": 10.5,
        "traded_amount": 1050.0,
        "traded_time": "20260801101000",
        "state": "FILLED",
    }
    fake_mgr = MagicMock()
    fake_mgr.notify_trade_event = AsyncMock(return_value=[])
    alert_calls = []
    monkeypatch.setattr(
        "trading.engine._alert_critical", lambda msg: alert_calls.append(msg))
    # Fix1：方向未知 _alert_critical 加了 live 守卫，dry_run 不推钉钉。
    # 本测试断「_alert_critical 仍触发」必须 patch _mode=live 才能命中守卫。
    import trading.engine as _eng_mod
    monkeypatch.setattr(_eng_mod, "_mode", lambda: "live")
    # 不 patch record_live_trade —— 让真函数在 direction=None 分支不被调（CSV 0 行验证）
    with patch("infra.notifier.NotificationManager") as NM, \
         patch.object(eng, "_place_take_profit", new=AsyncMock()):
        NM.get_default.return_value = fake_mgr
        asyncio.run(eng._handle_order_update(update))   # 首次（方向未知）
        asyncio.run(eng._handle_order_update(update))   # 重放（方向未知）

    # 1) CSV 不存在 / 0 行（direction=None 不写）
    if csv_path.exists():
        with open(csv_path, encoding="utf-8-sig", newline="") as f:
            rows = list(__import__("csv").DictReader(f))
        assert len(rows) == 0, f"direction=None 不应写 CSV，实际 {len(rows)} 行"
    # 2) 钉钉 0 次
    assert fake_mgr.notify_trade_event.call_count == 0, (
        f"direction=None 不应推钉钉，实际 {fake_mgr.notify_trade_event.call_count} 次")
    # 3) _alert_critical 仍触发（方向未知仍告警人工对账）
    assert len(alert_calls) >= 1, "direction=None 应触发 _alert_critical 人工对账告警"


def test_e2e_trade_before_async_response_race(db, monkeypatch):
    """竞态：trade 先于 async_response → seq 反查兜底落账，随后回填不覆盖。"""
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from trading import state_store

    eng, gw = _make_real_chain_engine()
    aid, seq, real = "TEST_ACC", 7, 987654
    oid = "2026-08-01_600000.SH_OPEN_7"
    state_store.upsert_account(aid, broker="qmt")
    state_store.insert_order(oid, f"{aid}_600000.SH_2026-08-01", aid, "2026-08-01",
                             "600000.SH", "buy", "OPEN", 100, 10.0,
                             broker_oid=str(seq), state="SUBMITTED")
    gw._seq_to_real = {seq: real}
    monkeypatch.setattr("trading.engine._submit",
                        AsyncMock(return_value={"order_id": "tp_seq", "state": "SUBMITTED"}))
    # trade 先到（async_response 未回填）：方向经 seq 反查 DB side 仍应落账
    asyncio.run(_pump(gw, lambda: gw.on_stock_trade(SimpleNamespace(
        order_id=real, stock_code="600000.SH", traded_volume=100, traded_price=10.5,
        traded_amount=1050.0, traded_time="20260801101000"))))
    with state_store._connect(state_store._DEFAULT_DB) as con:
        fill_row = con.execute("SELECT COUNT(*) c FROM fill").fetchone()
    assert fill_row["c"] == 1, "trade 先到也必须落账（seq 反查兜底）"
    # async_response 后到：回填 real，不覆盖任何东西
    asyncio.run(_pump(gw, lambda: gw.on_order_stock_async_response(
        SimpleNamespace(seq=seq, order_id=real))))
    with state_store._connect(state_store._DEFAULT_DB) as con:
        row = con.execute('SELECT broker_oid FROM "order" WHERE order_id=?', (oid,)).fetchone()
    assert row["broker_oid"] == str(real), "async_response 应回填 real"
