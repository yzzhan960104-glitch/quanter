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


# ============================================================================
# 4.7 Task 10（R-2 日内熔断）：pre_open 快照 + post_close 三步串联
# ============================================================================
def test_pre_open_snapshot_start_equity(monkeypatch):
    """pre_open：确认闸通过后调 query_asset → snapshot_start_equity 写 daily_equity。

    物理意图（plan Task 10 Step 2 · 对齐缺口 R-2）：
        熔断判定需要 start_equity 基线。pre_open 在确认闸后（撤昨日单前或后均可，
        关键是开盘前抓基线）调 gw.query_asset 拿当日开盘总资产，写 daily_equity 表。
        - query_asset 返 {}（未连接/锁定/异常）→ 跳过快照 + WARN（不拿 0 误触发熔断）
        - 重入幂等：snapshot_start_equity 用 INSERT OR REPLACE，重启安全
    """
    from trading import position_book

    # 隔离 position_book db（防污染生产账本）
    db_path = os.path.join(os.environ["TRADE_PLAN_DIR"], "..", "state.db")
    monkeypatch.setattr(position_book, "_DEFAULT_DB", db_path)
    position_book.init_db()

    # 已确认计划
    orders = [{
        "order": {"symbol": "300001.SZ", "qty": 100, "side": "buy", "price": 10.0},
        "stop_price": 9.0, "take_profit": 11.0,
    }]
    trading_plan.save_plan("2099-01-02", orders)
    trading_plan.confirm_plan("2099-01-02")

    # 假 gw：query_asset 返 total_asset=1_000_000
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

    # 验证 daily_equity 快照已写
    today = datetime.now().strftime("%Y-%m-%d")
    start_eq = position_book.get_start_equity(today)
    assert start_eq == 1_000_000.0


def test_pre_open_snapshot_skip_when_query_asset_empty(monkeypatch):
    """query_asset 返 {}（未连接）→ 跳过快照 + WARN（不拿 0 误触发熔断）。

    物理意图（边界 · spec §5.2）：
        gw 未连接 / 锁定 / 超时 → query_asset 返 {}。绝不能拿 0 当基线，
        否则 post_close check_daily_loss_limit(0, curr) 会因 start<=0 返 False
        反而永不熔断，或拿 None 参与除法抛 TypeError。正确行为：跳过快照 + 告警。
    """
    from trading import position_book

    db_path = os.path.join(os.environ["TRADE_PLAN_DIR"], "..", "state.db")
    monkeypatch.setattr(position_book, "_DEFAULT_DB", db_path)
    position_book.init_db()

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

    # 验证未写 daily_equity（get_start_equity 返 None）
    today = datetime.now().strftime("%Y-%m-%d")
    assert position_book.get_start_equity(today) is None


async def _no_op_submit_should_not_be_called_unused(order, **kw):
    """占位（用于 test_pre_open_snapshot_skip_when_query_asset_empty，挂单会被调
    但本测试不验证挂单，故不抛错只返 SUBMITTED）。"""
    return {"state": "SUBMITTED"}


def test_post_close_circuit_breaker_triggers(monkeypatch):
    """post_close：start=100w/curr=96w（-4%）→ cancel_all + emergency_halt + ERROR 告警。

    物理意图（plan Task 10 Step 3 · 对齐缺口 R-2）：
        post_close 在 reconcile 之后串联三步：
          1) get_start_equity(today) → start_equity
          2) query_asset → curr_equity
          3) check_daily_loss_limit(start, curr) → True 即 cancel_all + emergency_halt
        缺基线（start=None）→ 跳过 + WARN（不拿 0 误触发）。
    """
    from trading import position_book

    db_path = os.path.join(os.environ["TRADE_PLAN_DIR"], "..", "state.db")
    monkeypatch.setattr(position_book, "_DEFAULT_DB", db_path)
    position_book.init_db()
    # 写基线 100w
    today = datetime.now().strftime("%Y-%m-%d")
    position_book.snapshot_start_equity(today, 1_000_000.0)

    class _FakeGw:
        async def query_asset(self):
            return {"total_asset": 960_000.0}  # -4% 触发熔断（限 -3%）
        async def _fetch_broker_positions(self):
            return {}
    fake_gw = _FakeGw()

    # 拦截副作用：cancel_all + emergency_halt
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


def test_post_close_circuit_breaker_skip_when_within_limit(monkeypatch):
    """post_close：curr 只跌 2%（未触 -3%）→ 不熔断，cancel_all/halt 均不调。"""
    from trading import position_book

    db_path = os.path.join(os.environ["TRADE_PLAN_DIR"], "..", "state.db")
    monkeypatch.setattr(position_book, "_DEFAULT_DB", db_path)
    position_book.init_db()
    today = datetime.now().strftime("%Y-%m-%d")
    position_book.snapshot_start_equity(today, 1_000_000.0)

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


