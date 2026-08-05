# -*- coding: utf-8 -*-
"""_stoploss 从活跃计划注入 stop_prices（修现状 None 空转 · Task 7）。

测试边界（Grill Me · 控制器 scope #5）：
- 绝不真起 APScheduler、绝不做真行情/真单：TradingEngine 仅实例化（装配 4 job 不 start），
  ``stop_loss_monitor`` 与 ``trading_plan.load_plan`` 均 patch 拦截，断言注入参数。
- 现状（Task 7 前）：``_stoploss`` 恒传 ``stop_prices=None`` → stop_loss_monitor
  在 ``stop_prices`` 空判断处直接返「无止损价配置」no-op，**监控链路恒空转**（致命：
  持仓跌破止损价也不会触发卖出）。本测试固化「从活跃计划读 symbol→stop_price 注入」契约。

物理意图（Why）：
    cron 在盘中每 5 分钟触发 ``_stoploss``，必须把 T+1 日 pre_open 已挂单 + 人审
    confirmed 的活跃计划的 ``{symbol: stop_price}`` 注入 stop_loss_monitor，
    否则盘中止损监控拿不到止损价 → 永远跳过 → 持仓裸奔。
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from trading.engine import TradingEngine


def test_stoploss_injects_stop_prices_from_plan():
    """有活跃 confirmed 计划 → _stoploss 把 symbol→stop_price 注入 monitor。

    断言：``stop_loss_monitor(stop_prices={"300001.SZ": 9.5})`` 被精确调用，
    而非 None（现状 bug）。

    C2c：_stoploss 直读 DB list_signals_with_meta_by_plan_date（不再 load_plan），
    每个 SIGNAL 须 latest=CONFIRMED 才塞 stop_prices（确认闸 per-trade）。
    """
    eng = TradingEngine()
    # C2c：DB SIGNAL meta（shape = {symbol, **meta}）
    signals = [{
        "symbol": "300001.SZ",
        "order": {"symbol": "300001.SZ", "qty": 100, "side": "buy", "price": 10.0},
        "stop_price": 9.5, "take_profit": 12.0,
    }]
    # C-5 V4：_stoploss 入口先过 _gw_health_gate，须 patch get_gateway 返 connected+ready
    # gw 让 gate 放行，否则 gate skip 到不了 stop_prices 注入逻辑。
    gw = MagicMock()
    gw._connected = True
    gw.is_client_ready.return_value = True
    with patch("trading.engine.get_gateway", return_value=gw), \
         patch("trading.engine.calendar") as cal, \
         patch("trading.engine.stop_loss_monitor", new=AsyncMock()) as mon:
        # C2c：mock _state_store.list_signals + get_latest_action=CONFIRMED
        cal.is_trading_day.return_value = True
        cal.today.return_value = "2026-08-05"
        with patch("trading.engine._state_store") as ss:
            ss.list_signals_with_meta_by_plan_date.return_value = signals
            ss.build_trade_id.side_effect = lambda aid, sym, d: f"{aid}_{sym}_{d}"
            ss.get_latest_action.return_value = "CONFIRMED"
            asyncio.run(eng._stoploss())
    # 断言 stop_prices 被注入（非 None）：symbol→stop_price 精确映射
    _, kwargs = mon.call_args
    assert kwargs.get("stop_prices") == {"300001.SZ": 9.5}


def test_stoploss_no_plan_injects_none():
    """无计划 / list_signals 返空 → 注入 None（monitor 内部 no-op，不崩、不盲卖）。

    物理边界（保守降级红线）：无 confirmed 计划时绝不能构造出非空 stop_prices，
    否则 monitor 拿脏数据误判跌破 → 盲卖。此处断言 stop_prices ∈ {None, {}}。
    """
    eng = TradingEngine()
    # C-5 V4：补 connected+ready gw 让 gate 放行（见上用例同款）。
    gw = MagicMock()
    gw._connected = True
    gw.is_client_ready.return_value = True
    # 显式 patch is_trading_day=True 让测试与运行日无关
    with patch("trading.engine.get_gateway", return_value=gw), \
         patch("trading.engine.calendar.is_trading_day", return_value=True), \
         patch("trading.engine.clock.today", return_value="2026-08-05"), \
         patch("trading.engine.stop_loss_monitor", new=AsyncMock()) as mon:
        # C2c：list_signals 返空（无计划）
        with patch("trading.engine._state_store") as ss:
            ss.list_signals_with_meta_by_plan_date.return_value = []
            asyncio.run(eng._stoploss())
    _, kwargs = mon.call_args
    assert kwargs.get("stop_prices") in (None, {})


def test_stoploss_skips_non_trading_day():
    """非交易日 _stoploss 直接返回，不查 DB SIGNAL 不调 monitor（Task 8 fix · review I1）。

    物理意图：旧 stop_loss cron ``*/5 9-14 * * 1-5`` 的 ``1-5`` 限制工作日；Task 8
    迁 IntervalTrigger(seconds=30) 后丢掉工作日过滤，周末盘中时段也会触发。
    守卫补在 _stoploss 顶部（与 _eod/_pre_open/_post_close 同口径 is_trading_day），
    非交易日不查 SIGNAL、不调 monitor——避免无谓调用 + 不依赖 monitor 内 is_intraday_session
    兜底（该兜底只查时间不查工作日，挡不住周末）。
    """
    eng = TradingEngine()
    # C-5 V4：gate 在交易日守卫前，须 gw 绿让 gate 放行，才能测到「非交易日守卫」拦截。
    gw = MagicMock()
    gw._connected = True
    gw.is_client_ready.return_value = True
    with patch("trading.engine.get_gateway", return_value=gw), \
         patch("trading.engine.calendar.is_trading_day", return_value=False), \
         patch("trading.engine.stop_loss_monitor", new=AsyncMock()) as mon:
        # C2c：非交易日不应查 list_signals
        with patch("trading.engine._state_store") as ss:
            asyncio.run(eng._stoploss())
            ss.list_signals_with_meta_by_plan_date.assert_not_called()
    mon.assert_not_called()   # 非交易日不调 monitor
