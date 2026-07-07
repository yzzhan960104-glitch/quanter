"""真实交易模块：QMT 对接、订单状态机、风控挡板。

职责：
1. Mock 交易模拟层（第一优先级）
2. 订单状态机（处理断线、限频、部分成交）
3. QMT 实盘执行网关（xtquant 异步封装）
4. 风控挡板（纯函数，下单前 10 关校验）
5. 保证金敞口监控
"""

from .mock_broker import MockBroker
from .order_state import OrderStateMachine, OrderState
# QmtExecutionGateway 延迟 import：qmt_gateway 顶部 try/except 容错 import xtquant，
# 无 xtquant 的开发/CI 环境仍可正常加载（_XTQUANT_AVAILABLE=False 退化基类为 object）。
from .qmt_gateway import QmtExecutionGateway

__all__ = [
    "MockBroker",
    "OrderStateMachine",
    "OrderState",
    "QmtExecutionGateway",
]