# ============================================================================
# gap4：position_book 接线（_post_close 读账本 + _handle_order_update 写账本）
# ============================================================================
def test_post_close_reads_position_book(monkeypatch):
    """_post_close：读 position_book 账本 → 注入 local_positions → 调 post_close 对账。"""
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

    eng = eng_mod.TradingEngine()
    asyncio.run(eng._post_close())
    assert captured["local"] == {"300001.SZ": 100.0}  # 账本读出注入 post_close


def test_post_close_empty_book_passes_empty_dict(monkeypatch):
    """_post_close：账本空 → 传 {}（非 None）——live 下 broker 有时能报 only_broker drift。"""
    from trading import position_book, engine as eng_mod

    monkeypatch.setattr(position_book, "get_local_positions", lambda **kw: {})
    captured = {}

    async def _fake_post_close(date, *, gw=None, local_positions=None, tolerance=0.0):
        captured["local"] = local_positions
        return {"date": date}

    monkeypatch.setattr(eng_mod, "post_close", _fake_post_close)
    monkeypatch.setattr(eng_mod.calendar, "is_trading_day", lambda d: True)

    eng = eng_mod.TradingEngine()
    asyncio.run(eng._post_close())
    assert captured["local"] == {}  # 空 dict 直传，不转 None


def test_handle_order_update_writes_book(monkeypatch):
    """BUY 成交回报 → apply_fill 被调（方向 "BUY"）；order_id 缺失 _orders（direction=None）→ apply_fill 不调。

    双 case 诚实覆盖（T3-M1 fix）：
      - 正向：order_type=23(STOCK_BUY) → _order_direction 返 "BUY" → engine 第四连守门
        ``if direction in ("BUY","SELL")`` 放行 → apply_fill 被调一次、direction 实参为 "BUY"；
      - 反向：_orders 清空 → order_id 不在 dict → _order_direction 返 None → 守门拦截 → apply_fill 零调用
        （对齐 engine.py:941 注释「方向 None 不写」与 c 连「不挂止盈」保守语义——不猜方向误记为买当卖/卖当买）。
    """
    from unittest.mock import MagicMock, AsyncMock, patch
    from trading import position_book
    from trading.engine import TradingEngine

    # ---- 公共前置：成交回报报文 + 引擎实例 ----
    eng = TradingEngine()
    eng._tp_placed = set()
    update = {
        "kind": "trade", "order_id": "999", "stock_code": "300001.SZ",
        "traded_volume": 100, "traded_price": 10.5, "state": "FILLED",
    }

    # ---- Case 1（正向）：order_type=23 → BUY → apply_fill 被调一次、方向 "BUY" ----
    eng._gw = MagicMock()
    eng._gw._orders = {"999": {"order_type": 23}}  # 23=STOCK_BUY

    with patch("presentation.server.services.trading_service.record_live_trade"), \
         patch("infra.notifier.NotificationManager"), \
         patch.object(eng, "_place_take_profit", new=AsyncMock()), \
         patch.object(position_book, "apply_fill", return_value=True) as af_buy:
        asyncio.run(eng._handle_order_update(update))
    af_buy.assert_called_once()
    assert af_buy.call_args.args[2] == "BUY"  # direction 参数（位置参）

    # ---- Case 2（反向）：order_id 不在 _orders → direction None → apply_fill 零调用 ----
    eng._gw = MagicMock()
    eng._gw._orders = {}  # 清空 _orders：_order_direction 查无 order_type → None

    with patch("presentation.server.services.trading_service.record_live_trade"), \
         patch("infra.notifier.NotificationManager"), \
         patch.object(eng, "_place_take_profit", new=AsyncMock()), \
         patch.object(position_book, "apply_fill", return_value=True) as af_none:
        asyncio.run(eng._handle_order_update(update))
    af_none.assert_not_called()  # 方向 None 守门拦截，账本不写（防误记买当卖/卖当买）


