# -*- coding: utf-8 -*-
"""C-5 V4：_stoploss / _post_close 入口 _gw_health_gate 前置（锁态 skip+CRITICAL）。

物理意图（spec §4.3 · B 共享前置 gate）：
    两 job 在 @_critical_guard 后、交易日守卫前调 _gw_health_gate，网关锁态时
    _alert_critical + return 不跑业务（不调 stop_loss_monitor / post_close），
    不停调度（等 _health_guard 60s 自愈恢复 live）。与 _pre_open_gate 网关锁态
    skip+CRITICAL 同口径；与 _health_guard 不升 L1 自愈取向一致（C-4 决议）。

测试边界：
    gw 用 MagicMock 模拟锁态（_connected=False），patch stop_loss_monitor / post_close
    为哨兵断言「未被触达」。交易日守卫 patch is_trading_day=True（隔离 gate 与交易日）。
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from trading.engine import TradingEngine


def test_stoploss_gw_locked_skips_and_alerts():
    """gw 锁态（_connected=False）→ _stoploss skip+CRITICAL，不调 stop_loss_monitor。

    断言：① _alert_critical 被调（CRITICAL 推钉钉）；② stop_loss_monitor 未被调
    （不查 plan、不发卖）；③ 不停调度（无 _CriticalHalt 抛出，方法正常 return）。
    """
    eng = TradingEngine()
    gw = MagicMock()
    gw._connected = False   # 锁态：未连接
    with patch("trading.engine.get_gateway", return_value=gw), \
         patch("trading.engine._alert_critical") as ac, \
         patch("trading.engine.calendar.is_trading_day", return_value=True), \
         patch("trading.engine.stop_loss_monitor", new=AsyncMock()) as mon, \
         patch("trading.engine.trading_plan.load_plan") as lp:
        asyncio.run(eng._stoploss())   # 不抛 _CriticalHalt（gate skip 不停调度）
    ac.assert_called_once()       # CRITICAL 告警推送
    mon.assert_not_called()       # 业务未触达
    lp.assert_not_called()        # 连 plan 都不查（gate 在交易日守卫前更前）


def test_stoploss_gw_green_proceeds_to_monitor():
    """gw 绿（_connected=True + ready）→ _stoploss 放行调 stop_loss_monitor（回归）。

    隔离 gate 通过后下游仍走原逻辑（load_plan → stop_prices 注入 → monitor）。
    断言 stop_loss_monitor 被调（无计划则 stop_prices=None，但 monitor 一定被触达）。
    """
    eng = TradingEngine()
    gw = MagicMock()
    gw._connected = True
    gw.is_client_ready.return_value = True
    with patch("trading.engine.get_gateway", return_value=gw), \
         patch("trading.engine._alert_critical") as ac, \
         patch("trading.engine.calendar.is_trading_day", return_value=True), \
         patch("trading.engine.trading_plan.load_plan", return_value=None), \
         patch("trading.engine.stop_loss_monitor", new=AsyncMock()) as mon:
        asyncio.run(eng._stoploss())
    ac.assert_not_called()        # gate 绿不告警
    mon.assert_called_once()      # 业务放行


def test_post_close_gw_locked_skips_and_alerts():
    """gw 锁态 → _post_close skip+CRITICAL，不调 post_close（不对账）。"""
    eng = TradingEngine()
    gw = MagicMock()
    gw._connected = False
    with patch("trading.engine.get_gateway", return_value=gw), \
         patch("trading.engine._alert_critical") as ac, \
         patch("trading.engine.calendar.is_trading_day", return_value=True), \
         patch("trading.engine.post_close", new=AsyncMock()) as pc, \
         patch("trading.position_book.get_local_positions", return_value={}):
        asyncio.run(eng._post_close())
    ac.assert_called_once()
    pc.assert_not_called()        # 对账业务未触达


def test_post_close_gw_green_proceeds():
    """gw 绿 → _post_close 放行调 post_close（回归）。"""
    eng = TradingEngine()
    gw = MagicMock()
    gw._connected = True
    gw.is_client_ready.return_value = True
    with patch("trading.engine.get_gateway", return_value=gw), \
         patch("trading.engine._alert_critical") as ac, \
         patch("trading.engine.calendar.is_trading_day", return_value=True), \
         patch("trading.engine.post_close", new=AsyncMock()) as pc, \
         patch("trading.position_book.get_local_positions", return_value={}):
        asyncio.run(eng._post_close())
    ac.assert_not_called()
    pc.assert_called_once()
