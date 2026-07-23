# -*- coding: utf-8 -*-
"""trading.compute.types — 决策层共享的【纯数据契约】值对象（frozen dataclass）。

物理定位（Layer2 阶段2 · spec §3.5/§4）：
    trading/compute/ 子包是 functional core（无 I/O、无状态、确定性），回测与实盘
    共用同一套决策逻辑。本模块集中放置被多个 compute 子模块共享的纯 dataclass
    值对象——它们的字段全部为基础类型，无 I/O、无副作用、可被任意 compute 子模块
    与 strategies / backtest 引用而不破坏 compute 零外部依赖不变量。

单源契约（strangler 铁律①：搬迁非复制）：
    OrderRequest 原物理定义在 trading/execution_gateway.py，被 risk_shield.check_order
    / signal_runner.build_orders_from_signals / 实盘执行链路大量共享。本阶段把其纯
    dataclass 主体迁到本模块（compute 自洽，无 execution 反向依赖）。Layer2 阶段6
    follow-up #4b 已删 execution_gateway 垫片，全仓消费点已直指
    ``from trading.compute.types import OrderRequest`` 真身。

    Why OrderResult 不进 compute：它字段含 OrderState 枚举（trading.order_state，
    状态机领域），属"执行态"而非"决策态"——留在 broker.base（I/O 域）。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class OrderRequest:
    """下单请求（与具体券商解耦的最小契约）。

    Why 最小化：只保留策略层真正需要的语义字段；券商私有的「最小手数/报价
    方式/股东代码」等参数留到子类适配层补充，避免基类被 QMT/同花顺等差异化
    字段污染。

    Why frozen：决策层传入后不可变，规避子类适配就地改写 qty/side 等风险参数
    造成回测/实盘口径分叉（早期 MacroAwareGateway 就地改 frozen.quantity 抛
    FrozenInstanceError 的反面教训——见 execution_gateway.py 尾部注释）。
    """

    symbol: str
    qty: float
    side: str                              # "buy" / "sell"
    price: float | None = None             # None=市价；有值=限价
    order_id: str | None = None            # 由调用方透传的客户端单号