def test_handle_order_update_book_failure_soft_degrades(monkeypatch):
    """apply_fill 抛异常 → 不阻断 a 日志/b 通知/c 止盈（独立 try-except 软降级）。"""
    from unittest.mock import MagicMock, AsyncMock, patch
    from trading import position_book
    from trading.engine import TradingEngine

    eng = TradingEngine()
    eng._tp_placed = set()
    update = {
        "kind": "trade", "order_id": "999", "stock_code": "300001.SZ",
        "traded_volume": 100, "traded_price": 10.5, "state": "FILLED",
    }
    eng._gw = MagicMock()
    eng._gw._orders = {"999": {"order_type": 23}}

    tp_called = []
    async def _tp(*a, **kw):
        tp_called.append(True)

    with patch("presentation.server.services.trading_service.record_live_trade"), \
         patch("infra.notifier.NotificationManager"), \
         patch.object(position_book, "apply_fill", side_effect=RuntimeError("db locked")), \
         patch.object(eng, "_place_take_profit", new=_tp):
        asyncio.run(eng._handle_order_update(update))  # apply_fill 抛异常不应冒泡
    # 止盈仍被调（账本失败不阻断 c 连）
    assert tp_called == [True]


# ============================================================================
# 4.8 Task 8（P0-4 max_holding 超时平仓）：post_close 标记超期 + pre_open 跌停价平仓
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


def test_post_close_writes_expired_positions(monkeypatch):
    """post_close：未熔断 + 有超期持仓 → 写 expired_positions.json + result 标记数。

    物理意图（plan Task 8 Step 2）：post_close 在 ⑤ max_holding 段（熔断后、清白名单前）
    扫超期持仓写标记文件，供次日 pre_open 消费平仓。熔断未触发时正常写。
    """
    from trading import position_book
    monkeypatch.setattr(position_book, "get_entry_dates",
                        lambda **kw: {"A.SH": "2099-01-01"})
    monkeypatch.setattr(engine, "_trading_days_between", lambda s, e: 20)
    # 隔离 expired_positions.json 到 tmp
    expired_path = os.path.join(os.environ["TRADE_PLAN_DIR"], "..", "expired.json")
    monkeypatch.setattr(engine, "_EXPIRED_POSITIONS_PATH", expired_path)
    # 隔离 position_book db + 写熔断基线（curr=start → 0% 不熔断）
    db_path = os.path.join(os.environ["TRADE_PLAN_DIR"], "..", "state.db")
    monkeypatch.setattr(position_book, "_DEFAULT_DB", db_path)
    position_book.init_db()
    today = datetime.now().strftime("%Y-%m-%d")
    position_book.snapshot_start_equity(today, 1_000_000.0)

    class _FakeGw:
        async def query_asset(self):
            return {"total_asset": 1_000_000.0}  # 0% 不熔断
        async def _fetch_broker_positions(self):
            return {}
    from trading.compute.reconcile import ReconciliationResult
    async def _fake_rec(gw, local, tolerance=0.0):
        return ReconciliationResult([], [], [], [], 0.0, True)
    monkeypatch.setattr(engine.reconcile_job, "run_reconcile", _fake_rec)
    monkeypatch.setattr(engine, "_cancel_all_open_orders", _no_op_cancel)

    result = asyncio.run(engine.post_close(today, gw=_FakeGw(), local_positions={}))

    assert result.get("expired_positions") == 1
    payload = json.loads(open(expired_path, encoding="utf-8").read())
    assert payload["expired"][0]["symbol"] == "A.SH"
    assert payload["date"] == today


def test_post_close_breaker_skips_max_holding(monkeypatch):
    """post_close：日内熔断触发 → 跳过 max_holding 标记（熔断优先，不写 expired.json）。

    物理意图（plan Task 8 Step 4 · 熔断优先约束）：
        熔断（-3%）已 emergency_halt + lock_down 全场停摆，此时再标超期会让次日
        pre_open 平仓单与熔断善后冲突。spec 红线：熔断优先于 max_holding 标记。
    """
    from trading import position_book
    monkeypatch.setattr(position_book, "get_entry_dates",
                        lambda **kw: {"A.SH": "2099-01-01"})
    monkeypatch.setattr(engine, "_trading_days_between", lambda s, e: 20)
    expired_path = os.path.join(os.environ["TRADE_PLAN_DIR"], "..", "expired_breaker.json")
    monkeypatch.setattr(engine, "_EXPIRED_POSITIONS_PATH", expired_path)
    db_path = os.path.join(os.environ["TRADE_PLAN_DIR"], "..", "state.db")
    monkeypatch.setattr(position_book, "_DEFAULT_DB", db_path)
    position_book.init_db()
    today = datetime.now().strftime("%Y-%m-%d")
    position_book.snapshot_start_equity(today, 1_000_000.0)

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
    assert "expired_positions" not in result       # 熔断 → 不标 max_holding
    assert not os.path.exists(expired_path)        # 且不写 expired.json


