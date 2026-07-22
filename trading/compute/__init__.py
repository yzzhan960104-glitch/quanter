# -*- coding: utf-8 -*-
"""trading.compute — 交易决策 functional core（纯函数子包）。

物理定位（Layer2 阶段2 · spec §3.5/§4）：
    本子包集中所有交易决策的【纯函数】——无 I/O、无状态、确定性。回测与实盘
    共用同一套决策逻辑（杀手不变量：回测可改依赖 trading.compute，决策物理单源）。

    与 trading/ 顶层其他模块的职责切分（functional core / imperative shell）：
    - compute/*         纯判定/计算（本子包，零外部 I/O 依赖）
    - execution_gateway 网关 I/O（连接、下单、撤单、持仓拉取）——决策已下沉 compute
    - risk_shield       垫片 re-export compute.risk（保留旧路径兼容）
    - signal_runner     垫片 re-export compute.plan（保留旧路径兼容）
    - stop_loss         垫片 re-export compute.stop（保留旧路径兼容）
    - circuit_breaker   垫片 re-export（cancel_all→io.breaker + check_daily_loss_limit←compute.breaker）

    零外部依赖不变量（由 tests/test_compute_purity.py 守护）：
    本子包下所有 .py 仅可 import：标准库 / pandas / numpy / dataclasses /
    typing / strategies（Signal 等 frozen dataclass 纯数据契约）/ trading.types
    （若建）/ trading.compute 内部互引。禁止：broker / data / execution /
    trading.io / trading.orchestrate / requests / xtquant 等任何 I/O 库。

公开 API（按决策域分组）：
    - 离场判定：check_exit, ExitDecision, ExitAction, ExitReason（exit.py）
    - 风控挡板：check_order, RiskDecision（risk.py）
    - 下单计划：build_orders_from_signals, PlannedOrder（plan.py）
    - 止损系列：compute_stop_price, check_stop_loss, check_take_profit,
                update_trailing_stop（stop.py）
    - 持仓对账：reconcile, PositionDrift, ReconciliationResult（reconcile.py）
    - 熔断判定：check_daily_loss_limit（breaker.py）
    - 共享数据契约：OrderRequest（types.py）
"""
from __future__ import annotations

# ============================================================================
# 离场判定（exit.py）
# ============================================================================
from trading.compute.exit import (  # noqa: F401
    check_exit,
    ExitDecision,
    ExitAction,
    ExitReason,
)

# ============================================================================
# 风控挡板（risk.py）
# ============================================================================
from trading.compute.risk import (  # noqa: F401
    check_order,
    RiskDecision,
)

# ============================================================================
# 下单计划（plan.py）
# ============================================================================
from trading.compute.plan import (  # noqa: F401
    build_orders_from_signals,
    PlannedOrder,
)

# ============================================================================
# 止损系列（stop.py）
# ============================================================================
from trading.compute.stop import (  # noqa: F401
    compute_stop_price,
    check_stop_loss,
    check_take_profit,
    update_trailing_stop,
    should_trigger_stop,
)

# ============================================================================
# 持仓对账（reconcile.py）
# ============================================================================
from trading.compute.reconcile import (  # noqa: F401
    reconcile,
    PositionDrift,
    ReconciliationResult,
)

# ============================================================================
# 熔断判定（breaker.py）
# ============================================================================
from trading.compute.breaker import (  # noqa: F401
    check_daily_loss_limit,
)

# ============================================================================
# 共享数据契约（types.py）
# ============================================================================
from trading.compute.types import (  # noqa: F401
    OrderRequest,
)


__all__ = [
    # 离场判定
    "check_exit",
    "ExitDecision",
    "ExitAction",
    "ExitReason",
    # 风控挡板
    "check_order",
    "RiskDecision",
    # 下单计划
    "build_orders_from_signals",
    "PlannedOrder",
    # 止损系列
    "compute_stop_price",
    "check_stop_loss",
    "check_take_profit",
    "update_trailing_stop",
    "should_trigger_stop",
    # 持仓对账
    "reconcile",
    "PositionDrift",
    "ReconciliationResult",
    # 熔断判定
    "check_daily_loss_limit",
    # 共享数据契约
    "OrderRequest",
]
