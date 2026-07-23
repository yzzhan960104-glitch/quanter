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
from unittest.mock import AsyncMock, patch

from trading.engine import TradingEngine


def test_stoploss_injects_stop_prices_from_plan():
    """有活跃 confirmed 计划 → _stoploss 把 symbol→stop_price 注入 monitor。

    断言：``stop_loss_monitor(stop_prices={"300001.SZ": 9.5})`` 被精确调用，
    而非 None（现状 bug）。
    """
    eng = TradingEngine()
    # confirmed=True 的活跃计划：1 个标的 stop_price=9.5
    plan = {
        "confirmed": True,
        "orders": [
            {
                "order": {"symbol": "300001.SZ", "qty": 100, "side": "buy", "price": 10.0},
                "stop_price": 9.5,
                "take_profit": 12.0,
            }
        ],
    }
    with patch("trading.engine.trading_plan.load_plan", return_value=plan), \
         patch("trading.engine.calendar") as cal, \
         patch("trading.engine.stop_loss_monitor", new=AsyncMock()) as mon:
        # calendar 被 patch 成 MagicMock（is_intraday_session 返 truthy），
        # 但因 stop_loss_monitor 也被 AsyncMock 拦截，calendar 实际不会被真调到；
        # 此处 patch 仅保持环境洁净 + 对齐 brief 给的口径。
        asyncio.run(eng._stoploss())
    # 断言 stop_prices 被注入（非 None）：symbol→stop_price 精确映射
    _, kwargs = mon.call_args
    assert kwargs.get("stop_prices") == {"300001.SZ": 9.5}


def test_stoploss_no_plan_injects_none():
    """无计划 / load_plan 返 None → 注入 None（monitor 内部 no-op，不崩、不盲卖）。

    物理边界（保守降级红线）：无 confirmed 计划时绝不能构造出非空 stop_prices，
    否则 monitor 拿脏数据误判跌破 → 盲卖。此处断言 stop_prices ∈ {None, {}}。
    """
    eng = TradingEngine()
    with patch("trading.engine.trading_plan.load_plan", return_value=None), \
         patch("trading.engine.stop_loss_monitor", new=AsyncMock()) as mon:
        asyncio.run(eng._stoploss())
    _, kwargs = mon.call_args
    assert kwargs.get("stop_prices") in (None, {})
