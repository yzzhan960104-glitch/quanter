"""成本模型：滑点、手续费、印花税

职责：
1. 计算交易成本（佣金、印花税、过户费）
2. 模拟滑点（线性/对数模型）
3. 极端场景下的成本放大（流动性枯竭）

设计原则：
- 显式计算每一项成本（不黑盒）
- 滑点与订单规模相关
- 防范除以零
"""
import numpy as np
import pandas as pd
from typing import Literal, Optional


class CostModel:
    """
    成本模型

    成本结构（A股）：
    - 佣金：成交金额的 0.03%（双向）
    - 印花税：成交金额的 0.05%（仅卖出）
    - 过户费：成交金额的 0.001%（仅上海市场，双向）
    - 滑点：因市场深度不足导致的价格偏离
    """

    def __init__(
        self,
        commission_rate: float = 0.0003,
        stamp_duty: float = 0.0005,
        min_commission: float = 5.0,
        slippage_model: Literal["linear", "log"] = "linear",
        slippage_rate: float = 0.001,
        liquidity_threshold: float = 0.02
    ):
        """
        初始化成本模型

        参数：
            commission_rate: 佣金率（默认万三）
            stamp_duty: 印花税率（默认千五，仅卖出）
            min_commission: 最低佣金（默认 5 元）
            slippage_model: 滑点模型（"linear"/"log"）
            slippage_rate: 基础滑点率（默认 0.1%）
            liquidity_threshold: 流动性阈值（默认 2%）
        """
        self.commission_rate = commission_rate
        self.stamp_duty = stamp_duty
        self.min_commission = min_commission
        self.slippage_model = slippage_model
        self.slippage_rate = slippage_rate
        self.liquidity_threshold = liquidity_threshold

    def calculate_commission(self, amount: float) -> float:
        """
        计算佣金

        参数：
            amount: 成交金额

        返回：
            佣金金额

        公式：
        commission = max(成交金额 × 佣金率, 最低佣金)
        """
        commission = amount * self.commission_rate
        commission = max(commission, self.min_commission)
        return commission

    def calculate_stamp_duty(self, amount: float, is_sell: bool) -> float:
        """
        计算印花税

        参数：
            amount: 成交金额
            is_sell: 是否卖出（印花税仅收取卖出）

        返回：
            印花税金额

        公式：
        stamp_duty = 成交金额 × 印花税率（仅卖出）
        """
        if is_sell:
            return amount * self.stamp_duty
        else:
            return 0.0

    def calculate_transfer_fee(self, amount: float, symbol: str) -> float:
        """
        计算过户费（仅上海市场）

        参数：
            amount: 成交金额
            symbol: 交易标的代码

        返回：
            过户费金额

        公式：
        transfer_fee = 成交金额 × 0.001%（仅上海市场）
        """
        # 判断是否为上海市场（代码以 6 开头或 5 开头）
        if symbol.startswith(("6", "5")):
            return amount * 0.00001
        else:
            return 0.0

    def calculate_slippage(
        self,
        price: float,
        volume: float,
        avg_volume: float,
        is_buy: bool,
        liquidity_factor: float = 1.0
    ) -> float:
        """
        计算滑点

        参数：
            price: 基准价格
            volume: 交易成交量
            avg_volume: 平均成交量
            is_buy: 是否买入
            liquidity_factor: 流动性因子（1.0=正常，<1.0=流动性枯竭）

        返回：
            滑点后的价格

        滑点模型：
        - 线性模型：slippage = 基础滑点率 × (成交量 / 平均成交量) × 流动性因子
        - 对数模型：slippage = 基础滑点率 × log(1 + 成交量 / 平均成交量) × 流动性因子
        """
        # 计算成交量占比（防范除以零）
        volume_ratio = volume / avg_volume if avg_volume > 0 else 1.0

        # 计算滑点率
        if self.slippage_model == "linear":
            slippage_rate = self.slippage_rate * volume_ratio * liquidity_factor
        elif self.slippage_model == "log":
            slippage_rate = self.slippage_rate * np.log(1 + volume_ratio) * liquidity_factor
        else:
            raise ValueError(f"不支持的滑点模型: {self.slippage_model}")

        # 限制滑点率（防范极端值）
        slippage_rate = np.clip(slippage_rate, 0.0, 0.1)  # 最多 10% 滑点

        # 计算滑点后的价格
        if is_buy:
            # 买入：价格上涨
            slippage_price = price * (1 + slippage_rate)
        else:
            # 卖出：价格下跌
            slippage_price = price * (1 - slippage_rate)

        return slippage_price

    def calculate_liquidity_factor(
        self,
        current_volume: float,
        avg_volume: float
    ) -> float:
        """
        计算流动性因子

        参数：
            current_volume: 当前成交量
            avg_volume: 平均成交量

        返回：
            流动性因子（1.0=正常，<1.0=流动性枯竭）

        流动性枯竭定义：
        - 当前成交量 < 平均成交量的阈值，流动性因子 = 1 / (阈值 / 占比)
        """
        # 计算成交量占比
        volume_ratio = current_volume / avg_volume if avg_volume > 0 else 1.0

        # 流动性枯竭检测
        if volume_ratio < self.liquidity_threshold:
            # 流动性枯竭，滑点放大
            liquidity_factor = 1.0 / (self.liquidity_threshold / volume_ratio)
        else:
            # 流动性正常
            liquidity_factor = 1.0

        # 限制流动性因子（防范极端值）
        liquidity_factor = np.clip(liquidity_factor, 1.0, 10.0)

        return liquidity_factor

    def calculate_total_cost(
        self,
        price: float,
        volume: int,
        avg_volume: float,
        symbol: str,
        is_sell: bool,
        current_volume: Optional[float] = None
    ) -> dict:
        """
        计算总成本

        参数：
            price: 成交价格
            volume: 成交数量（股）
            avg_volume: 平均成交量
            symbol: 交易标的代码
            is_sell: 是否卖出
            current_volume: 当前成交量（可选，用于计算滑点）

        返回：
            成本字典（含明细）
        """
        # 计算成交金额
        amount = price * volume

        # 计算各项成本
        commission = self.calculate_commission(amount)
        stamp_duty = self.calculate_stamp_duty(amount, is_sell)
        transfer_fee = self.calculate_transfer_fee(amount, symbol)

        # 计算滑点
        if current_volume is not None:
            liquidity_factor = self.calculate_liquidity_factor(current_volume, avg_volume)
            slippage_price = self.calculate_slippage(
                price, volume, avg_volume, not is_sell, liquidity_factor
            )
            slippage_cost = abs(slippage_price - price) * volume
        else:
            slippage_cost = 0.0
            slippage_price = price

        # 总成本
        total_cost = commission + stamp_duty + transfer_fee + slippage_cost

        return {
            "amount": amount,
            "commission": commission,
            "stamp_duty": stamp_duty,
            "transfer_fee": transfer_fee,
            "slippage_cost": slippage_cost,
            "total_cost": total_cost,
            "slippage_price": slippage_price,
        }