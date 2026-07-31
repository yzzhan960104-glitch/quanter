# -*- coding: utf-8 -*-
"""C-5 V3：_gw_health_gate 共享前置 gate 单测（从 _pre_open_gate ② 段抽）。

物理意图（spec §4.1）：触发点业务前显式探测网关健康，锁态时返 (False, reason)
让调用方 skip+CRITICAL 不跑业务（防静默全失败）。从 _pre_open_gate ② 段抽离，
共享给 _stoploss/_post_close，三入口同口径（与 _health_guard 不升 L1 自愈取向一致）。

测试边界：gw 用 MagicMock 模拟（_connected / is_client_ready 返指定值），不真连网关。
"""
from __future__ import annotations

from unittest.mock import MagicMock

from trading.engine import TradingEngine


def test_gw_none_blocks():
    """gw=None → (False, '网关未连接')。"""
    eng = TradingEngine()
    ok, reason = eng._gw_health_gate(None)
    assert ok is False
    assert "网关" in reason


def test_gw_not_connected_blocks():
    """gw._connected=False → (False, '网关未连接')。"""
    eng = TradingEngine()
    gw = MagicMock()
    gw._connected = False
    ok, reason = eng._gw_health_gate(gw)
    assert ok is False
    assert "网关" in reason


def test_gw_connected_but_client_not_ready_blocks():
    """gw._connected=True 但 is_client_ready()=False → (False, '客户端未就绪')。"""
    eng = TradingEngine()
    gw = MagicMock()
    gw._connected = True
    gw.is_client_ready.return_value = False
    ok, reason = eng._gw_health_gate(gw)
    assert ok is False
    assert "客户端" in reason


def test_gw_all_green_passes():
    """gw._connected=True 且 is_client_ready()=True → (True, '')。"""
    eng = TradingEngine()
    gw = MagicMock()
    gw._connected = True
    gw.is_client_ready.return_value = True
    ok, reason = eng._gw_health_gate(gw)
    assert ok is True
    assert reason == ""
