# -*- coding: utf-8 -*-
"""EnginePorts：phases 外迁函数的窄依赖接口（T1 缝合点 #1 解）。

仅承载 engine **实例特有**、原经 ``_ACTIVE_ENGINE`` 单例桥访问的依赖（盘前三段闸 gate +
动态白名单注入/清空）。项目级单例（state_store / lake / gateway）保持模块级访问，phases
直接 import —— 不越界 T6（state_store SSoT 演进），亦不经 Ports 注入（窄接口红线）。

物理意图（spec · engine 模块化拆分 T1）：
    phases 外迁（T6-T8）把 pre_open / stop_loss / post_close 搬到独立模块后，这些模块级
    函数无法再反查 TradingEngine 实例（原经模块级 ``_ACTIVE_ENGINE`` 全局单例）。EnginePorts
    把「实例特有」的三个依赖收敛成一个显式 dataclass 参数，调用方（engine cron wrapper /
    catchup 补跑）注入 ``self._ports``，依赖方向由隐式全局反查变为显式参数透传。

    三个依赖逐一对齐原 ``_ACTIVE_ENGINE`` 路径的语义（行为零变更）：
        gate            ← TradingEngine._pre_open_gate（盘前三段闸，返 (ok, reason)）
        whitelist_add   ← self._dynamic_whitelist.update（set 原地并集，对齐原 ``|=``）
        whitelist_clear ← self._dynamic_whitelist.clear（set 原地清空，对齐原 ``.clear()``）
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable


@dataclass
class EnginePorts:
    """phases 外迁函数依赖的 engine 实例窄接口（缝合点 #1）。

    Attributes:
        gate: 盘前三段闸（plan-confirmed → gateway-health → data-ready），任一未绿早返
            skip payload，绝不触达网关写操作。绑定 ``TradingEngine._pre_open_gate``：
            ``async (date, gw) -> (ok: bool, reason: str)``。
        whitelist_add: pre_open 把当日计划标的注入 ``self._dynamic_whitelist``（set 原地
            并集语义）。接收任意 symbol 迭代器（生产传 set，测试可传 list）。
        whitelist_clear: post_close 清空 ``self._dynamic_whitelist``，保证下一交易日从
            干净状态开始（与 pre_open 注入对称的物理隔离机制）。
    """

    gate: Callable[[str, Any], Awaitable[tuple[bool, str]]]
    whitelist_add: Callable[[Iterable[str]], None]
    whitelist_clear: Callable[[], None]
