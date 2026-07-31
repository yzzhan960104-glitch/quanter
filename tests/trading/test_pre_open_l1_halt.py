# -*- coding: utf-8 -*-
"""U3a：pre_open DB 写失败 → raise _CriticalHalt → _pre_open 被 guard 捕获 → _halt。

物理意图（spec §3 L1 · review 补强边界）：
    原 pre_open 四处 DB 读写点 except 软降级（logger.exception + 继续挂下一只）=
    「DB 真相源已失真但调度继续」→ 对账幽灵单 / 重复挂 / FK 失效连环错。改 L1：
    DB 写/幂等读异常立即 raise _CriticalHalt 停整批，绝不带病挂下一只（真金损失防线）。

判定线（严守 plan review 补强）：
    - DB **写**异常（insert_order/update_order_state/insert_trade_event）= L1
      （哪怕只挂一只，DB 真相源失真优先于「单只」语义）；
    - DB 幂等**读**异常（has_order/get_latest_action）= L1（不知是否已挂→继续挂=可能重复挂）；
    - 单只 _submit RuntimeError（业务拒单：涨跌停/资金不足）= L2（外层 try，本 task 不动）。

测试范式（沿袭 test_engine_pre_open_gate.py:15-17 TDD 约定）：
    本仓库未配 pytest-asyncio 的 asyncio_mode，历史 engine 测试一律 ``asyncio.run(...)``
    同步驱动 async。本测试沿袭该范式，避免引入 @pytest.mark.asyncio 造成风格分叉。

隔离模式（同 test_engine_pre_open_gate.py:222-230）：
    隔离 TRADE_PLAN_DIR + state_store._DEFAULT_DB + position_book._DEFAULT_DB，
    杜绝污染真实 .db / .json。
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from trading.engine import TradingEngine, _CriticalHalt


# ----------------------------------------------------------------------------
# 共享 fixture：隔离 env + DB + 构造 engine + 把 pre_open 走到挂单循环的统一前置
# ----------------------------------------------------------------------------
@pytest.fixture
def isolated_eng(monkeypatch, tmp_path):
    """隔离环境 + 构造 TradingEngine + 注入 _ACTIVE_ENGINE 单例。

    返回 eng，供每个 case 在其上 patch 模块级引用。
    """
    # 隔离 plan dir + state_store / position_book DB（与 test_engine_pre_open_gate.py 同款）
    monkeypatch.setenv("TRADE_PLAN_DIR", str(tmp_path / "plans"))
    monkeypatch.setenv("AUTO_TRADE_MODE", "dry_run")
    from trading import state_store, position_book
    _db = str(tmp_path / "state.db")
    monkeypatch.setattr(position_book, "_DEFAULT_DB", _db)
    monkeypatch.setattr(state_store, "_DEFAULT_DB", _db)
    position_book.init_db()
    state_store.init_store()

    from trading import engine
    eng = TradingEngine()
    monkeypatch.setattr(engine, "_ACTIVE_ENGINE", eng)

    # gw=None（pre_open 内 696 行 warning 跳过撤昨日单 + 746 行跳过基线快照）。
    # dry_run 模式下 _submit 命中 dry_run 分支返 {"state":"DRY_RUN"} 不触达 gw——
    # 但本测试重点是 DB 失败，不让 _submit 真发单：每个 case 在 _submit 上加断言守卫。
    monkeypatch.setattr(engine, "get_gateway", lambda: None)

    return eng


def _green_plan():
    """1 只标的 + 已确认的 plan（让 pre_open 走到挂单循环）。"""
    return {"confirmed": True, "orders": [{
        "order": {"symbol": "300214.SZ", "qty": 100, "side": "buy", "price": 10.0},
        "formed_at": None,  # None → 不走 max_wait 窗口过滤（days=0，挂单）
    }]}


def _walk_to_submit_chain(engine_mod, ss_mock):
    """统一构造 pre_open 走到挂单循环的 mock 链：gate 绿 / 撤单 / 基线 / 过期持仓 / 幂等读。

    ss_mock 由调用方注入具体 side_effect（决定哪个 DB 调用抛异常）。
    """
    # ① gate 绿（_pre_open_gate 是 async，需 AsyncMock 返 (True, "")）
    eng = engine_mod._ACTIVE_ENGINE
    engine_mod._ACTIVE_ENGINE._pre_open_gate = AsyncMock(return_value=(True, ""))


# ============================================================================
# Case (a)：insert_order(OPEN) 写异常 → L1
# ============================================================================
def test_insert_order_failure_raises_critical_halt(isolated_eng, monkeypatch):
    """insert_order(OPEN) 抛异常 → pre_open raise _CriticalHalt（不再软降级继续挂）。

    物理意图：insert_order 是 DB 真相源写入——失败=柜台可能挂了但 DB 没记=对账幽灵单。
    """
    from trading import engine

    _walk_to_submit_chain(engine, None)

    plan = _green_plan()
    with patch("trading.engine.trading_plan") as tp, \
         patch("trading.engine._cancel_all_open_orders",
               new=AsyncMock(return_value={"cancelled": 0, "unconfirmed": 0})), \
         patch("trading.engine._load_expired_positions", return_value=[]), \
         patch("trading.engine._submit") as submit_mock:
        tp.load_plan.return_value = plan
        with patch("trading.engine._state_store") as ss:
            # account 行存在（跳过 upsert 分支）
            ss.get_account.return_value = MagicMock()
            # 幂等读通过（无 veto / 无已挂 OPEN）
            ss.get_latest_action.return_value = None
            ss.has_order.return_value = False
            # insert_order 抛异常 → 应 raise _CriticalHalt
            ss.insert_order.side_effect = RuntimeError("sqlite locked")

            # _submit 绝不应被调（insert_order 在 _submit 之前抛 L1，提前中断）
            async def _should_not_submit(*a, **kw):
                raise AssertionError("insert_order 抛 L1 后绝不应触达 _submit")
            submit_mock.side_effect = _should_not_submit

            with pytest.raises(_CriticalHalt, match="insert_order"):
                asyncio.run(engine.pre_open("2026-07-31"))


# ============================================================================
# Case (b)：DB 幂等读异常（has_order/get_latest_action）→ L1
# ============================================================================
def test_idempotent_read_failure_raises_critical_halt(isolated_eng, monkeypatch):
    """has_order 抛异常 → pre_open raise _CriticalHalt（不知是否已挂=可能重复挂=真金损失）。"""
    from trading import engine

    _walk_to_submit_chain(engine, None)

    plan = _green_plan()
    with patch("trading.engine.trading_plan") as tp, \
         patch("trading.engine._cancel_all_open_orders",
               new=AsyncMock(return_value={"cancelled": 0, "unconfirmed": 0})), \
         patch("trading.engine._load_expired_positions", return_value=[]), \
         patch("trading.engine._submit") as submit_mock:
        tp.load_plan.return_value = plan
        with patch("trading.engine._state_store") as ss:
            ss.get_account.return_value = MagicMock()
            # get_latest_action 通过，has_order 抛异常
            ss.get_latest_action.return_value = None
            ss.has_order.side_effect = RuntimeError("db disk full")

            async def _should_not_submit(*a, **kw):
                raise AssertionError("幂等读 L1 后绝不应触达 _submit")
            submit_mock.side_effect = _should_not_submit

            with pytest.raises(_CriticalHalt, match="幂等读"):
                asyncio.run(engine.pre_open("2026-07-31"))


# ============================================================================
# Case (c)：回填 SUBMITTED/ORDERED 写异常 → L1
# ============================================================================
def test_submit_backfill_failure_raises_critical_halt(isolated_eng, monkeypatch):
    """update_order_state(SUBMITTED)/insert_trade_event(ORDERED) 抛异常 → raise _CriticalHalt。

    物理意图：柜台已挂成功（_submit 返 DRY_RUN）但 DB 没回 SUBMITTED=对账以为没挂→
    重复挂/幽灵单。升 L1（review 补强：DB 真相源失真优先于单只语义）。
    """
    from trading import engine

    _walk_to_submit_chain(engine, None)

    plan = _green_plan()
    with patch("trading.engine.trading_plan") as tp, \
         patch("trading.engine._cancel_all_open_orders",
               new=AsyncMock(return_value={"cancelled": 0, "unconfirmed": 0})), \
         patch("trading.engine._load_expired_positions", return_value=[]), \
         patch("trading.engine._submit") as submit_mock:
        tp.load_plan.return_value = plan
        with patch("trading.engine._state_store") as ss:
            ss.get_account.return_value = MagicMock()
            ss.get_latest_action.return_value = None
            ss.has_order.return_value = False
            # insert_order(OPEN) 通过（这是 case (a) 的职责，不在这测）
            ss.insert_order.return_value = None
            # _submit 返成功（DRY_RUN）→ 走回填 SUBMITTED 分支
            submit_mock.return_value = {"state": "DRY_RUN", "order_id": "seq_1"}
            # update_order_state(SUBMITTED) 抛异常 → raise _CriticalHalt
            ss.update_order_state.side_effect = RuntimeError("db locked")

            with pytest.raises(_CriticalHalt, match="SUBMITTED"):
                asyncio.run(engine.pre_open("2026-07-31"))


# ============================================================================
# Case (d)：account 行写异常 → L1
# ============================================================================
def test_account_row_failure_raises_critical_halt(isolated_eng, monkeypatch):
    """upsert_account / get_account 抛异常 → raise _CriticalHalt（DB 真故障，FK 全失效）。"""
    from trading import engine

    _walk_to_submit_chain(engine, None)

    plan = _green_plan()
    with patch("trading.engine.trading_plan") as tp, \
         patch("trading.engine._cancel_all_open_orders",
               new=AsyncMock(return_value={"cancelled": 0, "unconfirmed": 0})), \
         patch("trading.engine._load_expired_positions", return_value=[]), \
         patch("trading.engine._submit") as submit_mock:
        tp.load_plan.return_value = plan
        with patch("trading.engine._state_store") as ss:
            # get_account 抛异常 → account 行无法确认 → L1
            ss.get_account.side_effect = RuntimeError("sqlite corruption")

            async def _should_not_submit(*a, **kw):
                raise AssertionError("account L1 后绝不应触达 _submit")
            submit_mock.side_effect = _should_not_submit

            with pytest.raises(_CriticalHalt, match="account"):
                asyncio.run(engine.pre_open("2026-07-31"))


# ============================================================================
# 边界守卫：_submit 业务拒单（L2）保持 RuntimeError 不抛 _CriticalHalt
# ============================================================================
def test_submit_runtime_error_stays_l2_not_halt(isolated_eng, monkeypatch):
    """_submit raise RuntimeError（业务拒单：涨跌停/资金不足）= L2，不抛 _CriticalHalt。

    严守 plan 风险提示的「分层 try」：内层 DB 写异常→L1；外层 _submit RuntimeError→L2。
    两层不能合并（U4 才聚合 L2，本 task 不改）。本测试断言 RuntimeError 不被吞成 _CriticalHalt。
    """
    from trading import engine

    _walk_to_submit_chain(engine, None)

    plan = _green_plan()
    with patch("trading.engine.trading_plan") as tp, \
         patch("trading.engine._cancel_all_open_orders",
               new=AsyncMock(return_value={"cancelled": 0, "unconfirmed": 0})), \
         patch("trading.engine._load_expired_positions", return_value=[]), \
         patch("trading.engine._submit") as submit_mock:
        tp.load_plan.return_value = plan
        with patch("trading.engine._state_store") as ss:
            ss.get_account.return_value = MagicMock()
            ss.get_latest_action.return_value = None
            ss.has_order.return_value = False
            ss.insert_order.return_value = None
            # _submit 业务拒单（L2 路径，应被外层 try 吞掉，不抛 _CriticalHalt）
            submit_mock.side_effect = RuntimeError("涨停价挡板拒单")
            # 回填 REJECTED 的写也不抛（不影响本断言）
            ss.update_order_state.return_value = None

            # 不应 raise _CriticalHalt（应正常返回 submitted=0）
            result = asyncio.run(engine.pre_open("2026-07-31"))

    assert result["submitted"] == 0
