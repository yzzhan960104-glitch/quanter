# -*- coding: utf-8 -*-
"""strategies 包：策略中性接口 + 各策略实现（caisen 形态 / 颈线法）。

架构：execution/backtest_replay.replay(price_data, strategy, ...) 依赖 Strategy Protocol，
不 import 具体策略。新增策略 = 实现 strategies.base.Strategy + @register_strategy 装饰 +
在本 __init__.py import 该模块触发注册。

阶段 A：注册 caisen（CaisenPatternStrategy，包原 backtest_replay 形态逻辑，零行为变化）。
阶段 B：注册 neckline（NecklineMethodStrategy，挂 scripts/neckline_*）。
"""
from .base import Strategy, TRADE_REQUIRED_KEYS
from .registry import register_strategy, build_strategy, list_strategies

# import 各策略模块触发 @register_strategy 注册
from . import caisen_pattern  # noqa: F401  阶段A：caisen 形态适配器
from . import neckline_method  # noqa: F401  阶段B：颈线法适配器

__all__ = [
    "Strategy",
    "TRADE_REQUIRED_KEYS",
    "register_strategy",
    "build_strategy",
    "list_strategies",
]
