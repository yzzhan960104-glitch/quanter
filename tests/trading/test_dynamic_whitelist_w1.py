# -*- coding: utf-8 -*-
"""W1 白名单实例属性化（C-2 scheduling-orchestration Task 4）。

物理意图（Why this test exists）：
    二期 engine 与 server 即将合并进同进程后，模块级 ``_DYNAMIC`` 全局会被
    engine 注入的标的污染 server 手动下单路径（破坏向后兼容红线）。改造方案：
    engine 自带 ``self._dynamic_whitelist`` 实例属性 + ``submit_order(whitelist=...)``
    显式参数透传；server 路径不传 whitelist（仍走 ``get_effective_whitelist()`` = 纯 env，
    ``_DYNAMIC`` 恒空）——物理隔离靠参数，不靠全局态。

    本测试固化三条契约：
    1. ``static_env_whitelist()`` 是纯 env 解析（不掺 ``_DYNAMIC``）——供 engine 实例拼接
       ``self._dynamic_whitelist | static_env_whitelist()``。
    2. server 路径 ``get_effective_whitelist()`` 行为不变（``_DYNAMIC`` 留空时 = 纯 env）。
    3. ``TradingEngine._dynamic_whitelist`` 与模块级 ``_DYNAMIC`` 物理隔离——实例注入不污染全局。
"""
from trading.dynamic_whitelist import static_env_whitelist, get_effective_whitelist


def test_static_env_whitelist_pure_env(monkeypatch):
    monkeypatch.setenv("QMT_SYMBOL_WHITELIST", "600001.SH,688001.SH")
    assert static_env_whitelist() == {"600001.SH", "688001.SH"}


def test_get_effective_whitelist_still_works(monkeypatch):
    # server 路径：模块全局 _DYNAMIC 留空，get_effective 返纯 env（向后兼容不变）
    monkeypatch.setenv("QMT_SYMBOL_WHITELIST", "600001.SH")
    from trading import dynamic_whitelist
    dynamic_whitelist.clear_dynamic_whitelist()  # 确保模块全局空
    assert get_effective_whitelist() == {"600001.SH"}


def test_engine_instance_whitelist_isolated():
    # engine 实例属性与模块全局物理隔离
    from trading.dynamic_whitelist import _DYNAMIC
    from trading.engine import TradingEngine
    eng = TradingEngine()
    eng._dynamic_whitelist.add("300001.SZ")
    assert "300001.SZ" in eng._dynamic_whitelist
    assert "300001.SZ" not in _DYNAMIC  # 模块全局不被污染
