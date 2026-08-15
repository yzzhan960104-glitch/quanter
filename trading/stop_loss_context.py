# -*- coding: utf-8 -*-
"""StopLossContext —— 止损监控三 map 上下文容器（tech-debt M2 · 2026-08-15）。

物理意图（M2 收口）：engine._stoploss → stop_loss_monitor 的调用面原先散传三个同形
dict（stop_prices / monitor_ctx / pending_ctx），三者的派生一致性（同一张 confirmed
SIGNAL meta 单源，见 engine._stoploss「Task 9 · U6」构造块）只靠调用点自觉——多一个
消费方（如 tests/e2e_long_cycle/orchestrator）就多一处散传漂移面。本 frozen dataclass
把三 map 装箱为单一值对象，让「三 map 是一组语义整体」在类型上显形：

- ``stop_prices``：{symbol: stop_price}——D12 fallback 兜底比价基准（decide_exit
  异常时退回 should_trigger_stop 用此比价；monitor_ctx 未注入时退化为纯旧路径）；
- ``monitor_ctx``：{symbol: {"state": dict, "cfg": dict}}——decide_exit 主路径输入，
  字段契约详见 stop_loss_monitor docstring；
- ``pending_ctx``：{symbol: cancel_on}——D11 pending 期撤单阈值（pending 买单当日
  累积 high ≥ cancel_on 撤单）。

行为等价红线（spec M2 原文）：本容器是**纯数据收口**，不改三 map 任何语义——
「空 dict → None」的 no-op 归一延续旧调用点契约（统一在 stop_loss_monitor 解包处做），
构造点（engine._stoploss）与消费点（monitor 体内）逻辑零变更；若未来任何改动需要
在这里加校验/变换/方法，即触碰状态机语义，应先对照 spec 降级条款重新评审。

Why frozen：防下游偷偷塞第四个 map 或改字段逃逸收口——扩字段必须改本文件（评审可见），
与「三 map 单源派生」的收口意图配套。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional


@dataclass(frozen=True)
class StopLossContext:
    """止损监控三 map 上下文（frozen 值对象 · 派生关系与红线见模块 docstring）。"""

    # D12 fallback 基准：{symbol: stop_price}。值必须「能拿来比价」（构造点对
    # symbol 缺失/stop_price 非数已双重防御，此处不做二次校验——保持纯数据）。
    stop_prices: Optional[Mapping[str, float]] = None
    # decide_exit 主路径：{symbol: {"state": {...}, "cfg": {...}}}。state/cfg 字段
    # 对齐 execution.decide_exit 契约 + simulate_exit cfg（backtest.py）。
    monitor_ctx: Optional[Mapping[str, Mapping[str, Any]]] = None
    # D11 pending 期撤单：{symbol: cancel_on}。仅 plan 显式落盘 cancel_on 的标的入
    # （None/缺失不入=放飞不撤，向后兼容）。
    pending_ctx: Optional[Mapping[str, float]] = None
