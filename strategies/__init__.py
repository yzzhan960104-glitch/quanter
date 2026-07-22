# -*- coding: utf-8 -*-
"""strategies 包：策略中性接口 + 各策略实现（颈线法）。

架构：execution/backtest_replay.replay(price_data, strategy, ...) 依赖 Strategy Protocol，
不 import 具体策略。新增策略 = 实现 strategies.base.Strategy + @register_strategy 装饰 +
在本 __init__.py import 该模块触发注册。

注册表：颈线法（NecklineMethodStrategy，算法收口于 strategies/neckline/ 子包）。
注：caisen 形态（W底/头肩/三角形）已于 Layer2 解耦·Task 1.3 完整退役，颈线法是唯一活跃策略。
"""
from .base import Strategy, TRADE_REQUIRED_KEYS
from .registry import register_strategy, build_strategy, list_strategies

# import 各策略模块触发 @register_strategy 注册
from . import neckline_method  # noqa: F401  颈线法适配器（唯一活跃策略）

__all__ = [
    "Strategy",
    "TRADE_REQUIRED_KEYS",
    "register_strategy",
    "build_strategy",
    "list_strategies",
]
