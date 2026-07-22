# -*- coding: utf-8 -*-
"""trading.types.order_state — 订单状态枚举（纯数据契约单源）。

物理定位（Layer2 阶段5 · spec §3.5 五层第①层）：
    OrderState 是跨 trading 五层共享的【领域枚举】——
    - compute（reconcile 不直接用，但 OrderResult 字段引用）；
    - io（circuit_breaker.cancel_all_open_orders 终态集判定）；
    - orchestrate / state（订单状态机迁移合法性校验）。
    独立成层避免任一上层反向依赖（如 io 不该 import orchestrate，反之亦然），
    共享契约下沉到零依赖的 types/ 单源。

迁移路径（strangler 铁律① · 搬迁非复制）：
    trading/order_state.py（旧物理定义 · 与 OrderStateMachine 状态机混居）
        ──搬迁──▶ trading/types/order_state.py（本文件 · 纯枚举单源）
              ◀──垫片 re-export── trading/order_state.py（保既有调用零改动）

状态迁移路径（FSM 语义 · 物理意图保留）：
    PENDING -> SUBMITTED -> PARTIAL_FILLED -> FILLED
    PENDING -> SUBMITTED -> CANCELLED
    PENDING -> SUBMITTED -> REJECTED
    PENDING -> SUBMITTED -> PARTIAL_FILLED -> PARTIAL_CANCELLED -> FILLED
    ANY -> FAILED（异常处理）

依赖不变量：本模块仅依赖 stdlib（enum），零外部依赖（test_compute_purity 守护）。
"""
from __future__ import annotations

from enum import Enum, auto


class OrderState(Enum):
    """订单状态枚举（纯数据契约）。

    状态迁移路径：
    PENDING -> SUBMITTED -> PARTIAL_FILLED -> FILLED
    PENDING -> SUBMITTED -> CANCELLED
    PENDING -> SUBMITTED -> REJECTED
    PENDING -> SUBMITTED -> PARTIAL_FILLED -> PARTIAL_CANCELLED -> FILLED
    ANY -> FAILED（异常处理）
    """

    PENDING = auto()           # 待提交
    SUBMITTED = auto()         # 已提交
    PARTIAL_FILLED = auto()    # 部分成交
    FILLED = auto()            # 完全成交
    CANCELLED = auto()         # 已取消
    PARTIAL_CANCELLED = auto()  # 部分取消
    REJECTED = auto()          # 已拒绝
    FAILED = auto()            # 失败（异常）
