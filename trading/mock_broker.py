"""Mock 交易模拟层

职责：
1. 模拟订单执行
2. 模拟滑点与部分成交
3. 模拟网络延迟与断线
4. 模拟限频

设计原则：
- 第一优先级：让您在不接入实盘的情况下测试策略
- 可配置的随机性（可复现）
- 显式处理所有异常情况
"""
import numpy as np
import pandas as pd
from typing import Dict, Any, Optional, List
from datetime import datetime
from .order_state import OrderStateMachine, OrderState
import time


class MockBroker:
    """
    Mock 交易模拟层

    功能：
    1. 接收订单（买入/卖出）
    2. 模拟订单执行（滑点、部分成交）
    3. 管理账户（现金、持仓）
    4. 模拟异常情况（断线、限频）
    """

    def __init__(
        self,
        initial_cash: float = 1_000_000,
        seed: Optional[int] = 42,
        partial_fill_prob: float = 0.1,
        connection_fail_prob: float = 0.01,
        rate_limit: int = 100  # 每分钟最多订单数
    ):
        """
        初始化 Mock 券商

        参数：
            initial_cash: 初始现金
            seed: 随机种子（确保可复现）
            partial_fill_prob: 部分成交概率
            connection_fail_prob: 断线概率
            rate_limit: 限频（每分钟订单数）
        """
        self.initial_cash = initial_cash
        self.cash = initial_cash
        self.positions: Dict[str, int] = {}  # {symbol: shares}
        self.rng = np.random.default_rng(seed)

        self.partial_fill_prob = partial_fill_prob
        self.connection_fail_prob = connection_fail_prob
        self.rate_limit = rate_limit

        # 订单记录
        self.orders: List[OrderStateMachine] = []
        self.order_timestamps: List[datetime] = []

        # 配置
        self.min_commission = 5.0
        self.commission_rate = 0.0003
        self.stamp_duty = 0.0005

    def place_order(
        self,
        symbol: str,
        direction: str,  # "buy" or "sell"
        shares: int,
        price: Optional[float] = None,
        order_type: str = "market"  # "market" or "limit"
    ) -> OrderStateMachine:
        """
        下单

        参数：
            symbol: 交易标的代码
            direction: 方向（"buy"/"sell"）
            shares: 股数
            price: 价格（限价单必须指定）
            order_type: 订单类型（"market"/"limit"）

        返回：
            订单状态机
        """
        # 检查限频
        if not self._check_rate_limit():
            # 超过限频，返回失败订单
            order = OrderStateMachine()
            order.submit({
                "symbol": symbol,
                "direction": direction,
                "shares": shares,
                "price": price,
                "order_type": order_type,
            })
            order.fail("超过限频")
            return order

        # 检查断线
        if self.rng.random() < self.connection_fail_prob:
            # 断线，返回失败订单
            order = OrderStateMachine()
            order.submit({
                "symbol": symbol,
                "direction": direction,
                "shares": shares,
                "price": price,
                "order_type": order_type,
            })
            order.fail("网络断线")
            return order

        # 创建订单
        order = OrderStateMachine()
        order_info = {
            "symbol": symbol,
            "direction": direction,
            "shares": shares,
            "price": price,
            "order_type": order_type,
            "submit_time": datetime.now(),
        }

        order.submit(order_info)
        self.orders.append(order)
        self.order_timestamps.append(datetime.now())

        return order

    def execute_order(
        self,
        order: OrderStateMachine,
        market_price: float,
        current_volume: float,
        avg_volume: float,
    ) -> bool:
        """
        执行订单（模拟成交）

        参数：
            order: 订单状态机
            market_price: 市场价格
            current_volume: 当前成交量
            avg_volume: 平均成交量

        返回：
            是否成功执行
        """
        if order.get_state() != OrderState.SUBMITTED:
            raise ValueError(f"订单状态 {order.get_state()} 不支持执行")

        order_info = order.get_order_info()

        # 计算滑点后的价格
        slippage_price = self._calculate_slippage(
            market_price=market_price,
            shares=order_info["shares"],
            avg_volume=avg_volume,
            direction=order_info["direction"],
            current_volume=current_volume,
        )

        # 判断是否部分成交
        if self.rng.random() < self.partial_fill_prob:
            # 部分成交
            filled_shares = int(order_info["shares"] * self.rng.uniform(0.3, 0.8))
            order.fill(filled_shares, slippage_price)

            # 更新账户
            self._update_account(order_info, filled_shares, slippage_price)

            # 剩余部分后续成交
            remaining_shares = order_info["shares"] - filled_shares
            order.fill(remaining_shares, slippage_price)

            # 更新账户
            self._update_account(order_info, remaining_shares, slippage_price)
        else:
            # 完全成交
            order.fill(order_info["shares"], slippage_price)

            # 更新账户
            self._update_account(order_info, order_info["shares"], slippage_price)

        return True

    def _update_account(self, order_info: Dict[str, Any], filled_shares: int, filled_price: float):
        """
        更新账户（现金、持仓）

        参数：
            order_info: 订单信息
            filled_shares: 成交股数
            filled_price: 成交价格
        """
        # 计算成交金额
        amount = filled_shares * filled_price

        # 计算佣金
        commission = max(amount * self.commission_rate, self.min_commission)

        # 计算印花税（仅卖出）
        stamp_duty = amount * self.stamp_duty if order_info["direction"] == "sell" else 0.0

        # 计算总成本
        total_cost = commission + stamp_duty

        # 更新账户
        if order_info["direction"] == "buy":
            # 买入
            self.cash -= (amount + total_cost)
            if order_info["symbol"] in self.positions:
                self.positions[order_info["symbol"]] += filled_shares
            else:
                self.positions[order_info["symbol"]] = filled_shares
        else:
            # 卖出
            self.cash += (amount - total_cost)
            if order_info["symbol"] in self.positions:
                self.positions[order_info["symbol"]] -= filled_shares

    def _calculate_slippage(
        self,
        market_price: float,
        shares: int,
        avg_volume: float,
        direction: str,
        current_volume: float,
    ) -> float:
        """
        计算滑点后的价格

        参数：
            market_price: 市场价格
            shares: 股数
            avg_volume: 平均成交量
            direction: 方向
            current_volume: 当前成交量

        返回：
            滑点后的价格
        """
        # 计算流动性因子
        liquidity_factor = 1.0
        if current_volume < avg_volume * 0.1:
            # 流动性枯竭，滑点放大
            liquidity_factor = 2.0

        # 计算滑点率（与订单规模相关）
        volume_ratio = shares / avg_volume if avg_volume > 0 else 1.0
        slippage_rate = 0.001 * volume_ratio * liquidity_factor

        # 限制滑点率（最多 1%）
        slippage_rate = min(slippage_rate, 0.01)

        # 计算滑点后的价格
        if direction == "buy":
            slippage_price = market_price * (1 + slippage_rate)
        else:
            slippage_price = market_price * (1 - slippage_rate)

        return slippage_price

    def _check_rate_limit(self) -> bool:
        """
        检查限频

        返回：
            是否在限频范围内
        """
        now = datetime.now()
        one_minute_ago = now - pd.Timedelta(minutes=1)

        # 统计最近一分钟的订单数
        recent_orders = sum(1 for ts in self.order_timestamps if ts >= one_minute_ago)

        return recent_orders < self.rate_limit

    def get_account_info(self) -> Dict[str, Any]:
        """
        获取账户信息

        返回：
            账户信息字典
        """
        return {
            "cash": self.cash,
            "positions": self.positions.copy(),
            "initial_cash": self.initial_cash,
        }

    def get_orders(self) -> List[Dict[str, Any]]:
        """
        获取所有订单

        返回：
            订单列表
        """
        return [order.get_order_info() for order in self.orders]

    def get_portfolio_value(self, prices: Dict[str, float]) -> float:
        """
        获取组合价值

        参数：
            prices: 当前价格字典（{symbol: price}）

        返回：
            组合价值
        """
        # 计算持仓价值
        position_value = sum(
            shares * prices.get(symbol, 0.0)
            for symbol, shares in self.positions.items()
        )

        # 组合价值 = 现金 + 持仓价值
        return self.cash + position_value

    def reset(self):
        """
        重置账户
        """
        self.cash = self.initial_cash
        self.positions = {}
        self.orders = []
        self.order_timestamps = []