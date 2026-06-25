"""回测引擎模块：事件驱动回测与压力测试

职责：
1. 核心回测引擎（事件驱动）
2. 成本模型（滑点、手续费、印花税）
3. 极端场景模拟（流动性枯竭、涨跌停板）
4. 收益指标计算与因子归因
5. 多资产组合调仓（基于 TargetWeightSignal）
"""

from .engine import BacktestEngine, Order, OrderSide
from .cost_model import CostModel
from .stress_test import StressTester
from .metrics import MetricsCalculator

__all__ = [
    "BacktestEngine",
    "Order",
    "OrderSide",
    "CostModel",
    "StressTester",
    "MetricsCalculator",
]