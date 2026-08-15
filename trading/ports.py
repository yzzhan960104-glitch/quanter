# -*- coding: utf-8 -*-
"""EnginePorts：phases 外迁函数的窄依赖接口（T1 缝合点 #1 解）。

仅承载 engine **实例特有**、原经 ``_ACTIVE_ENGINE`` 单例桥访问的依赖（盘前三段闸 gate +
动态白名单注入/清空）+ W1-A/T2 收口的「行情黑屏 30min 节流状态」。项目级单例
（state_store / lake / gateway）保持模块级访问，phases 直接 import —— 不越界 T6
（state_store SSoT 演进），亦不经 Ports 注入（窄接口红线）。

物理意图（spec · engine 模块化拆分 T1 + W1-A「模块级可变状态收口」红线）：
    phases 外迁（T6-T8）把 pre_open / stop_loss / post_close 搬到独立模块后，这些模块级
    函数无法再反查 TradingEngine 实例（原经模块级 ``_ACTIVE_ENGINE`` 全局单例）。EnginePorts
    把「实例特有」的三个依赖收敛成一个显式 dataclass 参数，调用方（engine cron wrapper /
    catchup 补跑）注入 ``self._ports``，依赖方向由隐式全局反查变为显式参数透传。

    W1-A/T2 追加（行情黑屏节流收口）：原 engine 模块级可变状态
    ``_last_quote_blackout_alert_ts`` + 常量 ``_QUOTE_BLACKOUT_ALERT_INTERVAL_S``（stop_loss
    原经 engine 反查读写）违反「模块级可变状态收口」红线——收敛为
    ``QuoteBlackoutThrottle`` dataclass 实例，经 ``EnginePorts.blackout`` 注入 stop_loss_monitor。
    依赖方向：engine 构造 ``QuoteBlackoutThrottle()`` 装入 ports.blackout → stop_loss 经
    ``ports.blackout.fire_if_due`` 原子读写——与 gate/whitelist 同口径（显式参数透传）。

    CR-3 追加（盘中组合级熔断评估节流 · 2026-08-15）：5min 评估节流 + 连续评估失败
    计数（miss_streak）同属跨轮巡检可变运行态，同红线同范式收敛为
    ``PortfolioBreakerThrottle`` dataclass 实例，经 ``EnginePorts.breaker_throttle``
    注入 stop_loss_monitor（依赖方向与 blackout 完全同构）。

    四个依赖逐一对齐原 ``_ACTIVE_ENGINE`` / 模块级路径的语义（行为零变更）：
        gate            ← TradingEngine._pre_open_gate（盘前三段闸，返 (ok, reason)）
        whitelist_add   ← self._dynamic_whitelist.update（set 原地并集，对齐原 ``|=``）
        whitelist_clear ← self._dynamic_whitelist.clear（set 原地清空，对齐原 ``.clear()``）
        blackout        ← 模块级 ``_last_quote_blackout_alert_ts`` + ``_INTERVAL``（节流状态机）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable

from trading.alerting import PortfolioBreakerThrottle, QuoteBlackoutThrottle


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
        blackout: 行情黑屏 30min 节流告警状态机（W1-A/T2 收口）。原 engine 模块级
            ``_last_quote_blackout_alert_ts`` + ``_QUOTE_BLACKOUT_ALERT_INTERVAL_S`` 经 engine
            反查读写的可变状态——现收敛为 ``QuoteBlackoutThrottle`` dataclass 实例，经 ports
            注入。仅 ``stop_loss_monitor`` 消费（live 全标的 last_price
            失效时推 CRITICAL，30min 节流防风暴）。默认 ``field(default_factory=...)`` 让
            未传 blackout 的旧调用方（测试裸调 / scan_expired / close_expired）自动装配
            默认实例，无破坏（blackout 默认 last_ts=0.0 + interval=1800.0 等价原模块初值）。
        breaker_throttle: 盘中组合级熔断 5min 评估节流 + 连续评估失败计数状态机
            （CR-3 · 2026-08-15「评估点前移」，与 blackout 同范式同注入方向）。仅
            ``stop_loss_monitor`` 消费（⑤ pending 撤单后 5min 节流评估「日内 -3% 组合
            熔断」，miss_streak ≥3 升级观测）。default_factory 保既有构造（engine
            :636 与全部测试构造 EnginePorts(...) 不传此字段也自动装配新实例）——
            状态生命周期绑定 engine 实例（每 TradingEngine 一份），非模块级单例。
            ⚠️ dataclass 规则：有默认字段必须在无默认字段之后——blackout/breaker_throttle
            放末尾合规。
    """

    gate: Callable[[str, Any], Awaitable[tuple[bool, str]]]
    whitelist_add: Callable[[Iterable[str]], None]
    whitelist_clear: Callable[[], None]
    # ⚠️ blackout/breaker_throttle 放最后（gate/whitelist_* 无默认值，有默认字段必须在无默认之后）。
    blackout: QuoteBlackoutThrottle = field(default_factory=QuoteBlackoutThrottle)
    breaker_throttle: PortfolioBreakerThrottle = field(default_factory=PortfolioBreakerThrottle)
