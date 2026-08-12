# -*- coding: utf-8 -*-
"""stop_loss_monitor 切 decide_exit + pending cancel_on + tp 价格同源验证（Task 9 · U6）。

物理定位（spec §2 目标 3/6 + §4.6 + D10/D11/D12 · 最高风险 · 盘中关键路径）：
    实盘 stop_loss_monitor（盘中止损巡检）切调 decide_exit（Task 4 的执行单源纯函数），
    让实盘与回测 simulate_exit 共用执行判定。补 pending cancel_on（D11）。
    盘中关键路径：漏止损/误止损/重复挂单 = 真金损失，should_trigger_stop fallback（D12）
    必留不裸奔，bar 用 xtdata 当日累积 high/low（R7 防误判）。

测试边界（Grill Me）：
    - 绝不真起 APScheduler、绝不真行情/真单：TradingEngine 仅实例化（装配 job 不 start）；
      stop_loss_monitor 内部 gw / get_quotes / _submit / load_plan / position_book 全 patch。
    - 6 测覆盖 decide_exit 四分支（STOP_LOSS/TIMEOUT/HOLD）+ D12 fallback + pending cancel_on
      + tp1/tp2 价格同源验证（build_orders cfg 对比 simulate_exit cfg）。

风控红线固化（R6/R7）：
    - D12 fallback：decide_exit 抛异常 → 降级 should_trigger_stop，盘中绝不裸奔；
    - R7 bar 防御：high/low 来自 xtdata 当日累积快照（get_quotes 返 tick.high/low），
      非单 tick last_price——避免漏判盘中摸高/探底。
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from trading.engine import TradingEngine, stop_loss_monitor, place_take_profit
# W1-A/T2：blackout 节流状态从 engine 模块级 (_last_quote_blackout_alert_ts) 迁 ports.blackout
# （QuoteBlackoutThrottle dataclass）——blackout 测试改经 ports 注入 + 重置节流。
from trading.alerting import QuoteBlackoutThrottle
from trading.ports import EnginePorts


# ----------------------------------------------------------------------------
# 公共 fixture：隔离 state_store / position_book DB（T10 幂等键 + C-4 U3b L1）
# ----------------------------------------------------------------------------
# 物理意图（测试卫生 · C-4 U6 发现的退化）：
#   stop_loss_monitor 主路径成功触发后会调 _record_stop → _state_store.insert_order
#   落 STOP 行（DB 幂等键，防下轮重发=双倍卖）。原测试无 DB 隔离 → _record_stop 直接
#   写生产 logs/trading_state.db（account 取 .env 真实 QMT_ACCOUNT_ID），同测试文件内
#   后续 case 再跑同 sym → _stop_already_placed 返 True 跳过 submit → stop_triggered=0
#   假失败。C-4 U3b 把 _stop_already_placed 的 except 从「回退 False」改为「raise L1」，
#   让这层既有 DB 依赖彻底暴露（无表=L1 停调度、有表污染=幂等跳过）。
#   修法（最小改动）：autouse 隔离 state_store._DEFAULT_DB 到 tmp_path + init_store，
#   每个 case 独立库，互不污染生产账本也不互相串读。
@pytest.fixture(autouse=True)
def _isolate_state_db(tmp_path, monkeypatch):
    from trading import state_store, position_book
    db_path = str(tmp_path / "stop_loss_state.db")
    monkeypatch.setattr(state_store, "_DEFAULT_DB", db_path)
    monkeypatch.setattr(position_book, "_DEFAULT_DB", db_path)
    state_store.init_store()


# ============================================================================
# 公共 fixture：构造一个 holding 期 monitor_ctx（state + cfg + 持仓 + quote）
# ============================================================================
SYM = "300001.SZ"


def _holding_ctx(*, stop=9.5, tp1=11.0, tp2=12.0, neckline=10.0, atr=0.5,
                 holding_days=3, is_last=False, max_holding=15,
                 stop_atr_mult=1.0, grace=5, step=0.1, floor=0.5,
                 tp1_portion=0.5):
    """构造 stop_loss_monitor 主路径需要的 monitor_ctx[symbol]。

    state/cfg 字段对齐 decide_exit 契约（execution.py:131-201）+ simulate_exit cfg
    （backtest.py:177-183）。stop 是【已盘后演进】的当日固定止损价（compute_stop_price
    盘后算好写 plan，盘中不移动，spec「盘中不调整」红线）。
    """
    return {
        "state": {
            "phase": "holding",
            "entry": 10.0,
            "stop": stop,           # plan 里的当日固定止损（已 trailing 演进）
            "tp1": tp1, "tp2": tp2,
            "neckline": neckline, "atr": atr,
            "holding_days": holding_days,
            "is_last": is_last,
            "lot1_open": True, "lot2_open": True,
        },
        "cfg": {
            "stop_atr_mult": stop_atr_mult,
            "trailing_grace": grace, "trailing_step": step, "trailing_floor": floor,
            "tp1_portion": tp1_portion, "max_holding": max_holding,
        },
    }


def _run_monitor(monitor_ctx, positions, quotes, submitted=None, gw=None,
                 pending_ctx=None, ports=None):
    """跑 stop_loss_monitor 一次，patch 所有外部副作用。

    - positions: gw._fetch_broker_positions 返回（{sym: {volume, avg_price}}）
    - quotes: qmt_market_data.get_quotes 返回（{sym: {last_price, high, low}}）
    - submitted: 收集 _submit 调用（list，append OrderRequest）
    - gw: 可注入 mock gw（None 时造一个 AsyncMock _fetch_broker_positions）
    - pending_ctx: {sym: cancel_on} pending 期撤单上下文（D11）
    - ports: W1-A/T2 注入的 EnginePorts（仅 blackout 测试需要传——含 QuoteBlackoutThrottle
      节流状态）。None 时 stop_loss_monitor 内 ports 守卫跳过 blackout 告警分支。
    """
    if gw is None:
        gw = MagicMock()
        gw._fetch_broker_positions = AsyncMock(return_value=positions)
    submitted_list: list = []

    async def fake_submit(order, *, confirm=True):
        if submitted is not None:
            submitted.append(order)
        return {"order_id": "fake", "state": "FILLED"}

    with patch("trading.engine.get_gateway", return_value=gw), \
         patch("trading.engine.calendar") as cal, \
         patch("trading.engine.qmt_market_data") as qmd, \
         patch("trading.engine._submit", new=fake_submit):
        # calendar.is_intraday_session 返 truthy（盘中）；is_trading_day 同理
        cal.is_intraday_session.return_value = True
        cal.is_trading_day.return_value = True
        qmd.get_quotes = AsyncMock(return_value=quotes)
        return asyncio.run(stop_loss_monitor(
            monitor_ctx=monitor_ctx, pending_ctx=pending_ctx, ports=ports))


# ============================================================================
# 测试 1：STOP_LOSS — low ≤ stop → decide_exit 返 CLOSE/STOP_LOSS → 发卖出单
# ============================================================================
def test_monitor_stop_loss_via_decide_exit():
    """low ≤ stop（plan 固定止损价）→ decide_exit STOP_LOSS → _submit 发卖出单。

    物理意图（resolution 2）：stop_loss_monitor 切 decide_exit 后，止损判定行为
    等价 should_trigger_stop（price≤stop → CLOSE/STOP_LOSS）。bar.low 取 xtdata 当日累积
    最低（R7 防御：非单 tick，避免漏判盘中探底穿止损后反弹）。
    """
    ctx = _holding_ctx(stop=9.5)
    submitted: list = []
    # quote.high/low 是 xtdata 当日累积（get_full_tick 返），close=last_price
    quotes = {SYM: {"last_price": 9.4, "high": 10.2, "low": 9.3}}
    positions = {SYM: {"volume": 100, "avg_price": 10.0}}
    result = _run_monitor({SYM: ctx}, positions, quotes, submitted=submitted)
    # 断言：触发 1 次，发了 1 张卖出单（qty=持仓 100，side=sell）
    assert result["stop_triggered"] == 1
    assert len(submitted) == 1
    assert submitted[0].side == "sell"
    assert submitted[0].qty == 100


# ============================================================================
# 测试 2：TIMEOUT — holding_days >= max_holding + is_last → CLOSE/TIMEOUT → 发卖
# ============================================================================
def test_monitor_timeout_via_decide_exit():
    """holding_days >= max_holding → is_last=True → decide_exit TIMEOUT → 发卖出单。

    物理意图（resolution 6）：实盘无「末根 K 线」概念，设
    is_last = (holding_days >= max_holding)（超期即当末根）→ decide_exit TIMEOUT
    触发 CLOSE/TIMEOUT。【不判浮盈 threshold】（brief 描述不精确，以 decide_exit is_last 为准，
    对齐 simulate_exit:193-199）。
    """
    ctx = _holding_ctx(stop=9.0, holding_days=15, is_last=True, max_holding=15)
    submitted: list = []
    # decide_exit 内部 compute_stop_price(15 天, grace=5)：eff_mult=max(1.0-(15-5)×0.1,0.5)=0.5
    # → stop=10-0.5×0.5=9.75。low=9.8 > 9.75（不破止损），high=10.2 < tp1=11.0（未触止盈）
    # → 进 is_last TIMEOUT 分支。
    quotes = {SYM: {"last_price": 9.8, "high": 10.2, "low": 9.8}}
    positions = {SYM: {"volume": 200, "avg_price": 10.0}}
    result = _run_monitor({SYM: ctx}, positions, quotes, submitted=submitted)
    assert result["stop_triggered"] == 1
    assert len(submitted) == 1
    assert submitted[0].side == "sell"
    assert submitted[0].qty == 200


# ============================================================================
# 测试 3：HOLD — 未触发任何条件 → decide_exit HOLD → 跳过不发单
# ============================================================================
def test_monitor_hold_when_no_trigger():
    """low > compute_stop_price(算出的 trailing stop) 且 high < tp1 且非 is_last → HOLD → 不发单。

    物理意图：decide_exit 四分支均未触发 → 继续持有。stop_loss_monitor 不发卖单，
    不误动持仓（误止损 = 真金损失）。

    ⚠️ decide_exit 内部用 compute_stop_price 重算 trailing stop（不读 state["stop"]，
    state.stop 仅 _stoploss 观测/fallback 用）。holding_days=3 在 grace=5 内 →
    stop=base_stop=neckline-stop_atr_mult×ATR=10-1.0×0.5=9.5。故 low=9.6 > 9.5 才不破止损。
    """
    ctx = _holding_ctx(stop=9.5, tp1=11.0, tp2=12.0, holding_days=3,
                      is_last=False, max_holding=15, stop_atr_mult=1.0,
                      grace=5, step=0.1, floor=0.5)
    submitted: list = []
    # low=9.6 > compute_stop_price(9.5)（不破止损）；high=10.2 < tp1=11.0（未触止盈）；非 is_last
    quotes = {SYM: {"last_price": 10.0, "high": 10.2, "low": 9.6}}
    positions = {SYM: {"volume": 100, "avg_price": 10.0}}
    result = _run_monitor({SYM: ctx}, positions, quotes, submitted=submitted)
    assert result["stop_triggered"] == 0
    assert len(submitted) == 0


# ============================================================================
# 测试 4：D12 fallback — decide_exit 抛异常 → 降级 should_trigger_stop（不裸奔）
# ============================================================================
def test_monitor_decide_exit_fallback(monkeypatch):
    """decide_exit 抛异常 → 降级 should_trigger_stop(price, sp) 兜底（D12 红线）。

    物理意图（resolution 2 D12 红线 · 盘中不裸奔）：
        decide_exit 是 Task 4 新挂的纯函数，实盘首次切用它，state/bar/cfg 构造任何
        异常（compute_stop_price 除零、state 缺键、bar NaN 等）绝不能让止损监控整体
        崩溃——盘中持仓裸奔 = 真金损失。try-except 降级 should_trigger_stop(price, sp)
        （原 :684 逻辑，已证等价 decide_exit STOP_LOSS 分支），保底不裸奔。
        fallback 必须有日志告警（decide_exit 异常降级）。
    """
    ctx = _holding_ctx(stop=9.5)
    submitted: list = []
    quotes = {SYM: {"last_price": 9.4, "high": 10.2, "low": 9.3}}
    positions = {SYM: {"volume": 100, "avg_price": 10.0}}

    # patch decide_exit 抛异常（模拟 state/bar 构造错或 compute_stop_price 异常）
    import trading.engine as eng_mod
    with patch.object(eng_mod, "decide_exit", side_effect=RuntimeError("构造 state 错")):
        result = _run_monitor({SYM: ctx}, positions, quotes, submitted=submitted)
    # fallback 命中：should_trigger_stop(9.4, 9.5)=True → 发卖出单（不裸奔）
    assert result["stop_triggered"] == 1
    assert len(submitted) == 1
    assert submitted[0].side == "sell"
    assert result.get("fallback_used") == 1   # 告警计数


# ============================================================================
# 测试 5：tp1/tp2 价格同源 — _place_take_profit 读 plan 与 build_orders 同 mult
# ============================================================================
def test_place_take_profit_tp1_tp2_two_orders_and_same_source():
    """_place_take_profit 读 plan.tp1/tp2 挂两腿，价格与 build_orders_from_signals 同 cfg。

    物理意图（resolution 1 · 验证不重写）：
        _place_take_profit 的两腿逻辑（Task 1/8 已实现 use_two_legs），价格来自 plan orders
        的 tp1/tp2 字段。本测试验证【价格同源】——build_orders_from_signals 用 stop_cfg 的
        tp1_h_mult/tp_h_mult 算 tp1/tp2 落盘，_place_take_profit 读同 plan 挂单；
        simulate_exit 用 exec[id]_cfg 同 mult 算同价（backtest.py:125-126）。三处同 mult 同源。

    验证三步：
      1) build_orders 用 stop_cfg 算 tp1/tp2（mult=1.0/2.0，H=neckline-bottom）；
      2) _place_take_profit 读 plan 的 tp1/tp2 挂两张限价卖单（_submit × 2）；
      3) 挂单价 == build_orders 算出的 tp1/tp2（同源，无漂移）。
    """
    from strategies.neckline.signal import Signal
    from trading.compute.plan import build_orders_from_signals

    # 构造一个信号：neckline=10, bottom=8, atr=0.5, entry=10 → H=2
    # tp1 = 10 + 1.0×2 = 12.0；tp2 = 10 + 2.0×2 = 14.0（stop_cfg mult 同 NecklineConfig 默认）
    sig = Signal(symbol=SYM, entry_price=10.0, neckline=10.0, bottom=8.0, atr=0.5,
                 formed_at="2026-07-28")
    orders = build_orders_from_signals(
        [sig], capital=100000.0, pos_cap=0.05,
        atr_map={SYM: 0.5},
        stop_cfg={"stop_atr_mult": 1.0, "tp_h_mult": 2.0, "max_wait": 5,
                  "tp1_h_mult": 1.0, "tp1_portion": 0.5})
    assert len(orders) == 1
    o = orders[0]
    # build_orders 算出的 tp1/tp2（同 simulate_exit:125-126 的 c_star + mult×H）
    assert abs(o.tp1 - 12.0) < 1e-9
    assert abs(o.take_profit - 14.0) < 1e-9

    # 把 build_orders 结果落盘成 plan，再让 _place_take_profit 读它挂两腿
    plan = {
        "confirmed": True,
        "orders": [{
            "order": {"symbol": SYM, "qty": int(o.order.qty), "side": "buy",
                      "price": round(o.order.price, 2)},
            "stop_price": round(o.stop_price, 2),
            "take_profit": round(o.take_profit, 2),   # = tp2
            "tp1": round(o.tp1, 2),
            "tp1_portion": o.tp1_portion,
            "neckline": 10.0, "atr": 0.5,
        }],
    }
    eng = TradingEngine()
    submitted: list = []
    with patch("trading.engine.trading_plan.load_plan", return_value=plan), \
         patch("trading.engine._submit", new=AsyncMock(
             side_effect=lambda order, **kw: (submitted.append(order),
                                              {"state": "FILLED"})[1])):
        asyncio.run(place_take_profit(SYM, 1000, 10.0, order_id="ord-x"))

    # 两腿都挂了（tp1 portion=0.5 × 1000 = 500 整手；tp2 = 500）
    assert len(submitted) == 2
    prices = sorted(float(o.price) for o in submitted)
    # 同源断言：挂单价就是 build_orders 算的 tp1=12.0 / tp2=14.0（无漂移）
    assert prices == [12.0, 14.0]
    # 两腿合计 = filled_qty（全平）
    assert sum(int(o.qty) for o in submitted) == 1000


# ============================================================================
# 测试 6：pending cancel_on — 挂单等待期 high ≥ cancel_on → 撤买单（D11）
# ============================================================================
def test_pending_cancel_on_during_wait():
    """pending 期（挂买单未成交）high ≥ cancel_on → 撤买单（D11，对齐 simulate_exit:130）。

    物理意图（resolution 4 · D11 新增 · 当前实盘缺这环）：
        挂单等待期（pre_open 挂买单未成交期间），盘中监控 high≥cancel_on → 涨幅已兑现，
        回踩是退潮，撤买单不买（对齐 simulate_exit:130 skip_target_met）。cancel_on 价从
        plan orders（build_orders 落盘，颈线+cancel_thresh_mult×H）。

    验证：
      1) pending_ctx 注入 {sym: cancel_on}；
      2) quote.high ≥ cancel_on → 调 gw.cancel_order 撤该 sym 的 pending 买单；
      3) 不发卖出单（pending 期无持仓可卖）。
    """
    cancel_on = 11.5   # 颈线 10 + 0.75×H(H=2)，触线即撤
    pending_ctx = {SYM: cancel_on}
    quotes = {SYM: {"last_price": 11.8, "high": 11.8, "low": 11.0}}  # high ≥ cancel_on
    # pending 期无持仓（买单未成交），positions 空
    positions = {}
    cancelled: list = []

    gw = MagicMock()
    gw._fetch_broker_positions = AsyncMock(return_value=positions)
    # gw.query_orders(cancelable_only=True) 返该 sym 的 pending 买单
    gw.query_orders = AsyncMock(return_value=[
        {"order_id": "ord-pending-1", "stock_code": SYM, "order_type": 23,
         "order_status": 48, "state": "PENDING"}
    ])
    gw.cancel_order = AsyncMock(side_effect=lambda oid: (cancelled.append(oid),
                                                         MagicMock(order_id=oid,
                                                                   state="CANCELED",
                                                                   message="ok"))[1])
    # M2（T3）：撤单后调 _confirm_cancelled 确认终态。MagicMock 默认 hasattr 任意属性
    # 为 True 但返裸 MagicMock 不能 await → 会抛异常进 except 致 pending_cancelled 不增。
    # 显式设 AsyncMock(return_value=True) 让确认通过，测真实「撤单→确认→计数」路径。
    gw._confirm_cancelled = AsyncMock(return_value=True)

    result = _run_monitor({}, positions, quotes, submitted=[],
                          gw=gw, pending_ctx=pending_ctx)
    # 断言：撤了 1 单（pending 买单），未发卖单
    assert result.get("pending_cancelled") == 1
    assert len(cancelled) == 1
    assert cancelled[0] == "ord-pending-1"


# ============================================================================
# 测试 7（I-2）：TAKE_PROFIT skip — monitor 不发市价单，交 _place_take_profit 预挂限价单（D10）
# ============================================================================
def test_monitor_take_profit_skipped_for_premarked_limit():
    """high ≥ tp1 → decide_exit 返 CLOSE/TAKE_PROFIT/portion<1.0 → monitor **不发卖单**。

    物理意图（I-1 修正 · spec D10 物理边界 · reviewer 方案 A）：
        实盘止盈由 _place_take_profit（engine.py:1899）在买单成交时预挂 tp1+tp2 限价卖单
        撮合（D10 物理边界，spec §4.6）。monitor 是 holding 巡检，decide_exit 无状态——
        monitor_ctx.state.lot1_open/lot2_open 默认 True 不翻转（限价单成交无回报改 state），
        若 monitor 对 TAKE_PROFIT 分支发市价单：tp1 限价单成交后下次巡检 decide_exit 仍
        返 CLOSE/TAKE_PROFIT → monitor 再发 tp1 市价部分卖单 = 与已成交限价单重复（滑点
        差异 + broker 拒单风险）。

        故 monitor 收到 dec.reason == TAKE_PROFIT 时 **不发单 continue 跳过**（TP 完全交
        预挂限价单）。本测试验证此 skip 行为。

    验证：
      1) 构造 high≥tp1 的 bar（decide_exit priority 3 返 CLOSE/TAKE_PROFIT/portion<1.0）；
      2) 断言 monitor **不发卖单**（_submit 未被调，submitted 为空）；
      3) 断言 stop_triggered==0（未触发止损/超期市价单）。
    """
    ctx = _holding_ctx(stop=9.5, tp1=11.0, tp2=12.0, holding_days=3,
                      is_last=False, max_holding=15, tp1_portion=0.5)
    submitted: list = []
    # high=11.2 ≥ tp1=11.0 → decide_exit priority 3 命中 → CLOSE/TAKE_PROFIT/portion=0.5
    # （low=9.6 > compute_stop_price(9.5) 不破止损；非 is_last 不超期）
    quotes = {SYM: {"last_price": 11.2, "high": 11.2, "low": 9.6}}
    positions = {SYM: {"volume": 100, "avg_price": 10.0}}
    result = _run_monitor({SYM: ctx}, positions, quotes, submitted=submitted)
    # 断言：TAKE_PROFIT 被 skip（不发市价单，交 _place_take_profit 预挂限价单）
    assert result["stop_triggered"] == 0
    assert len(submitted) == 0


# ============================================================================
# Task D2（live-mainchain-fixes）：TP 漏挂盘中兜底（#10）
# ============================================================================
def test_tp_missing_places_fallback(monkeypatch):
    """decide_exit=TAKE_PROFIT 且 DB 无 TP1/TP2 → 盘中补挂（#10）。

    复用本文件既有 _holding_ctx/_run_monitor 夹具：autouse _isolate_state_db 保证
    DB 无任何 TP 行 → has_order(TP1/TP2) 均 False → 必须触发 place_take_profit 补挂。
    """
    from unittest.mock import patch as _patch

    ctx = _holding_ctx(stop=9.5, tp1=11.0, tp2=12.0, holding_days=3,
                       is_last=False, max_holding=15, tp1_portion=0.5)
    # high=11.2 ≥ tp1=11.0 → decide_exit priority 3 → CLOSE/TAKE_PROFIT/portion=0.5
    quotes = {SYM: {"last_price": 11.2, "high": 11.2, "low": 9.6}}
    positions = {SYM: {"volume": 100, "avg_price": 10.0}}
    placed = {"n": 0}

    async def _fake_place(symbol, filled_qty, fill_price, order_id):
        placed["n"] += 1

    with _patch("trading.engine.place_take_profit", new=_fake_place):
        result = _run_monitor({SYM: ctx}, positions, quotes)
    assert placed["n"] == 1, "TP 漏挂必须触发盘中补挂"
    assert result["stop_triggered"] == 0, "补挂走限价单路径，monitor 不发市价单"


# ============================================================================
# R2 降级告警：行情源整体失效（xtdata 黑屏）→ live CRITICAL，30min 节流
# W1-A/T2：节流状态从 engine 模块级 _last_quote_blackout_alert_ts 迁 ports.blackout
# （QuoteBlackoutThrottle dataclass）——经 ports 注入 + 重置节流（构造 QuoteBlackoutThrottle
# (last_ts=0.0) 等价原 monkeypatch("trading.engine._last_quote_blackout_alert_ts", 0.0)）。
# ============================================================================
def _make_ports_with_fresh_blackout():
    """造 EnginePorts 实例：blackout 重置 last_ts=0.0（首次必触发，对齐原模块级初值）。

    gate/whitelist_* 用 no-op lambda 占位（blackout 测试不触达 pre_open 路径）。
    """
    return EnginePorts(
        gate=lambda d, gw: (True, ""),
        whitelist_add=lambda syms: None,
        whitelist_clear=lambda: None,
        blackout=QuoteBlackoutThrottle(last_ts=0.0, interval=1800.0),
    )


def test_quote_blackout_alerts_critical_in_live(monkeypatch):
    """live 模式全标的 last_price 缺失 → CRITICAL（止损链路裸奔必须叫醒人）。"""
    monkeypatch.setenv("AUTO_TRADE_MODE", "live")
    ports = _make_ports_with_fresh_blackout()
    with patch("trading.engine._alert_critical") as alert:
        _run_monitor({SYM: _holding_ctx()}, {SYM: {"volume": 100}}, {SYM: None}, ports=ports)
    alert.assert_called_once()
    assert "行情源整体失效" in alert.call_args.args[0]


def test_quote_blackout_throttled_30min(monkeypatch):
    """30min 节流：连续两轮黑屏只推一条 CRITICAL（防告警风暴）。

    W1-A/T2：两次 _run_monitor 共享同一 ports 实例 → 同一 QuoteBlackoutThrottle →
    第一次 should_alert=True + mark(now1)，第二次 should_alert=False（now2-now1 < 1800）。
    """
    monkeypatch.setenv("AUTO_TRADE_MODE", "live")
    ports = _make_ports_with_fresh_blackout()
    with patch("trading.engine._alert_critical") as alert:
        _run_monitor({SYM: _holding_ctx()}, {SYM: {"volume": 100}}, {SYM: None}, ports=ports)
        _run_monitor({SYM: _holding_ctx()}, {SYM: {"volume": 100}}, {SYM: None}, ports=ports)
    alert.assert_called_once()


def test_quote_blackout_no_alert_when_price_valid(monkeypatch):
    """任一标的有价 → 不告警（行情源正常）。"""
    monkeypatch.setenv("AUTO_TRADE_MODE", "live")
    ports = _make_ports_with_fresh_blackout()
    quotes = {SYM: {"last_price": 10.0, "high": 10.2, "low": 9.8}}
    with patch("trading.engine._alert_critical") as alert:
        _run_monitor({SYM: _holding_ctx()}, {SYM: {"volume": 100}}, quotes, ports=ports)
    alert.assert_not_called()


def test_quote_blackout_no_alert_in_dry_run(monkeypatch):
    """dry_run 无真金风险 → 不告警（防影子模式误报）。"""
    monkeypatch.setenv("AUTO_TRADE_MODE", "dry_run")
    ports = _make_ports_with_fresh_blackout()
    with patch("trading.engine._alert_critical") as alert:
        _run_monitor({SYM: _holding_ctx()}, {SYM: {"volume": 100}}, {SYM: None}, ports=ports)
    alert.assert_not_called()