def test_pre_open_closes_expired_positions(monkeypatch):
    """pre_open：读 expired 标记 → 查 gw 持仓 → 跌停价卖 + 消费标记文件。

    物理意图（plan Task 8 Step 3）：
        pre_open 在撤昨日单后、挂新买单前，平【昨日盘后标记】的超期持仓：
        - qty 来自 gw._fetch_broker_positions 真实持仓（红线，同 stop_loss_monitor）；
        - 挂跌停价卖单（保证成交，超时释放资金接受滑点）；
        - 平仓尝试后消费（删）标记文件——防下次 pre_open 重复挂卖单致卖超（漏平<卖超）。
    """
    expired_path = os.path.join(os.environ["TRADE_PLAN_DIR"], "..", "expired_pre.json")
    monkeypatch.setattr(engine, "_EXPIRED_POSITIONS_PATH", expired_path)
    # 写昨日盘后的超期标记（holding_days=20>15）
    with open(expired_path, "w", encoding="utf-8") as f:
        json.dump({"date": "2099-01-02", "expired": [
            {"symbol": "300001.SZ", "entry_date": "2099-01-01",
             "holding_days": 20, "max_holding": 15}]}, f)
    # 当日已确认计划（买单标的与超期标的不同，互不干扰）
    orders = [{"order": {"symbol": "300002.SZ", "qty": 100, "side": "buy", "price": 10.0},
               "stop_price": 9.0, "take_profit": 11.0}]
    trading_plan.save_plan("2099-01-02", orders)
    trading_plan.confirm_plan("2099-01-02")

    class _FakeGw:
        async def query_asset(self):
            return {"total_asset": 1_000_000.0}
        async def _fetch_broker_positions(self):
            return {"300001.SZ": {"volume": 200, "avg_price": 10.0}}  # 真实持仓 200 股
    monkeypatch.setattr(engine, "get_gateway", lambda: _FakeGw())
    monkeypatch.setattr(engine, "_cancel_all_open_orders", _no_op_cancel)
    # 跌停价行情（low_limit=8.5）
    async def _fake_quotes(syms):
        return {sym: {"last_price": 9.0, "low_limit": 8.5} for sym in syms}
    monkeypatch.setattr(engine.qmt_market_data, "get_quotes", _fake_quotes)
    # 拦截 _submit：记录所有挂单（区分 buy/sell）
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
    # 验标记文件被消费（删除，防重复挂卖单）
    assert not os.path.exists(expired_path)


def test_pre_open_close_expired_skip_when_no_price(monkeypatch):
    """pre_open：无跌停价/现价 → 跳过该标的（拒发盲单），仍消费标记文件。

    物理意图（边界 · Grill Me 风控）：
        quote 缺 low_limit + last_price（停牌/行情源异常）→ 无价不能挂卖单（盲单=卖错价=
        致命）。跳过该标的但【仍消费标记文件】：避免下次 pre_open 反复尝试同一无价标的；
        漏平（删了没卖成）由人工对账兜底，风控宁可漏平也不发盲单/重复卖超。
    """
    expired_path = os.path.join(os.environ["TRADE_PLAN_DIR"], "..", "expired_nopx.json")
    monkeypatch.setattr(engine, "_EXPIRED_POSITIONS_PATH", expired_path)
    with open(expired_path, "w", encoding="utf-8") as f:
        json.dump({"date": "2099-01-02", "expired": [
            {"symbol": "300001.SZ", "entry_date": "2099-01-01",
             "holding_days": 20, "max_holding": 15}]}, f)
    orders = [{"order": {"symbol": "300002.SZ", "qty": 100, "side": "buy", "price": 10.0},
               "stop_price": 9.0, "take_profit": 11.0}]
    trading_plan.save_plan("2099-01-02", orders)
    trading_plan.confirm_plan("2099-01-02")

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
    # 标记文件仍被消费（避免反复尝试无价标的）
    assert not os.path.exists(expired_path)


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
    from trading import position_book
    db_path = os.path.join(os.environ["TRADE_PLAN_DIR"], "..", "state.db")
    monkeypatch.setattr(position_book, "_DEFAULT_DB", db_path)
    position_book.init_db()
    today = datetime.now().strftime("%Y-%m-%d")
    position_book.snapshot_start_equity(today, 1_000_000.0)
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
    db_path = os.path.join(os.environ["TRADE_PLAN_DIR"], "..", "state.db")
    monkeypatch.setattr(position_book, "_DEFAULT_DB", db_path)
    position_book.init_db()
    today = datetime.now().strftime("%Y-%m-%d")
    position_book.snapshot_start_equity(today, 1_000_000.0)
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
# 4.10 Task 11（R-1 盘后 query_trades 兜底纠正）：post_close reconcile 后成交流水交叉校验
# ============================================================================
def test_post_close_query_trades_reconcile_drift(monkeypatch):
    """post_close ② query_trades 兜底：CSV 流水聚合 vs position_book drift → 以 CSV 为准重写 + 告警。

    物理意图（plan Task 11 · spec §5.1）：
        apply_fill 因 db lock/异常漏记（_handle_order_update 软降级），position_book 少记；
        record_live_trade 写 CSV 是独立 try-except，漏笔概率低于 apply_fill。post_close 用
        CSV 流水聚合 vs position_book，drift 以 CSV 为准重写 qty（分工：reconcile 查持仓 drift，
        本步查成交流水漏笔）。
    """
    from trading import position_book
    db_path = os.path.join(os.environ["TRADE_PLAN_DIR"], "..", "state.db")
    monkeypatch.setattr(position_book, "_DEFAULT_DB", db_path)
    position_book.init_db()
    # position_book 记 30（apply_fill 漏记致少记：实际成交 100）
    position_book.apply_fill("ord1", "A.SH", "BUY", 30, 10.0, "2099-01-01 10:00:00")
    today = datetime.now().strftime("%Y-%m-%d")
    position_book.snapshot_start_equity(today, 1_000_000.0)   # 熔断基线（0% 不触发）

    # mock service.query_trades 返当日成交 100@10（CSV 流水权威）
    def _fake_qt(start, end, **kw):
        return {"trades": [{"symbol": "A.SH", "direction": "BUY", "shares": 100.0,
                            "price": 10.0}],
                "total": 1, "limit": 1000, "offset": 0}
    monkeypatch.setattr(
        "presentation.server.services.trading_service.query_trades", _fake_qt)

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

    assert result.get("trades_reconciled") == 1
    # 验 position_book 已以 CSV 为准纠正为 100
    assert position_book.get_local_positions().get("A.SH") == 100.0


