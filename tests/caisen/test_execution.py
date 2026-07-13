# -*- coding: utf-8 -*-
"""ExecutionEngine 测试：状态迁移 + 止损/止盈/时间止损/移动止盈 + 回踩触发 + tick 编排。

物理定位（CLAUDE.md 极简 + 显式原则）：
    本测试覆盖蔡森形态学流水线 Phase 3 · Task 2 的两大组件：
        1. check_exit 离场纯函数（无 I/O）——回放验证器（Phase 2 Task10）与实盘共用，
           杜绝双源真理。优先级：止损 > 止盈 > 时间止损，并联 + 移动止盈（盈亏平衡）。
        2. ExecutionEngine 状态机编排（tick_pullback / tick_exit）——盘中 beat 调用，
           遍历 ARMED/FILLED 计划触发下单。trading_service 用 Mock 注入（隔离 I/O）。

测试分层：
    - 离场纯函数（4 测试）：止损/止盈/时间止损/移动止盈——直接断言 ExitDecision。
    - 回踩触发判定（2 测试）：触及/未触及回踩区间——断言 check_pullback 返回。
    - tick 编排（2 测试）：mock trading_service + monkeypatch storage，验证
      ARMED→FILLED（submit_order buy）与 FILLED→CLOSED（submit_order sell）+ 断线跳过。
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from caisen.config import StrategyConfig
from caisen.execution import (
    ExitAction,
    ExitDecision,
    ExitReason,
    ExecutionEngine,
    check_exit,
)


# ============================================================================
# 离场纯函数 check_exit（4 测试，plan 完整代码逐字对齐）
# ============================================================================
def test_stop_loss_hit():
    """low ≤ stop_loss → 止损离场。

    物理意图：日内最低价触及/跌破止损 = 硬风控触发，立即平仓（优先级最高）。
    """
    cfg = StrategyConfig()
    # entry 10, stop 9, take_profit 12, 持仓 1 天
    pos = {"entry": 10.0, "stop": 9.0, "take_profit": 12.0, "take_profit_2x": 14.0,
           "entry_bar": 0, "bars_held": 1}
    bar = {"high": 9.5, "low": 8.8, "close": 9.0}
    act = check_exit(pos, bar, bars_held=1, cfg=cfg)
    assert act.action == ExitAction.CLOSE
    assert act.reason == ExitReason.STOP_LOSS


def test_take_profit_hit():
    """high ≥ take_profit → 止盈离场。

    物理意图：日内最高价触及/突破第一波满足点 = 止盈目标达成，平仓锁盈。
    """
    cfg = StrategyConfig()
    pos = {"entry": 10.0, "stop": 9.0, "take_profit": 12.0, "take_profit_2x": 14.0,
           "entry_bar": 0, "bars_held": 2}
    bar = {"high": 12.5, "low": 11.8, "close": 12.2}
    act = check_exit(pos, bar, bars_held=2, cfg=cfg)
    assert act.action == ExitAction.CLOSE
    assert act.reason == ExitReason.TAKE_PROFIT


def test_take_profit_2x_preferred_over_first_wave():
    """high ≥ take_profit_2x → 止盈离场（第二波优先，与回测 backtest_replay 对齐）。

    物理意图(#16)：回测 _simulate_one_trade 离场优先级 stop_loss > take_profit_2x >
    take_profit；实盘 check_exit 原仅看第一波，构成双源真理（回测按 2x 价记大盈 rr、
    实盘第一波即平），系统性虚高回测 avg_rr。现 check_exit 先判 2x，与本测试共同
    守护「2x 字段被消费」+ 与回测判定链一致。本用例 high=14.5 ≥ 2x(14) → CLOSE。
    """
    cfg = StrategyConfig()
    pos = {"entry": 10.0, "stop": 9.0, "take_profit": 12.0, "take_profit_2x": 14.0,
           "entry_bar": 0, "bars_held": 2}
    bar = {"high": 14.5, "low": 13.8, "close": 14.2}
    act = check_exit(pos, bar, bars_held=2, cfg=cfg)
    assert act.action == ExitAction.CLOSE
    assert act.reason == ExitReason.TAKE_PROFIT


def test_take_profit_2x_absent_falls_back_to_first_wave():
    """pos 缺 take_profit_2x → 降级只看第一波 take_profit（向后兼容老调用）。

    物理意图：tick_exit 传 pos["take_profit_2x"]=plan.get("take_profit_2x")，老计划
    可能无此字段；check_exit 用 .get 取，缺失/None 时跳过 2x 档直接判第一波，不破老契约。
    本用例 high=12.5 ≥ take_profit(12)，2x 缺失 → 仍 TAKE_PROFIT（第一波）。
    """
    cfg = StrategyConfig()
    pos = {"entry": 10.0, "stop": 9.0, "take_profit": 12.0,   # 无 take_profit_2x
           "entry_bar": 0, "bars_held": 2}
    bar = {"high": 12.5, "low": 11.8, "close": 12.2}
    act = check_exit(pos, bar, bars_held=2, cfg=cfg)
    assert act.action == ExitAction.CLOSE
    assert act.reason == ExitReason.TAKE_PROFIT


def test_timeout_exit_when_profit_below_threshold():
    """持仓 ≥ max_holding_bars 且浮盈 < timeout_exit_threshold → 时间止损。

    物理意图：超时未达目标 + 浮盈不足阈值 = 资金占用机会成本过高，离场释放资金。
    注：plan 测试名"below_threshold"，但 check_exit 语义是 profit < threshold 才离场
    （浮盈不足即离场）。本用例浮盈 0% < 1% → TIMEOUT。
    """
    cfg = StrategyConfig(max_holding_bars=3, timeout_exit_threshold=0.01)
    pos = {"entry": 10.0, "stop": 9.0, "take_profit": 12.0, "take_profit_2x": 14.0,
           "entry_bar": 0, "bars_held": 4}
    bar = {"high": 10.05, "low": 9.9, "close": 10.0}   # 浮盈 0% < 1%
    act = check_exit(pos, bar, bars_held=4, cfg=cfg)
    assert act.action == ExitAction.CLOSE
    assert act.reason == ExitReason.TIMEOUT


def test_trailing_to_breakeven_after_activation():
    """持仓 ≥ trailing_activation_bars → 止损上移至盈亏平衡(entry)。

    物理意图：移动止盈激活后，将止损从原 C 波低点上移至 entry（盈亏平衡），
    锁定本金（浮亏不可能扩大到原始止损幅度）。本用例 low 10.2 > 上移后止损 10.0，
    故不触发止损 → HOLD，但 new_stop=10.0 指示执行器更新持久化止损。
    """
    cfg = StrategyConfig(trailing_activation_bars=2, trailing_to_breakeven=True)
    pos = {"entry": 10.0, "stop": 9.0, "take_profit": 12.0, "take_profit_2x": 14.0,
           "entry_bar": 0, "bars_held": 3}
    bar = {"high": 11.0, "low": 10.2, "close": 10.5}   # 未触发止盈/止损
    act = check_exit(pos, bar, bars_held=3, cfg=cfg)
    # 止损应已上移到 entry(10)，low 10.2 > 10 不触发；返回 HOLD + 更新止损
    assert act.action == ExitAction.HOLD
    assert act.new_stop == pytest.approx(10.0)   # 盈亏平衡


# ============================================================================
# 回踩触发判定 check_pullback（2 测试）
# ============================================================================
def test_check_pullback_triggers():
    """触及回踩区间（low≤entry_upper 且 high≥entry_lower）→ 触发 True。

    物理意图：盘中价格曾跌入回踩挂单区间 [entry_lower, entry_upper]，
    限价挂单（entry_upper）应被触发成交（ARMED → FILLED）。
    """
    cfg = StrategyConfig()
    engine = ExecutionEngine(trading_service=MagicMock(), cfg=cfg)
    plan = {"entry_upper": 10.0, "entry_lower": 9.8, "symbol": "FAKE.SZ"}
    # low 9.9 ≤ entry_upper 10.0 且 high 10.1 ≥ entry_lower 9.8 → 触及
    quote = {"high": 10.1, "low": 9.9, "close": 10.0}
    assert engine.check_pullback(plan, quote) is True


def test_check_pullback_no_trigger():
    """未触及回踩区间（low > entry_upper）→ 不触发 False。

    物理意图：盘中价格始终高于回踩挂单上限（未回踩），限价挂单不触发。
    """
    cfg = StrategyConfig()
    engine = ExecutionEngine(trading_service=MagicMock(), cfg=cfg)
    plan = {"entry_upper": 10.0, "entry_lower": 9.8, "symbol": "FAKE.SZ"}
    # low 10.2 > entry_upper 10.0 → 未触及（价始终在挂单上方）
    quote = {"high": 10.5, "low": 10.2, "close": 10.3}
    assert engine.check_pullback(plan, quote) is False


# ============================================================================
# tick 编排（mock trading_service + monkeypatch storage）
# ============================================================================
def test_tick_pullback_armed_to_filled(monkeypatch, tmp_path):
    """tick_pullback：ARMED 计划触及回踩 → submit_order(buy) + update_plan(FILLED)。

    物理意图：盘中 beat 调用 tick_pullback，遍历 ARMED 计划，触及回踩区间则
    限价挂 entry_upper 买入（过 trading_service.submit_order 10 关风控 + EMT），
    成交后状态推进 FILLED。trading_service / storage 用 Mock + monkeypatch 隔离 I/O。
    """
    cfg = StrategyConfig()
    # mock trading_service：get_status 返回 live + connected；submit_order async mock
    trading = MagicMock()
    trading.get_status.return_value = {"connected": True, "locked": False, "mode": "live"}
    trading.submit_order = AsyncMock(return_value={"order_id": "test-1", "state": "FILLED"})

    engine = ExecutionEngine(trading_service=trading, cfg=cfg)

    # monkeypatch storage：load_plans 返回一个 ARMED 计划；update_plan 记录调用
    armed_plan = {
        "plan_id": "p1", "symbol": "FAKE.SZ", "status": "ARMED",
        "entry_upper": 10.0, "entry_lower": 9.8, "shares": 100,
    }
    updates = []
    monkeypatch.setattr(
        "caisen.execution.storage.load_plans",
        lambda status=None: [armed_plan] if status == "ARMED" else [],
    )
    monkeypatch.setattr(
        "caisen.execution.storage.update_plan",
        lambda plan_id, **fields: updates.append((plan_id, fields)),
    )
    # _get_quote 返回触及回踩的行情
    monkeypatch.setattr(engine, "_get_quote", AsyncMock(return_value={"high": 10.1, "low": 9.9}))

    asyncio.run(engine.tick_pullback())

    # 断言：submit_order 被调用（buy + price=entry_upper）
    trading.submit_order.assert_awaited_once()
    order = trading.submit_order.call_args.args[0]
    assert order.symbol == "FAKE.SZ"
    assert order.qty == 100
    assert order.side == "buy"
    assert order.price == 10.0
    # 断言：update_plan 推进 FILLED
    assert updates == [("p1", {"status": "FILLED", "entry_bar": engine._today_bar()})]


def test_tick_exit_filled_to_closed(monkeypatch):
    """tick_exit：FILLED 持仓触止损 → submit_order(sell) + update_plan(CLOSED)。

    物理意图：盘中 beat 调用 tick_exit，遍历 FILLED 持仓，check_exit 命中 CLOSE
    则市价平仓（submit_order side=sell），状态推进 CLOSED。storage 用 monkeypatch 隔离。
    """
    cfg = StrategyConfig()
    trading = MagicMock()
    trading.get_status.return_value = {"connected": True, "locked": False, "mode": "live"}
    trading.submit_order = AsyncMock(return_value={"order_id": "test-2", "state": "FILLED"})

    engine = ExecutionEngine(trading_service=trading, cfg=cfg)

    # FILLED 持仓：entry 10, stop 9, take_profit 12（触止损用）
    filled_plan = {
        "plan_id": "p2", "symbol": "FAKE.SZ", "status": "FILLED",
        "entry": 10.0, "stop": 9.0, "take_profit": 12.0, "take_profit_2x": 14.0,
        "shares": 100, "entry_bar": 0, "bars_held": 1,
    }
    updates = []
    monkeypatch.setattr(
        "caisen.execution.storage.load_plans",
        lambda status=None: [filled_plan] if status == "FILLED" else [],
    )
    monkeypatch.setattr(
        "caisen.execution.storage.update_plan",
        lambda plan_id, **fields: updates.append((plan_id, fields)),
    )
    # 行情：low 8.8 ≤ stop 9.0 → 止损触发
    monkeypatch.setattr(engine, "_get_quote", AsyncMock(return_value={"high": 9.5, "low": 8.8, "close": 9.0}))

    asyncio.run(engine.tick_exit())

    # 断言：submit_order 被调用（sell + price=None 市价）
    trading.submit_order.assert_awaited_once()
    order = trading.submit_order.call_args.args[0]
    assert order.symbol == "FAKE.SZ"
    assert order.qty == 100
    assert order.side == "sell"
    # 断言：update_plan 推进 CLOSED
    assert updates == [("p2", {"status": "CLOSED"})]


def test_tick_skipped_when_disconnected(monkeypatch):
    """断线（locked/not connected）→ tick 跳过本轮，不查行情不下单。

    物理意图（CLAUDE.md 接口与状态机边界）：trading_service 断线瞬间，行情/下单
    均不可靠，beat 本轮跳过（不补发、不重试），等下一轮重连后再处理。避免在
    不可用状态下发废单 / 误判离场。
    """
    cfg = StrategyConfig()
    trading = MagicMock()
    # locked=True（风控否决）→ tick 应跳过
    trading.get_status.return_value = {"connected": False, "locked": True, "mode": "vetoed_by_risk"}
    trading.submit_order = AsyncMock()

    engine = ExecutionEngine(trading_service=trading, cfg=cfg)

    # 即便有 ARMED 计划也不应被处理
    monkeypatch.setattr(
        "caisen.execution.storage.load_plans",
        lambda status=None: [{"plan_id": "x", "symbol": "FAKE.SZ"}],
    )
    quote_called = MagicMock()
    monkeypatch.setattr(engine, "_get_quote", AsyncMock(side_effect=quote_called))

    asyncio.run(engine.tick_pullback())
    trading.submit_order.assert_not_awaited()
    quote_called.assert_not_called()

    asyncio.run(engine.tick_exit())
    trading.submit_order.assert_not_awaited()


# ============================================================================
# tick_pullback 状态校验（B-4：杜绝幽灵持仓）
# ============================================================================
def test_tick_pullback_submitted_not_marked_filled(monkeypatch):
    """限价单仅 SUBMITTED（未成交）时，计划不得标 FILLED（防幽灵持仓 B-4）。

    EMT submit_order 返回 state=SUBMITTED（订单已提交，成交靠异步回报）。若 tick_pullback
    无视返回 state 直接标 FILLED，会在未实际成交的限价单上建出「幽灵持仓」，随后 tick_exit
    可能在其上发市价 SELL → 对不存在的持仓发卖单（裸卖空/拒单/敞口失控）。
    """
    cfg = StrategyConfig()
    trading = MagicMock()
    trading.get_status.return_value = {"connected": True, "locked": False, "mode": "live"}
    trading.submit_order = AsyncMock(
        return_value={"order_id": "test-sub", "state": "SUBMITTED", "message": "已提交排队"}
    )
    engine = ExecutionEngine(trading_service=trading, cfg=cfg)

    armed_plan = {
        "plan_id": "p-sub", "symbol": "FAKE.SZ", "status": "ARMED",
        "entry_upper": 10.0, "entry_lower": 9.8, "shares": 100,
    }
    updates = []
    monkeypatch.setattr(
        "caisen.execution.storage.load_plans",
        lambda status=None: [armed_plan] if status == "ARMED" else [],
    )
    monkeypatch.setattr(
        "caisen.execution.storage.update_plan",
        lambda plan_id, **fields: updates.append((plan_id, fields)),
    )
    monkeypatch.setattr(engine, "_get_quote", AsyncMock(return_value={"high": 10.1, "low": 9.9}))

    asyncio.run(engine.tick_pullback())

    # 限价单确实挂出去了
    trading.submit_order.assert_awaited_once()
    # 但 plan 绝不得被标 FILLED（SUBMITTED 未成交 = 无持仓）
    filled = [u for u in updates if u[1].get("status") == "FILLED"]
    assert filled == [], "SUBMITTED 不得标 FILLED（幽灵持仓 B-4）"


def test_tick_pullback_rejected_reverts_to_pending(monkeypatch):
    """下单被拒（REJECTED）→ 计划回退 PENDING_APPROVAL，不得标 FILLED（B-4）。"""
    cfg = StrategyConfig()
    trading = MagicMock()
    trading.get_status.return_value = {"connected": True, "locked": False, "mode": "live"}
    trading.submit_order = AsyncMock(
        return_value={"order_id": "", "state": "REJECTED", "message": "资金不足"}
    )
    engine = ExecutionEngine(trading_service=trading, cfg=cfg)

    armed_plan = {
        "plan_id": "p-rej", "symbol": "FAKE.SZ", "status": "ARMED",
        "entry_upper": 10.0, "entry_lower": 9.8, "shares": 100,
    }
    updates = []
    monkeypatch.setattr(
        "caisen.execution.storage.load_plans",
        lambda status=None: [armed_plan] if status == "ARMED" else [],
    )
    monkeypatch.setattr(
        "caisen.execution.storage.update_plan",
        lambda plan_id, **fields: updates.append((plan_id, fields)),
    )
    monkeypatch.setattr(engine, "_get_quote", AsyncMock(return_value={"high": 10.1, "low": 9.9}))

    asyncio.run(engine.tick_pullback())

    statuses = [u[1].get("status") for u in updates]
    assert "FILLED" not in statuses, "REJECTED 不得标 FILLED"
    assert "PENDING_APPROVAL" in statuses, "REJECTED 应回退 PENDING_APPROVAL 待人工"


def test_tick_exit_rejected_not_marked_closed(monkeypatch):
    """平仓卖单被拒（REJECTED）→ 不得标 CLOSED（防幽灵了结，B-4 对称）。

    tick_exit 命中止损后 submit_order(sell)，若卖单被拒（如锁态/限价未成交/柜台拒），
    不得盲目标 CLOSED——否则会把仍持有的仓位从监控移除，敞口失控且与券商不一致。
    保持 FILLED，下一轮 tick_exit 重新评估（止损只更急）。
    """
    cfg = StrategyConfig()
    trading = MagicMock()
    trading.get_status.return_value = {"connected": True, "locked": False, "mode": "live"}
    trading.submit_order = AsyncMock(
        return_value={"order_id": "", "state": "REJECTED", "message": "柜台拒单"}
    )
    engine = ExecutionEngine(trading_service=trading, cfg=cfg)

    filled_plan = {
        "plan_id": "p-exit-rej", "symbol": "FAKE.SZ", "status": "FILLED",
        "entry": 10.0, "stop": 9.0, "take_profit": 12.0, "take_profit_2x": 14.0,
        "shares": 100, "entry_bar": 0, "bars_held": 1,
    }
    updates = []
    monkeypatch.setattr(
        "caisen.execution.storage.load_plans",
        lambda status=None: [filled_plan] if status == "FILLED" else [],
    )
    monkeypatch.setattr(
        "caisen.execution.storage.update_plan",
        lambda plan_id, **fields: updates.append((plan_id, fields)),
    )
    # 行情 low 8.8 ≤ stop 9.0 → 触发止损 CLOSE
    monkeypatch.setattr(engine, "_get_quote",
                        AsyncMock(return_value={"high": 9.5, "low": 8.8, "close": 9.0}))

    asyncio.run(engine.tick_exit())

    # submit_order(sell) 被调用
    trading.submit_order.assert_awaited_once()
    # 但卖单 REJECTED → 不得标 CLOSED（防幽灵了结）
    closed = [u for u in updates if u[1].get("status") == "CLOSED"]
    assert closed == [], "卖单 REJECTED 不得标 CLOSED（幽灵了结，B-4 对称）"


def test_tick_exit_processes_when_connected_and_locked(monkeypatch):
    """connected+locked（vetoed_by_risk）→ tick_exit 仍处理离场（持仓风控持续，B-8）。

    tick_exit 旧闸门 `locked or not connected → return` 在风险否决锁态下停摆离场监控，
    已有 FILLED 持仓的止损/止盈失控。放宽为仅 not-connected 跳过（断线无可靠行情），
    connected+locked 时仍评估离场（卖单是否成交由网关/state 校验决定）。
    """
    cfg = StrategyConfig()
    trading = MagicMock()
    # connected=True + locked=True（vetoed_by_risk）—— 旧闸门会 return，新闸门放行
    trading.get_status.return_value = {"connected": True, "locked": True, "mode": "vetoed_by_risk"}
    trading.submit_order = AsyncMock(
        return_value={"order_id": "ok", "state": "FILLED", "message": "平仓成交"}
    )
    engine = ExecutionEngine(trading_service=trading, cfg=cfg)

    filled_plan = {
        "plan_id": "p-lock", "symbol": "FAKE.SZ", "status": "FILLED",
        "entry": 10.0, "stop": 9.0, "take_profit": 12.0, "take_profit_2x": 14.0,
        "shares": 100, "entry_bar": 0, "bars_held": 1,
    }
    monkeypatch.setattr(
        "caisen.execution.storage.load_plans",
        lambda status=None: [filled_plan] if status == "FILLED" else [],
    )
    monkeypatch.setattr(
        "caisen.execution.storage.update_plan",
        lambda plan_id, **fields: None,
    )
    monkeypatch.setattr(engine, "_get_quote",
                        AsyncMock(return_value={"high": 9.5, "low": 8.8, "close": 9.0}))

    asyncio.run(engine.tick_exit())

    # 关键：connected+locked 时 tick_exit 仍提交卖单（未在闸门处 return）
    trading.submit_order.assert_awaited_once()
