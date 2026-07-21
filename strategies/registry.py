# -*- coding: utf-8 -*-
"""策略工厂（按名字实例化策略 + cfg 覆盖）。

注册式：各策略模块用 @register_strategy("name") 装饰类，自动入全局表。
build_strategy(name, cfg_override, **kwargs) 按名字实例化。
新增策略 = 实现接口 + 装饰注册 + 在 __init__.py import 触发注册。
"""
from __future__ import annotations

from typing import Any, Optional

_STRATEGY_REGISTRY: dict = {}


def register_strategy(name: str):
    """策略类装饰器：注册到全局表（name → class）。"""
    def deco(cls):
        if name in _STRATEGY_REGISTRY:
            raise ValueError(f"策略名冲突: {name} 已注册为 {_STRATEGY_REGISTRY[name]}")
        _STRATEGY_REGISTRY[name] = cls
        return cls
    return deco


def build_strategy(name: str, cfg_override: Optional[dict] = None, **kwargs) -> Any:
    """按名字实例化策略。

    参数：
        name:          策略名（"caisen"=caisen 形态，"neckline"=颈线法）。
        cfg_override:  参数覆盖 dict（键必须在该策略 config_schema 的 model_fields 内）。
        **kwargs:      其他构造参数（caisen 需 risk/aum/trading_calendar）。
    """
    if name not in _STRATEGY_REGISTRY:
        raise ValueError(f"未知策略: {name}（已注册: {list(_STRATEGY_REGISTRY)})")
    return _STRATEGY_REGISTRY[name](cfg_override=cfg_override, **kwargs)


def list_strategies() -> list:
    """已注册策略名列表（供前端策略选择下拉）。"""
    return list(_STRATEGY_REGISTRY)
