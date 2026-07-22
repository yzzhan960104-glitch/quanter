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

    def _fake_push(date, orders):
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
    from strategies.signal import Signal

    monkeypatch.setattr(engine, "_submit", _no_op_submit)
    monkeypatch.setattr(trading_plan, "push_plan_to_dingtalk", lambda d, o: True)

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


# ============================================================================
# 2. pre_open：未确认不挂 / 确认后挂 / 撤昨日单 / submit raise 兜底
# ============================================================================
def test_pre_open_blocks_unconfirmed_plan():
    """pre_open：计划未确认 → 不挂单，reason 含「未确认」。"""
    trading_plan.save_plan("2099-01-02", [])  # confirmed=False
    result = asyncio.run(engine.pre_open("2099-01-02"))
    assert result["submitted"] == 0
    assert "未确认" in result["reason"]


def test_pre_open_blocks_when_no_plan():
    """pre_open：无计划文件 → 不挂单，reason 含「无计划」。"""
    result = asyncio.run(engine.pre_open("2099-12-31"))
    assert result["submitted"] == 0
    assert "无计划" in result["reason"]


def test_pre_open_cancels_yesterday_open_orders(monkeypatch):
    """scope #2：pre_open 开头必须调 cancel_all_open_orders 撤昨日未成交单。"""
    # 准备一份已确认但 orders 空的计划（聚焦撤单断言，不挂单）
    trading_plan.save_plan("2099-01-02", [])
    assert trading_plan.confirm_plan("2099-01-02")

    cancelled = {"n": 0}

    class _FakeGw:
        async def _fetch_broker_positions(self):
            return {}

    async def _fake_cancel(gw):
        cancelled["n"] += 1
        return 0

    monkeypatch.setattr(engine.circuit_breaker, "cancel_all_open_orders", _fake_cancel)
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

    monkeypatch.setattr(engine.circuit_breaker, "cancel_all_open_orders", _fake_cancel)
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
    monkeypatch.setattr(engine.circuit_breaker, "cancel_all_open_orders",
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


# ============================================================================
# 4. post_close：对账照做，熔断显式留 TODO（本 task 不实现）
# ============================================================================
def test_post_close_runs_reconcile(monkeypatch):
    """post_close：传 gw + local_positions 时调 run_reconcile，返 drift 标志。"""
    from trading.execution_gateway import ReconciliationResult

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


# ============================================================================
# 5. TradingEngine 装配：实例化即注册 4 cron job（不 start，不真起 scheduler）
# ============================================================================
def test_engine_registers_four_cron_jobs():
    """TradingEngine 实例化 → AsyncIOScheduler 装 4 个 job（eod/pre_open/stoploss/post_close）。

    不 start（plan 红线：不起 APScheduler 真调度），只验证 cron 注册成功。
    """
    eng = engine.TradingEngine()
    jobs = eng.sched.get_jobs()
    job_ids = {j.id for j in jobs}
    assert {"eod_plan", "pre_open", "stop_loss", "post_close"} <= job_ids


# ============================================================================
# 公共测试辅助
# ============================================================================
async def _no_op_submit(order, **kw):
    """占位 _submit：不应被调（调用即 fail）。"""
    raise AssertionError(f"_submit 不应被调（本 case 信号/计划为空）: {order}")


async def _no_op_cancel(gw):
    """占位 cancel_all_open_orders：no-op。"""
    return 0
