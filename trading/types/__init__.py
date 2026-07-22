# -*- coding: utf-8 -*-
"""trading.types — 纯数据契约层（functional core 的值对象单源）。

物理定位（Layer2 阶段5 · spec §3.5 五层定型第①层）：
    本子包集中放置【零外部依赖】的纯数据契约——枚举、frozen dataclass、值对象。
    仅依赖 stdlib（enum/dataclasses/typing），不触 broker/data/io/orchestrate。

为什么独立一层（与 trading.compute.types 的分工）：
    - trading.compute.types：决策层共享的值对象（OrderRequest 等）——历史命名，阶段2已建。
    - trading.types（本包）：跨五层共享的【领域枚举与基础契约】单源——如 OrderState，
      被 compute（reconcile/OrderResult 字段）/ io（cancel 终态集）/ orchestrate
      （state 判定）共同消费，必须独立于任一上层，避免循环依赖。

单源契约（strangler 铁律①：搬迁非复制）：
    OrderState 原物理定义在 trading/order_state.py（与 OrderStateMachine 状态机混居）。
    本阶段把纯枚举迁到 types/order_state.py（领域契约单源），order_state.py 反向
    ``from trading.types.order_state import OrderState`` re-export——所有既有
    ``from trading.order_state import OrderState`` / ``from trading import OrderState``
    调用零改动继续可用（tests/test_compute_purity.py is 同源契约守护）。

依赖不变量（test_compute_purity.py 守护）：
    trading.types.* 的 import 仅允许：stdlib / numpy / pandas / strategies /
    trading.compute / trading.types 内部互引。禁 broker/data/io/orchestrate/asyncio。
"""
from __future__ import annotations

from trading.types.order_state import OrderState

__all__ = ["OrderState"]
