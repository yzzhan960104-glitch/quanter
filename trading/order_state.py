"""订单状态机

职责：
1. 定义订单状态
2. 管理状态迁移
3. 处理异常情况（断线、限频、部分成交）

设计原则：
- 有限状态机（FSM）模式
- 显式状态迁移（不隐式跳转）
- 防范非法状态迁移
"""
from enum import Enum, auto
from typing import Dict, Any, Optional, Callable
from datetime import datetime


class OrderState(Enum):
    """
    订单状态枚举

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
    PARTIAL_CANCELLED = auto() # 部分取消
    REJECTED = auto()          # 已拒绝
    FAILED = auto()            # 失败（异常）


class OrderStateMachine:
    """
    订单状态机

    支持的状态迁移：
    1. 正常流程：PENDING -> SUBMITTED -> FILLED
    2. 部分成交：PENDING -> SUBMITTED -> PARTIAL_FILLED -> FILLED
    3. 取消：PENDING -> SUBMITTED -> CANCELLED
    4. 拒绝：PENDING -> SUBMITTED -> REJECTED
    5. 部分取消：PENDING -> SUBMITTED -> PARTIAL_FILLED -> PARTIAL_CANCELLED -> FILLED
    6. 异常处理：任何状态 -> FAILED
    """

    def __init__(self):
        """初始化状态机"""
        self.current_state = OrderState.PENDING
        self.order_id: Optional[str] = None
        self.order_info: Optional[Dict[str, Any]] = None
        self.callbacks: Dict[OrderState, Optional[Callable]] = {
            state: None for state in OrderState
        }

    def submit(self, order_info: Dict[str, Any]) -> bool:
        """
        提交订单

        参数：
            order_info: 订单信息字典

        返回：
            是否成功提交
        """
        if self.current_state != OrderState.PENDING:
            raise ValueError(f"当前状态 {self.current_state} 不支持提交订单")

        self.order_info = order_info
        self.order_id = f"ORDER_{datetime.now().strftime('%Y%m%d%H%M%S%f')}"

        # 状态迁移：PENDING -> SUBMITTED
        self._transition_to(OrderState.SUBMITTED)

        return True

    def fill(self, filled_shares: int, filled_price: float) -> bool:
        """
        成交（完全成交或部分成交）

        参数：
            filled_shares: 成交股数
            filled_price: 成交价格

        返回：
            是否成功更新状态
        """
        if self.current_state not in [OrderState.SUBMITTED, OrderState.PARTIAL_FILLED]:
            raise ValueError(f"当前状态 {self.current_state} 不支持成交")

        # 更新成交信息
        if "filled_shares" not in self.order_info:
            self.order_info["filled_shares"] = 0
        if "filled_price" not in self.order_info:
            self.order_info["filled_price"] = []

        self.order_info["filled_shares"] += filled_shares
        self.order_info["filled_price"].append(filled_price)

        # 判断是否完全成交
        if self.order_info["filled_shares"] >= self.order_info["shares"]:
            # 完全成交
            self._transition_to(OrderState.FILLED)
        else:
            # 部分成交
            self._transition_to(OrderState.PARTIAL_FILLED)

        return True

    def cancel(self) -> bool:
        """
        取消订单

        返回：
            是否成功取消
        """
        if self.current_state not in [OrderState.SUBMITTED, OrderState.PARTIAL_FILLED]:
            raise ValueError(f"当前状态 {self.current_state} 不支持取消")

        # 判断是否有部分成交
        if self.current_state == OrderState.PARTIAL_FILLED:
            # 部分取消
            self._transition_to(OrderState.PARTIAL_CANCELLED)
        else:
            # 完全取消
            self._transition_to(OrderState.CANCELLED)

        return True

    def reject(self, reason: str) -> bool:
        """
        拒绝订单

        参数：
            reason: 拒绝原因

        返回：
            是否成功拒绝
        """
        if self.current_state != OrderState.SUBMITTED:
            raise ValueError(f"当前状态 {self.current_state} 不支持拒绝")

        self.order_info["reject_reason"] = reason
        self._transition_to(OrderState.REJECTED)

        return True

    def fail(self, reason: str) -> bool:
        """
        失败（异常处理）

        参数：
            reason: 失败原因

        返回：
            是否成功标记为失败
        """
        self.order_info["fail_reason"] = reason
        self._transition_to(OrderState.FAILED)

        return True

    def register_callback(self, state: OrderState, callback: Callable):
        """
        注册状态回调

        参数：
            state: 状态
            callback: 回调函数
        """
        self.callbacks[state] = callback

    def _transition_to(self, new_state: OrderState):
        """
        状态迁移（内部方法）

        参数：
            new_state: 新状态
        """
        # 验证状态迁移是否合法
        if not self._is_valid_transition(self.current_state, new_state):
            raise ValueError(f"非法状态迁移: {self.current_state} -> {new_state}")

        # 记录状态迁移
        if "state_history" not in self.order_info:
            self.order_info["state_history"] = []

        self.order_info["state_history"].append({
            "from": self.current_state,
            "to": new_state,
            "time": datetime.now(),
        })

        # 更新状态
        self.current_state = new_state

        # 触发回调
        if self.callbacks[new_state] is not None:
            self.callbacks[new_state](self.order_info)

    def _is_valid_transition(self, from_state: OrderState, to_state: OrderState) -> bool:
        """
        验证状态迁移是否合法

        参数：
            from_state: 起始状态
            to_state: 目标状态

        返回：
            是否合法
        """
        # 定义合法的状态迁移
        valid_transitions = {
            OrderState.PENDING: [OrderState.SUBMITTED],
            OrderState.SUBMITTED: [OrderState.PARTIAL_FILLED, OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED, OrderState.FAILED],
            OrderState.PARTIAL_FILLED: [OrderState.PARTIAL_FILLED, OrderState.FILLED, OrderState.PARTIAL_CANCELLED, OrderState.FAILED],
            OrderState.PARTIAL_CANCELLED: [OrderState.FILLED],
            OrderState.FILLED: [],  # 终态
            OrderState.CANCELLED: [],  # 终态
            OrderState.REJECTED: [],  # 终态
            OrderState.FAILED: [],  # 终态
        }

        return to_state in valid_transitions.get(from_state, [])

    def get_state(self) -> OrderState:
        """
        获取当前状态

        返回：
            当前状态
        """
        return self.current_state

    def get_order_info(self) -> Optional[Dict[str, Any]]:
        """
        获取订单信息

        返回：
            订单信息字典
        """
        return self.order_info

    def reset(self):
        """
        重置状态机
        """
        self.current_state = OrderState.PENDING
        self.order_id = None
        self.order_info = None