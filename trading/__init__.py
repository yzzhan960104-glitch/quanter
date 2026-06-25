"""真实交易模块：QMT 对接与订单状态机

职责：
1. Mock 交易模拟层（第一优先级）
2. 订单状态机（处理断线、限频、部分成交）
3. QMT 接口预留（未来扩展）
4. 保证金敞口监控
"""

from .mock_broker import MockBroker
from .order_state import OrderStateMachine, OrderState

__all__ = [
    "MockBroker",
    "OrderStateMachine",
    "OrderState",
]