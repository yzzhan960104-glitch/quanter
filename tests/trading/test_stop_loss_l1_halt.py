# -*- coding: utf-8 -*-
"""U3b：stop_loss_monitor 查持仓/DB 异常 → raise _CriticalHalt（不再软降级）。

物理意图（spec §3 L1 · review 补强边界）：
    原 stop_loss_monitor 三处关键路径软降级——查持仓返 ``{checked:0}``、
    _stop_already_placed 回退 False、_record_stop 仅 logger——=「敞口未明 / DB 真相源
    失真但调度继续 30s 后再跑」。升 L1 后：异常立即 raise _CriticalHalt 停整轮调度，
    CRITICAL 唤醒人工，绝不在「不知是否已发卖」状态下继续盲跑（双倍卖 / 幽灵单致命）。

判定线（严守 plan review 分层提示，三层不混）：
    - 框架级 L1（本 task 改）：
        (a) 查持仓 ``_fetch_broker_positions`` 异常 = L1（敞口完全未明，盲卖致命）；
        (b) ``_stop_already_placed`` 幂等读异常 = L1（不知是否已挂 STOP → 可能重发 = 双倍卖）；
        (c) ``_record_stop`` 写异常 = L1（卖单已发但 DB 没记 = 幽灵单 + 下轮重发）。
    - 业务级 L2（本 task 不动，U4 才聚合）：
        decide_exit D12 fallback（盘中不裸奔是已定设计）、单只 ``_submit`` 业务拒单
        （涨跌停挡板 / 资金不足，外层 try 吞，逐单不炸整批）。
    - 待补 L3：查可撤单 / pending 撤单失败（本 task 不动）。

测试范式（沿袭 test_pre_open_l1_halt.py）：
    本仓库未配 pytest-asyncio 的 asyncio_mode，历史 engine 测试一律 ``asyncio.run(...)``
    同步驱动 async。本测试沿袭该范式，避免引入 @pytest.mark.asyncio 造成风格分叉。

隔离模式（同 test_pre_open_l1_halt.py）：
    隔离 TRADE_PLAN_DIR + state_store._DEFAULT_DB + position_book._DEFAULT_DB，
    杜绝污染真实 .db / .json。盘中判定用 monkeypatch ``calendar.is_intraday_session`` 放行。
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from trading.critical import _CriticalHalt  # W1-B：迁物理真身（engine re-export 已删）
from trading.phases.stop_loss import stop_loss_monitor  # W1-B：迁物理真身
from trading.stop_loss_context import StopLossContext  # M2：单参收三 map
from strategies.neckline.execution import ExitAction, ExitReason  # W1-B：迁物理真身


# ----------------------------------------------------------------------------
# 共享 fixture：隔离 env + DB + 让 calendar.is_intraday_session 恒返 True
# ----------------------------------------------------------------------------
@pytest.fixture
def isolated_stoploss(monkeypatch, tmp_path):
    """隔离环境 + 放行盘中时段判定 + 清干净模块级 _state_store。

    stop_loss_monitor 内引用的 _state_store / calendar / qmt_market_data 均 patch 入口
    在 engine 模块上（与 test_pre_open_l1_halt.py 同款隔离）。
    """
    # 隔离 plan dir + state_store / position_book DB（与 test_pre_open_l1_halt.py 同款）
    monkeypatch.setenv("TRADE_PLAN_DIR", str(tmp_path / "plans"))
    monkeypatch.setenv("AUTO_TRADE_MODE", "dry_run")
    from trading import state_store, position_book
    _db = str(tmp_path / "state.db")
    monkeypatch.setattr(position_book, "_DEFAULT_DB", _db)
    monkeypatch.setattr(state_store, "_DEFAULT_DB", _db)
    position_book.init_db()
    state_store.init_store()

    # 放行盘中时段判定（否则 monitor 第一行就 return checked:0，测不到查持仓异常）
    from trading import engine
    monkeypatch.setattr(engine.calendar, "is_intraday_session", lambda _dt: True)

    return engine


# ============================================================================
# Case (a)：查持仓 _fetch_broker_positions 异常 → L1
# ============================================================================
def test_fetch_positions_failure_raises_critical_halt(isolated_stoploss):
    """_fetch_broker_positions 抛异常 → raise _CriticalHalt（不再 return checked:0 软降级）。

    物理意图：查持仓失败=敞口完全未明——原 return 软降级只是本轮跳过，下轮 30s 后继续
    盲跑。升 L1：停调度（spec §3 查持仓失败=L1），CRITICAL 唤醒人工（敞口未明继续跑=盲卖致命）。
    """
    engine = isolated_stoploss
    gw = AsyncMock()
    gw._fetch_broker_positions.side_effect = RuntimeError("柜台断线")

    with pytest.raises(_CriticalHalt, match="查持仓"):
        asyncio.run(stop_loss_monitor(  # M2：三 map 装箱单参（2026-08-15）
            StopLossContext(
                stop_prices={"300214.SZ": 9.0},
                monitor_ctx={"300214.SZ": {"state": {"stop": 9.0}, "cfg": {}}}),
            gw=gw))


# ============================================================================
# Case (b)：_stop_already_placed 幂等读异常 → L1
# ============================================================================
def test_stop_already_placed_read_failure_raises_critical_halt(isolated_stoploss, monkeypatch):
    """_stop_already_placed 内 has_order(STOP) 抛异常 → raise _CriticalHalt。

    物理意图：不知是否已挂 STOP 委托 → 继续 _submit 可能重发 = 双倍卖（致命）。
    原回退 False（当作没挂）= 在重发路径上盲发。升 L1：停调度，人工核对 DB 真相。
    构造：持仓存在 + 现价跌破 stop + decide_exit 返 CLOSE/STOP_LOSS → 触达 _stop_already_placed。
    """
    engine = isolated_stoploss

    # gw 返 1 只持仓 + get_quotes 返跌破 stop 的现价（触发卖出分支）
    gw = AsyncMock()
    gw._fetch_broker_positions.return_value = {
        "300214.SZ": {"volume": 100, "avg_price": 10.0}}
    monkeypatch.setattr("trading.qmt_market_data.get_quotes",  # W1-B：迁共享模块
                        AsyncMock(return_value={"300214.SZ": {
                            "last_price": 8.5, "high": 10.5, "low": 8.4}}))

    # decide_exit 返 CLOSE/STOP_LOSS（仓位命中止损）
    fake_dec = MagicMock()
    fake_dec.action = ExitAction.CLOSE
    fake_dec.reason = ExitReason.STOP_LOSS
    fake_dec.portion = 1.0
    # W1-A/T2-Task19：stop_loss_monitor 函数体已迁 trading.phases.stop_loss，其内部
    # decide_exit/_submit/_state_store 经【顶部 import 本地绑定】（phases.stop_loss 模块
    # __globals__）。patch trading.engine.X 不命中函数体符号解析（engine re-export 仅保
    # wrapper 调用点），故 3 patch 全迁 trading.phases.stop_loss.X（Task 15 范式 · __globals__ 归属）。
    monkeypatch.setattr("trading.phases.stop_loss.decide_exit", lambda *a, **kw: fake_dec)

    # _submit 不应被调（幂等读 L1 在 _submit 之前抛，提前中断）
    async def _should_not_submit(*a, **kw):
        raise AssertionError("幂等读 L1 后绝不应触达 _submit")
    monkeypatch.setattr("trading.phases.stop_loss._submit", _should_not_submit)

    # has_order(STOP) 抛异常 → _stop_already_placed raise _CriticalHalt
    with patch("trading.phases.stop_loss._state_store") as ss:
        ss.has_order.side_effect = RuntimeError("db disk full")

        with pytest.raises(_CriticalHalt, match="has_order"):
            asyncio.run(stop_loss_monitor(  # M2：三 map 装箱单参（2026-08-15）
                StopLossContext(
                    stop_prices=None,
                    monitor_ctx={"300214.SZ": {"state": {"stop": 9.0}, "cfg": {}}}),
                gw=gw))


# ============================================================================
# Case (c)：_record_stop 写异常 → L1
# ============================================================================
def test_record_stop_write_failure_raises_critical_halt(isolated_stoploss, monkeypatch):
    """_record_stop 内 insert_order(STOP) 抛异常 → raise _CriticalHalt。

    物理意图：卖单已通过 _submit 发出（柜台已收单），但 DB 没记 = 对账以为没挂 →
    下轮重发 = 双倍卖（幽灵单）。升 L1：卖单已发 DB 真相源失真，立即停防重发。
    构造：持仓存在 + 现价跌破 + decide_exit CLOSE + 幂等读通过（无已挂）+ _submit 成功 →
    触达 _record_stop 的 insert_order。
    """
    engine = isolated_stoploss

    gw = AsyncMock()
    gw._fetch_broker_positions.return_value = {
        "300214.SZ": {"volume": 100, "avg_price": 10.0}}
    monkeypatch.setattr("trading.qmt_market_data.get_quotes",  # W1-B：迁共享模块
                        AsyncMock(return_value={"300214.SZ": {
                            "last_price": 8.5, "high": 10.5, "low": 8.4}}))

    fake_dec = MagicMock()
    fake_dec.action = ExitAction.CLOSE
    fake_dec.reason = ExitReason.STOP_LOSS
    fake_dec.portion = 1.0
    # decide_exit/_submit/_state_store 全迁 trading.phases.stop_loss.X（同 case (b) 注 ·
    # __globals__ 归属 · Task 15/19 范式）。
    monkeypatch.setattr("trading.phases.stop_loss.decide_exit", lambda *a, **kw: fake_dec)

    # 幂等读通过（无已挂 STOP）
    # _submit 成功（柜台收单，触发 _record_stop 回填）
    monkeypatch.setattr("trading.phases.stop_loss._submit",
                        AsyncMock(return_value={"state": "FILLED", "order_id": "seq_1"}))

    # insert_order(STOP) 抛异常 → _record_stop raise _CriticalHalt
    with patch("trading.phases.stop_loss._state_store") as ss:
        ss.has_order.return_value = False
        ss.get_account.return_value = MagicMock()  # 跳过 upsert_account 分支
        ss.insert_order.side_effect = RuntimeError("sqlite locked")

        with pytest.raises(_CriticalHalt, match="record_stop"):
            asyncio.run(stop_loss_monitor(  # M2：三 map 装箱单参（2026-08-15）
                StopLossContext(
                    stop_prices=None,
                    monitor_ctx={"300214.SZ": {"state": {"stop": 9.0}, "cfg": {}}}),
                gw=gw))


# ============================================================================
# 边界守卫：decide_exit 异常仍走 D12 fallback 不抛 _CriticalHalt（保持 L2）
# ============================================================================
def test_decide_exit_exception_stays_l2_fallback_not_halt(isolated_stoploss, monkeypatch):
    """decide_exit 抛异常 → D12 fallback（should_trigger_stop）兜底，不抛 _CriticalHalt。

    严守 plan 风险提示的「分层」：框架级（查持仓/DB）= L1（停调度）；
    decide_exit 业务级 = L2 fallback（盘中不裸奔是已定设计 D12，保持）。
    本测试断言 decide_exit 抛异常时 monitor 不停，走 fallback 继续判 should_trigger_stop。
    """
    engine = isolated_stoploss

    gw = AsyncMock()
    gw._fetch_broker_positions.return_value = {
        "300214.SZ": {"volume": 100, "avg_price": 10.0}}
    # 现价未跌破 stop（fallback should_trigger_stop 返 False → 不发单，正常返回）
    monkeypatch.setattr("trading.qmt_market_data.get_quotes",  # W1-B：迁共享模块
                        AsyncMock(return_value={"300214.SZ": {
                            "last_price": 9.6, "high": 10.5, "low": 9.5}}))

    # decide_exit 抛异常 → 应走 D12 fallback（不应 raise _CriticalHalt）
    # 迁 trading.phases.stop_loss.decide_exit（同上 __globals__ 归属）——迁移后 mock 真正命中
    # 抛 RuntimeError 触发 fallback（迁移前 mock 失效 · 真实 decide_exit 抛 KeyError 亦凑巧触发
    # fallback · 现象通过但语义错位 · 本 Task 一并订正）。
    monkeypatch.setattr("trading.phases.stop_loss.decide_exit",
                        MagicMock(side_effect=RuntimeError("state 缺键")))

    # _submit 绝不应被调（fallback 未触发 should_trigger_stop，现价 9.6 > stop 9.0）
    async def _should_not_submit(*a, **kw):
        raise AssertionError("fallback 未触发时应触达 _submit 说明 fallback 逻辑错")
    monkeypatch.setattr("trading.phases.stop_loss._submit", _should_not_submit)

    # _state_store 正常（has_order 返 False，幂等读不抛 L1）
    with patch("trading.phases.stop_loss._state_store") as ss:
        ss.has_order.return_value = False

        # 不应 raise _CriticalHalt（应正常返回，checked=1, fallback_used=1）
        result = asyncio.run(stop_loss_monitor(  # M2：三 map 装箱单参（2026-08-15）
            StopLossContext(
                stop_prices={"300214.SZ": 9.0},
                monitor_ctx={"300214.SZ": {"state": {"stop": 9.0}, "cfg": {}}}),
            gw=gw))

    assert result["checked"] == 1
    assert result["fallback_used"] == 1
