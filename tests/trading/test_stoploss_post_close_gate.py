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

W1-A/T2-Task13 patch 物理路径迁移（_stoploss/_post_close gate 调用链归属判定）：
    本文件 4 测全调 ``eng._stoploss()`` / ``eng._post_close()``——TradingEngine 实例
    方法（engine.py:1393 / 1562）。gate 路径上的符号（get_gateway/_mode/_alert_critical）
    均在 **engine 方法函数体内作模块全局名读取** → 经 engine ``__globals__`` 解析。下游
    phases 函数（stop_loss_monitor/post_close）要么被 patch 哨兵拦截（C4），要么 gw 锁态
    早返根本未触达 → phases ``__globals__`` 全程不经，无需双口子 patch。按「符号被读取时
    的 ``__globals__`` 归属模块」判 → **全 21 patch 保 trading.engine.X 不迁**：

    - ``get_gateway`` ×4（C3 · 保 engine）：_stoploss:1431 / _post_close:1566 读
      ``gw = get_gateway()``，engine 模块全局名 → engine ``__globals__``。engine.py:367
      自定义 wrapper（透传 gateway_service）即物理命中点；phases.stop_loss:217 的
      get_gateway 在本组测里因 monitor 被 mock/未触达永不执行。
    - ``_alert_critical`` ×4（C3 · 保 engine）：_stoploss:1437 / _post_close:1572（仅 gw
      锁态分支）读引擎模块全局名 → engine ``__globals__``。engine.py:93 顶部
      ``from trading.critical import _alert_critical`` re-export 命中 patch；critical
      物理真身不经（gate 路径不进 critical 模块全局名）→ 迁 critical 反 miss。
    - ``_mode`` ×2（C3 · 保 engine）：_stoploss:1436 / _post_close:1571 读引擎模块全局名
      → engine ``__globals__``。engine.py:96 顶部 re-export 命中（与 _alert_critical 同块）。
    - ``stop_loss_monitor`` ×2 / ``post_close`` ×2（C4 engine wrapper · 不迁）：engine.py:205
      / 234 顶部 ``from trading.phases.{stop_loss,post_close} import`` re-export；
      _stoploss:1554 ``await stop_loss_monitor(...)`` / _post_close:1585
      ``await post_close(...)`` 经 engine 模块全局名解析命中本 re-export → patch
      trading.engine.stop_loss_monitor / post_close 正确拦截（哨兵断言未触达/放行）。
    - ``calendar.is_trading_day`` ×4（B 共享属性 · 不迁）：calendar 是共享模块对象
      （engine.calendar IS trading.calendar IS phases.calendar），patch 属性路径改模块
      对象属性 → 全局命中。
    - ``trading_plan.load_plan`` ×2 / ``_state_store.list_signals_with_meta_by_plan_date``
      ×1（B 共享属性 · 不迁）：trading_plan / _state_store 同为共享模块对象，属性路径 patch
      全局命中（green 路径 _stoploss:1450/1408 经 engine 模块名读同对象）。

    绿门：4 passed（baseline 4 绿 → 仍 4 绿，零行为变更）。
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
         patch("trading.engine._mode", return_value="live"), \
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
         patch("trading.engine._state_store.list_signals_with_meta_by_plan_date",
               return_value=[]) as _ss_sig, \
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
         patch("trading.engine._mode", return_value="live"), \
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