def test_post_close_query_trades_no_drift_is_noop(monkeypatch):
    """post_close：CSV 聚合 == position_book → 无 drift 不重写（trades_reconciled 不设）。"""
    from trading import position_book
    db_path = os.path.join(os.environ["TRADE_PLAN_DIR"], "..", "state.db")
    monkeypatch.setattr(position_book, "_DEFAULT_DB", db_path)
    position_book.init_db()
    position_book.apply_fill("ord1", "A.SH", "BUY", 100, 10.0, "2099-01-01 10:00:00")
    today = datetime.now().strftime("%Y-%m-%d")
    position_book.snapshot_start_equity(today, 1_000_000.0)

    def _fake_qt(start, end, **kw):
        return {"trades": [{"symbol": "A.SH", "direction": "BUY", "shares": 100.0,
                            "price": 10.0}],
                "total": 1, "limit": 1000, "offset": 0}
    monkeypatch.setattr(
        "presentation.server.services.trading_service.query_trades", _fake_qt)

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

    assert "trades_reconciled" not in result        # 无 drift 不重写
    assert position_book.get_local_positions().get("A.SH") == 100.0   # 未改


def test_post_close_query_trades_skipped_when_no_gw(monkeypatch):
    """post_close：gw=None（dry_run）→ 跳过 query_trades 兜底（无网关无成交可对）。

    物理意图（边界）：dry_run 下 gw=None，无真实成交，CSV 兜底无意义（且避免无网关时
        误读 CSV 老数据重写账本）。gw=None 跳过 ② 段，与 ① reconcile 同口径。
    """
    from trading import position_book
    # 隔离 position_book db（防读生产账本 + 误归零真实持仓——live 前影子数据亦不应被测试污染）
    db_path = os.path.join(os.environ["TRADE_PLAN_DIR"], "..", "state.db")
    monkeypatch.setattr(position_book, "_DEFAULT_DB", db_path)
    position_book.init_db()
    # 确保 gw 全程 None：post_close 内部 ``if gw is None: gw = get_gateway()`` 兜底也要返 None，
    # 模拟 dry_run 无网关（否则若测试环境残留 gw 单例会误触发 ② 段读 CSV 重写账本）。
    monkeypatch.setattr(engine, "get_gateway", lambda: None)

    qt_calls = []

    def _fake_qt(*a, **kw):
        qt_calls.append(True)
        return {"trades": [], "total": 0}

    monkeypatch.setattr(
        "presentation.server.services.trading_service.query_trades", _fake_qt)

    today = datetime.now().strftime("%Y-%m-%d")
    result = asyncio.run(engine.post_close(today))   # gw=None → get_gateway 也 None

    assert qt_calls == []                             # gw=None → 未调 query_trades
    assert "trades_reconciled" not in result
