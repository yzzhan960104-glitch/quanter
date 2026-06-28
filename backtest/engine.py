"""回测引擎核心（事件驱动）

职责：
1. 执行回测流程
2. 记录每笔交易
3. 计算持仓与净值
4. 应用成本模型
5. 多资产组合调仓（基于 TargetWeightSignal）
6. A 股碎股处理（100 股整手向下取整）

设计原则：
- 事件驱动（而非向量化），便于处理复杂场景
- 显式记录每一笔交易（不黑盒）
- 支持多策略并行（预留）
- 多资产组合模式与单资产模式共存，互不干扰
"""
import math
from datetime import datetime
from dataclasses import dataclass, field
from enum import Enum, auto

import numpy as np
import pandas as pd
from typing import Dict, Any, Optional, List

from .cost_model import CostModel
from .metrics import MetricsCalculator
from factors.fusion import TargetWeightSignal, SignalDirection


# ============ 订单数据类 ============


class OrderSide(Enum):
    """订单买卖方向"""
    BUY = auto()
    SELL = auto()


@dataclass
class Order:
    """
    订单数据类

    回测引擎生成的原子交易指令，后续送入订单状态机执行。

    属性：
        order_id: 订单唯一标识（格式：ORDER_YYYYMMDDHHMMSSffffff）
        symbol: 交易标的代码（如 "510300.SH"）
        side: 买卖方向（BUY / SELL）
        shares: 订单股数（必为 100 的整数倍，由引擎保证）
        price: 订单价格（通常为开盘价）
        timestamp: 订单生成时间
        status: 订单状态（pending / submitted / filled / failed）
    """
    order_id: str
    symbol: str
    side: OrderSide
    shares: int
    price: float
    timestamp: pd.Timestamp
    status: str = "pending"

    def __post_init__(self):
        """初始化后验证订单合法性"""
        # A 股规则：订单股数必须为 100 的整数倍
        if self.shares <= 0:
            raise ValueError(f"订单股数必须为正整数，当前: {self.shares}")
        if self.shares % 100 != 0:
            raise ValueError(
                f"A 股规则：订单股数必须为 100 的整数倍，当前: {self.shares}"
            )
        if self.price <= 0:
            raise ValueError(f"订单价格必须为正数，当前: {self.price}")


# ============ 回测引擎 ============


class BacktestEngine:
    """
    回测引擎核心

    事件驱动架构：
    1. 逐日遍历数据
    2. 获取当前信号
    3. 调整仓位
    4. 计算成本
    5. 更新净值

    支持的场景：
    - 正常交易
    - 涨跌停板限制
    - 流动性枯竭
    - 持仓无法成交
    """

    def __init__(
        self,
        initial_capital: float = 1_000_000,
        cost_model: Optional[CostModel] = None,
        signal_freq: str = "1d"
    ):
        """
        初始化回测引擎

        参数：
            initial_capital: 初始资金
            cost_model: 成本模型（可选）
            signal_freq: 信号频率（"1d"/"1h"/"5m"/"1m"）
        """
        self.initial_capital = initial_capital
        self.cost_model = cost_model or CostModel()
        self.signal_freq = signal_freq

        # 回测状态（单资产模式，保持向后兼容）
        self.cash = initial_capital
        self.position = 0  # 持仓股数
        self.nav = initial_capital  # 净值

        # 多资产组合状态（组合调仓模式）
        # positions_dict: {symbol: 持仓股数}，如 {"510300.SH": 1000, "511010.SH": 500}
        self.positions_dict: Dict[str, int] = {}
        # latest_prices: {symbol: 最新收盘价}，用于计算 AUM
        self.latest_prices: Dict[str, float] = {}

        # 记录
        self.trades = []  # 交易记录
        self.daily_records = []  # 每日记录
        self.positions = []  # 持仓记录
        self.portfolio_orders: List[Order] = []  # 组合调仓订单记录

    def run(
        self,
        df: pd.DataFrame,
        signal: pd.Series,
        symbol: str = "600000.SH"
    ) -> Dict[str, Any]:
        """
        执行回测

        参数：
            df: OHLCV 数据
            signal: 信号序列（值在 [0, 1] 范围内）
            symbol: 交易标的代码

        返回：
            回测结果字典
        """
        # 重置状态
        self._reset_state()

        # 对齐数据与信号
        aligned_df = df.loc[signal.index]

        # 计算平均成交量（用于滑点计算）
        avg_volume = aligned_df["volume"].rolling(window=20).mean()
        avg_volume = avg_volume.fillna(aligned_df["volume"].mean())

        # 逐日遍历（事件驱动）
        for i, (date, row) in enumerate(aligned_df.iterrows()):
            # 获取当前信号
            current_signal = signal.loc[date]

            # 获取当前价格（使用开盘价交易）
            price = row["open"]

            # 获取平均成交量
            current_avg_volume = avg_volume.loc[date]

            # 调整仓位（核心逻辑）
            target_position = self._calculate_target_position(
                signal=current_signal,
                price=price,
                current_avg_volume=current_avg_volume,
                row=row
            )

            # 执行交易
            self._execute_trade(
                date=date,
                price=price,
                target_position=target_position,
                symbol=symbol,
                avg_volume=current_avg_volume,
                current_volume=row["volume"],
                row=row
            )

            # 更新每日净值
            self._update_daily_nav(date=date, row=row)

            # 记录每日状态
            self._record_daily_state(date, row, current_signal)

        # 计算最终结果
        result = self._calculate_result()

        return result

    def _reset_state(self):
        """重置回测状态（单资产 + 组合模式均重置）"""
        self.cash = self.initial_capital
        self.position = 0
        self.nav = self.initial_capital
        self.trades = []
        self.daily_records = []
        self.positions = []

        # 组合模式状态重置
        self.positions_dict = {}
        self.latest_prices = {}
        self.portfolio_orders = []

    def _calculate_target_position(
        self,
        signal: float,
        price: float,
        current_avg_volume: float,
        row: pd.Series
    ) -> int:
        """
        计算目标仓位

        参数：
            signal: 当前信号（0-1）
            price: 当前价格
            current_avg_volume: 平均成交量
            row: 当日数据

        返回：
            目标持仓股数
        """
        # 纯多头策略：信号 × 资金 / 价格
        target_value = signal * self.nav
        target_shares = int(target_value / price / 100) * 100  # 整百股（A股规则）

        return target_shares

    def _execute_trade(
        self,
        date: pd.Timestamp,
        price: float,
        target_position: int,
        symbol: str,
        avg_volume: float,
        current_volume: float,
        row: pd.Series
    ):
        """
        执行交易

        参数：
            date: 交易日期
            price: 成交价格
            target_position: 目标持仓
            symbol: 交易标的代码
            avg_volume: 平均成交量
            current_volume: 当前成交量
            row: 当日数据
        """
        # 计算需要交易的股数
        shares_to_trade = target_position - self.position

        if abs(shares_to_trade) < 100:
            # 交易量不足 100 股，不交易（最小交易单位）
            return

        # 判断买卖方向
        is_sell = shares_to_trade < 0
        shares_to_trade = abs(shares_to_trade)

        # 计算成本
        cost_info = self.cost_model.calculate_total_cost(
            price=price,
            volume=shares_to_trade,
            avg_volume=avg_volume,
            symbol=symbol,
            is_sell=is_sell,
            current_volume=current_volume
        )

        # 检查流动性枯竭（涨跌停板）
        if "high" in row and "low" in row:
            # 涨停：无法买入
            if not is_sell and abs(row["close"] - row["high"]) < 1e-6:
                self._record_failed_trade(
                    date=date,
                    reason="涨停无法买入",
                    shares=shares_to_trade,
                    price=price
                )
                return

            # 跌停：无法卖出
            if is_sell and abs(row["close"] - row["low"]) < 1e-6:
                self._record_failed_trade(
                    date=date,
                    reason="跌停无法卖出",
                    shares=shares_to_trade,
                    price=price
                )
                return

        # 执行交易
        trade_value = cost_info["amount"]  # 不包含滑点的成交金额
        actual_value = cost_info["slippage_price"] * shares_to_trade  # 包含滑点的成交金额

        if is_sell:
            # 卖出
            self.cash += actual_value - cost_info["total_cost"]
            self.position -= shares_to_trade
        else:
            # 买入
            if self.cash < actual_value + cost_info["total_cost"]:
                # 资金不足，无法全额买入
                # 计算可买入股数
                available_cash = self.cash - cost_info["total_cost"]
                shares_affordable = int(available_cash / cost_info["slippage_price"] / 100) * 100

                if shares_affordable < 100:
                    # 不足 100 股，不交易
                    self._record_failed_trade(
                        date=date,
                        reason="资金不足",
                        shares=shares_to_trade,
                        price=price
                    )
                    return

                # 部分成交
                self.cash -= shares_affordable * cost_info["slippage_price"] + cost_info["total_cost"]
                self.position += shares_affordable
                shares_to_trade = shares_affordable
            else:
                # 正常买入
                self.cash -= actual_value + cost_info["total_cost"]
                self.position += shares_to_trade

        # 记录交易
        self._record_trade(
            date=date,
            direction="sell" if is_sell else "buy",
            shares=shares_to_trade,
            price=cost_info["slippage_price"],
            cost=cost_info["total_cost"],
            symbol=symbol
        )

    def _update_daily_nav(self, date: pd.Timestamp, row: pd.Series):
        """
        更新每日净值

        参数：
            date: 日期
            row: 当日数据
        """
        # 使用收盘价计算持仓价值
        if self.position > 0:
            position_value = self.position * row["close"]
        else:
            position_value = 0.0

        # 更新净值
        self.nav = self.cash + position_value

    def _record_daily_state(self, date: pd.Timestamp, row: pd.Series, signal: float):
        """
        记录每日状态

        参数：
            date: 日期
            row: 当日数据
            signal: 当前信号
        """
        daily_record = {
            "date": date,
            "nav": self.nav,
            "cash": self.cash,
            "position": self.position,
            "position_value": self.position * row["close"],
            "price": row["close"],
            "signal": signal,
        }

        self.daily_records.append(daily_record)

    def _record_trade(
        self,
        date: pd.Timestamp,
        direction: str,
        shares: int,
        price: float,
        cost: float,
        symbol: str
    ):
        """
        记录交易

        参数：
            date: 交易日期
            direction: 方向（"buy"/"sell"）
            shares: 交易股数
            price: 成交价格
            cost: 交易成本
            symbol: 交易标的代码
        """
        trade = {
            "date": date,
            "direction": direction,
            "shares": shares,
            "price": price,
            "amount": shares * price,
            "cost": cost,
            "symbol": symbol,
        }

        self.trades.append(trade)

    def _record_failed_trade(
        self,
        date: pd.Timestamp,
        reason: str,
        shares: int,
        price: float
    ):
        """
        记录失败交易

        参数：
            date: 交易日期
            reason: 失败原因
            shares: 目标交易股数
            price: 目标价格
        """
        trade = {
            "date": date,
            "direction": "failed",
            "shares": shares,
            "price": price,
            "amount": shares * price,
            "cost": 0.0,
            "symbol": "N/A",
            "reason": reason,
        }

        self.trades.append(trade)

    def _calculate_result(self) -> Dict[str, Any]:
        """
        计算最终结果

        返回：
            回测结果字典
        """
        # 转换为 DataFrame
        daily_df = pd.DataFrame(self.daily_records)
        daily_df.set_index("date", inplace=True)

        # 计算收益率
        daily_df["return"] = daily_df["nav"].pct_change()

        # 计算累计收益率
        daily_df["cumulative_return"] = (1 + daily_df["return"]).cumprod() - 1

        # 计算最大回撤
        cumulative = (1 + daily_df["return"]).cumprod()
        rolling_max = cumulative.expanding().max()
        drawdown = (cumulative - rolling_max) / rolling_max
        max_drawdown = drawdown.min()

        # 计算年化收益率
        n_days = len(daily_df)
        if n_days > 0:
            annual_return = (1 + daily_df["cumulative_return"].iloc[-1]) ** (252 / n_days) - 1
        else:
            annual_return = 0.0

        # 计算年化波动率
        annual_volatility = daily_df["return"].std() * np.sqrt(252)

        # 计算夏普比率（假设无风险利率为 3%）
        risk_free_rate = 0.03
        sharpe_ratio = (annual_return - risk_free_rate) / annual_volatility if annual_volatility > 0 else 0.0

        # 计算卡玛比率
        calmar_ratio = annual_return / abs(max_drawdown) if max_drawdown != 0 else 0.0

        # 计算交易次数
        trades_df = pd.DataFrame(self.trades)
        successful_trades = trades_df[trades_df["direction"] != "failed"]
        n_trades = len(successful_trades)
        n_failed_trades = len(trades_df[trades_df["direction"] == "failed"])

        # 计算胜率
        buy_trades = successful_trades[successful_trades["direction"] == "buy"]
        sell_trades = successful_trades[successful_trades["direction"] == "sell"]

        # 计算盈亏
        total_profit = 0.0
        total_loss = 0.0
        win_count = 0
        loss_count = 0

        for i, sell_trade in sell_trades.iterrows():
            # 找到对应的买入交易
            corresponding_buy = buy_trades[
                (buy_trades["date"] < sell_trade["date"]) &
                (buy_trades["symbol"] == sell_trade["symbol"])
            ].iloc[-1]

            profit = (sell_trade["price"] - corresponding_buy["price"]) * corresponding_buy["shares"] - \
                     (sell_trade["cost"] + corresponding_buy["cost"])

            if profit > 0:
                total_profit += profit
                win_count += 1
            else:
                total_loss += abs(profit)
                loss_count += 1

        win_rate = win_count / (win_count + loss_count) if (win_count + loss_count) > 0 else 0.0

        # 计算盈亏比
        profit_loss_ratio = (total_profit / win_count) / (total_loss / loss_count) if loss_count > 0 else 0.0

        # 构建结果字典
        result = {
            "initial_capital": self.initial_capital,
            "final_nav": self.nav,
            "total_return": (self.nav - self.initial_capital) / self.initial_capital,
            "annual_return": annual_return,
            "annual_volatility": annual_volatility,
            "max_drawdown": max_drawdown,
            "sharpe_ratio": sharpe_ratio,
            "calmar_ratio": calmar_ratio,
            "win_rate": win_rate,
            "profit_loss_ratio": profit_loss_ratio,
            "n_trades": n_trades,
            "n_failed_trades": n_failed_trades,
            "trades": trades_df,
            "daily_records": daily_df,
        }

        return result

    # ============================================================
    # 多资产组合调仓核心方法
    # ============================================================
    #
    # 以下方法实现了从 TargetWeightSignal 到 Order 的端到端闭环：
    #   TargetWeightSignal → AUM 计算 → 目标股数计算 → A 股整手取整
    #   → 碎股过滤 → 现金约束检查 → Order 对象生成 → 状态更新
    #
    # 设计约束：
    # - A 股 ETF 最小交易单位为 100 股（1 手），不足 1 手的订单被丢弃
    # - 卖出订单先于买入订单执行（释放现金优先）
    # - 买入总额不超过可用现金（防御性约束）
    # ============================================================

    def calculate_aum(self) -> float:
        """
        计算账户总市值 (AUM = Assets Under Management)

        公式：
            AUM = 可用现金 + Σ(持仓股数 × 最新收盘价)

        防御性检查：
        - 未持仓的标的不计入市值
        - 最新收盘价缺失的标的使用 0.0（极端场景：停牌或数据缺失）
        - 防范 NaN / Inf：若价格为 NaN 或 Inf，视为 0.0

        返回：
            账户总市值
        """
        # 持仓市值 = Σ(持仓股数 × 最新收盘价)
        position_value = 0.0
        for symbol, shares in self.positions_dict.items():
            if shares <= 0:
                continue  # 无持仓或做空（本引擎为纯多头，不应出现负数）
            price = self.latest_prices.get(symbol, 0.0)
            # 防御：NaN / Inf 价格视为 0.0
            if not np.isfinite(price):
                price = 0.0
            position_value += shares * price

        aum = self.cash + position_value
        return aum

    def calculate_current_weights(self) -> Dict[str, float]:
        """
        计算当前各资产的实际权重

        公式：
            weight[symbol] = (持仓股数 × 最新收盘价) / AUM

        用途：
        - 作为 HMMStateMapper.map_single_day() 的 current_weights 参数
        - 迟滞滤波需要知道当前实际权重来判断是否需要调仓

        防御性检查：
        - AUM 为 0 时返回全零权重（极端场景：账户爆仓）
        - 价格缺失的持仓资产权重为 0

        返回：
            当前实际权重字典（symbol -> weight）
        """
        aum = self.calculate_aum()

        if aum <= 0:
            # 极端场景：账户总市值为 0 或负数（理论上不应发生）
            return {symbol: 0.0 for symbol in self.positions_dict}

        current_weights: Dict[str, float] = {}
        for symbol, shares in self.positions_dict.items():
            price = self.latest_prices.get(symbol, 0.0)
            if not np.isfinite(price):
                price = 0.0
            current_weights[symbol] = (shares * price) / aum

        return current_weights

    def _round_to_lot_size(self, shares: float) -> int:
        """
        A 股碎股处理：向下取整为 100 的整数倍（1 手）

        A 股 ETF 交易规则：
        - 最小交易单位为 100 股（1 手）
        - 不足 100 股的部分直接丢弃（不能零卖/零买）
        - 向下取整而非四舍五入（保守原则：不超买）

        为什么用向下取整而非四舍五入：
        - 向下取整确保不会超出目标权重对应的资金需求
        - 超买可能导致现金不足，引发部分成交失败

        参数：
            shares: 理论股数（可能含小数）

        返回：
            取整后的股数（100 的整数倍），不足 100 股返回 0
        """
        lots = math.floor(shares / 100)
        return lots * 100

    def process_target_weight_signal(
        self,
        signal: TargetWeightSignal,
        prices: Dict[str, float],
    ) -> List[Order]:
        """
        目标权重信号 → 订单列表（核心方法）

        这是从"信号"到"真实订单"的端到端转化入口。
        引擎的事件循环在每日处理时调用此方法。

        处理流程：
        ┌─────────────────────────────────────────────────────────────┐
        │ 1. 接收 TargetWeightSignal（理论权重 + 调仓方向）           │
        │ 2. 更新最新收盘价                                           │
        │ 3. 计算账户总市值 AUM                                       │
        │ 4. 对每个需调仓的标的：                                     │
        │    a. 计算目标市值 = AUM × 目标权重                         │
        │    b. 计算目标股数 = 目标市值 / 当前收盘价                   │
        │    c. A 股整手取整（向下取整为 100 的整数倍）               │
        │    d. 计算调仓股数 = 目标股数 - 当前持仓股数                 │
        │    e. 碎股过滤：调仓股数不足 100 股则丢弃                   │
        │ 5. 现金约束检查：买入总额不超过可用现金                      │
        │ 6. 生成 Order 对象列表                                      │
        │ 7. 更新持仓状态与现金                                       │
        └─────────────────────────────────────────────────────────────┘

        关键设计决策：
        - 仅对 signal.directions[symbol] != HOLD 的标的生成订单
        - 卖出订单先执行（释放现金供买入使用）
        - 买入订单受现金约束，不足时按比例缩减或丢弃

        参数：
            signal: 目标权重信号（来自 HMMStateMapper）
            prices: 当前收盘价字典（symbol -> price）
                必须覆盖信号中所有标的

        返回：
            本日生成的合法订单列表

        异常：
            ValueError: 价格字典未覆盖信号中的所有标的
            ValueError: 价格包含 NaN / Inf / 零值
        """
        # ============ 步骤 0：输入验证 ============
        # 验证价格覆盖性
        missing_symbols = set(signal.weights.keys()) - set(prices.keys())
        if missing_symbols:
            raise ValueError(
                f"价格字典未覆盖信号中的标的：{missing_symbols}，"
                f"请确保每个标的都有有效收盘价"
            )

        # 验证价格合法性（防范 NaN / Inf / 零值）
        for symbol, price in prices.items():
            if not np.isfinite(price) or price <= 0:
                raise ValueError(
                    f"标的 '{symbol}' 的价格不合法: {price}，"
                    f"无法据此计算目标股数"
                )

        # ============ 步骤 1：更新最新收盘价 ============
        self.latest_prices.update(prices)

        # ============ 步骤 2：计算账户总市值 ============
        aum = self.calculate_aum()

        if aum <= 0:
            # 极端场景：账户总市值为 0（不应发生）
            return []

        # ============ 步骤 3：计算各标的的目标股数与调仓量 ============
        # rebalance_plan: {symbol: (target_shares, delta_shares, direction)}
        rebalance_plan: Dict[str, tuple] = {}

        for symbol in signal.get_rebalance_symbols():
            target_weight = signal.weights[symbol]
            current_price = prices[symbol]
            current_shares = self.positions_dict.get(symbol, 0)

            # 计算目标市值
            target_value = aum * target_weight

            # 计算目标股数（理论值，可能含小数）
            # 防御：除以零已在步骤 0 排除
            theoretical_shares = target_value / current_price

            # A 股整手取整：向下取整为 100 的整数倍
            target_shares = self._round_to_lot_size(theoretical_shares)

            # 计算调仓股数（正数=买入，负数=卖出）
            delta_shares = target_shares - current_shares

            # 碎股过滤：调仓量不足 100 股则丢弃
            if abs(delta_shares) < 100:
                continue

            # 记录调仓计划
            rebalance_plan[symbol] = (target_shares, delta_shares, signal.directions[symbol])

        # ============ 步骤 4：生成订单（卖出优先） ============
        # 先执行卖出订单释放现金，再执行买入订单
        # 这是组合调仓的标准实践，确保现金充足
        sell_orders: List[Order] = []
        buy_orders: List[Order] = []

        for symbol, (target_shares, delta_shares, direction) in rebalance_plan.items():
            current_price = prices[symbol]

            if delta_shares < 0:
                # 卖出订单
                sell_shares = abs(delta_shares)
                order_id = self._generate_order_id(symbol, signal.timestamp)

                order = Order(
                    order_id=order_id,
                    symbol=symbol,
                    side=OrderSide.SELL,
                    shares=sell_shares,
                    price=current_price,
                    timestamp=signal.timestamp,
                    status="pending",
                )
                sell_orders.append(order)

            elif delta_shares > 0:
                # 买入订单
                order_id = self._generate_order_id(symbol, signal.timestamp)

                order = Order(
                    order_id=order_id,
                    symbol=symbol,
                    side=OrderSide.BUY,
                    shares=delta_shares,
                    price=current_price,
                    timestamp=signal.timestamp,
                    status="pending",
                )
                buy_orders.append(order)

        # ============ 步骤 5：执行卖出订单（释放现金） ============
        for order in sell_orders:
            self._execute_portfolio_order(order)

        # ============ 步骤 6：执行买入订单（现金约束） ============
        # 计算可用现金（扣除已执行的卖出释放的现金）
        for order in buy_orders:
            required_cash = order.shares * order.price
            # 计算交易成本（佣金 + 过户费等，不含印花税因为买入不收印花税）
            # 保守估计：预留 0.1% 的综合成本
            estimated_cost = required_cash * 0.001 + 5.0  # 最低佣金 5 元
            total_required = required_cash + estimated_cost

            if self.cash < total_required:
                # 现金不足：尝试缩减买入量
                affordable_shares = self._round_to_lot_size(
                    (self.cash - estimated_cost) / order.price
                )

                if affordable_shares < 100:
                    # 不足 1 手，丢弃该订单
                    order.status = "failed"
                    self.portfolio_orders.append(order)
                    self._record_failed_trade(
                        date=order.timestamp,
                        reason=f"资金不足：需要 {total_required:.2f}，"
                               f"可用 {self.cash:.2f}",
                        shares=order.shares,
                        price=order.price,
                    )
                    continue

                # 部分买入：缩减至可承受的手数
                order.shares = affordable_shares

            self._execute_portfolio_order(order)

        # ============ 步骤 7：记录订单 ============
        all_orders = sell_orders + buy_orders
        self.portfolio_orders.extend(all_orders)

        return all_orders

    def _execute_portfolio_order(self, order: Order) -> None:
        """
        执行组合调仓订单（更新持仓与现金）

        成本计算走注入的 self.cost_model（佣金/印花税/过户费），使请求传入的
        cost_model 参数真正生效（消除原硬编码）。默认行为与原硬编码完全一致。

        简化假设：
        - 以订单价格全部成交（无滑点，需逐标的逐日成交量数据，留模块③ BacktestBroker）
        - 佣金/印花税/过户费均由 self.cost_model 按注入参数显式计算

        参数：
            order: 待执行的订单对象

        注意：
            此方法会直接修改 self.cash 和 self.positions_dict
        """
        # 计算成交金额
        amount = order.shares * order.price

        # 成本走注入的 self.cost_model：使请求传入的 cost_model 参数真正生效，
        # 消除原硬编码（万三/千五/十万一）。默认值与原硬编码完全一致，故既有
        # 组合回测/测试无回归（CostModel 默认：佣金万三最低5元、印花千五卖出、
        # 过户十万一沪市）。
        # 注意：滑点（slippage）需逐标的逐日成交量数据，组合路径暂不接入，
        # 留模块③ BacktestBroker 的完整 CostModel（含滑点）。
        commission = self.cost_model.calculate_commission(amount)
        stamp_duty = self.cost_model.calculate_stamp_duty(
            amount, order.side == OrderSide.SELL
        )
        transfer_fee = self.cost_model.calculate_transfer_fee(amount, order.symbol)

        # 总交易成本
        total_cost = commission + stamp_duty + transfer_fee

        if order.side == OrderSide.BUY:
            # 买入：扣减现金，增加持仓
            self.cash -= (amount + total_cost)
            self.positions_dict[order.symbol] = (
                self.positions_dict.get(order.symbol, 0) + order.shares
            )
        else:
            # 卖出：增加现金，减少持仓
            self.cash += (amount - total_cost)
            self.positions_dict[order.symbol] = (
                self.positions_dict.get(order.symbol, 0) - order.shares
            )

        # 更新订单状态
        order.status = "filled"

        # 记录交易（复用原有的 _record_trade 方法）
        self._record_trade(
            date=order.timestamp,
            direction="sell" if order.side == OrderSide.SELL else "buy",
            shares=order.shares,
            price=order.price,
            cost=total_cost,
            symbol=order.symbol,
        )

    def _generate_order_id(self, symbol: str, timestamp: pd.Timestamp) -> str:
        """
        生成订单唯一标识

        格式：ORDER_{symbol}_{YYYYMMDDHHMMSSffffff}

        参数：
            symbol: 标的代码
            timestamp: 时间戳

        返回：
            订单 ID 字符串
        """
        # 将 symbol 中的点号替换为下划线（避免文件名/路径问题）
        safe_symbol = symbol.replace(".", "_")
        time_str = timestamp.strftime("%Y%m%d%H%M%S%f")
        return f"ORDER_{safe_symbol}_{time_str}"

    def run_portfolio(
        self,
        price_data: Dict[str, pd.DataFrame],
        signals: List[TargetWeightSignal],
    ) -> Dict[str, Any]:
        """
        多资产组合回测（事件驱动）

        逐日遍历信号列表，对每个调仓日：
        1. 接收 TargetWeightSignal
        2. 计算当前各资产权重
        3. 将信号与当前权重送入 mapper 进行迟滞滤波
        4. 将滤波后的信号转化为订单
        5. 执行订单，更新持仓与净值
        6. 在非调仓日，仅更新净值（按收盘价重估）

        参数：
            price_data: 各标的的价格数据字典
                格式：{symbol: DataFrame}，每个 DataFrame 需包含 OHLCV 列
            signals: 目标权重信号列表（按时间排序）

        返回：
            回测结果字典（含净值曲线、交易记录、绩效指标）

        异常：
            ValueError: 价格数据与信号标的不匹配
        """
        # 重置状态
        self._reset_state()

        # 收集所有交易日期（取所有标的的交易日并集）
        all_dates = set()
        for symbol, df in price_data.items():
            all_dates.update(df.index)
        all_dates = sorted(all_dates)

        # 构建信号索引（timestamp -> TargetWeightSignal）
        signal_index: Dict[pd.Timestamp, TargetWeightSignal] = {
            sig.timestamp: sig for sig in signals
        }

        # 逐日遍历（事件驱动核心循环）
        for date in all_dates:
            # ============ 更新最新收盘价 ============
            for symbol, df in price_data.items():
                if date in df.index:
                    close_price = df.loc[date, "close"]
                    if np.isfinite(close_price) and close_price > 0:
                        self.latest_prices[symbol] = close_price

            # ============ 检查是否有调仓信号 ============
            if date in signal_index:
                signal = signal_index[date]

                # 构建当前价格字典（使用开盘价执行交易）
                trade_prices: Dict[str, float] = {}
                for symbol in signal.weights.keys():
                    if symbol in price_data and date in price_data[symbol].index:
                        open_price = price_data[symbol].loc[date, "open"]
                        if np.isfinite(open_price) and open_price > 0:
                            trade_prices[symbol] = open_price
                        else:
                            # 开盘价异常，回退到收盘价
                            trade_prices[symbol] = self.latest_prices.get(symbol, 0.0)

                # 执行调仓（核心方法调用）
                if trade_prices:
                    self.process_target_weight_signal(signal, trade_prices)

            # ============ 更新每日净值 ============
            aum = self.calculate_aum()
            self.nav = aum

            # ============ 记录每日状态 ============
            position_values = {}
            for symbol, shares in self.positions_dict.items():
                price = self.latest_prices.get(symbol, 0.0)
                if not np.isfinite(price):
                    price = 0.0
                position_values[symbol] = shares * price

            daily_record = {
                "date": date,
                "nav": self.nav,
                "cash": self.cash,
                "position_value": sum(position_values.values()),
                "positions": dict(self.positions_dict),
                "position_values": position_values,
            }
            self.daily_records.append(daily_record)

        # ============ 计算最终结果 ============
        result = self._calculate_portfolio_result()

        return result

    def _calculate_portfolio_result(self) -> Dict[str, Any]:
        """
        计算组合回测最终结果

        返回：
            回测结果字典（含净值曲线、绩效指标、交易记录）
        """
        daily_df = pd.DataFrame(self.daily_records)
        if len(daily_df) == 0:
            # 早返回兜底：与正常路径结果字典契约保持一致，缺 win_rate/profit_loss_ratio
            # 会令上层调用方（如 service 的 metrics 解包）误读为 0，故显式补齐为 0.0
            return {
                "initial_capital": self.initial_capital,
                "final_nav": self.nav,
                "total_return": 0.0,
                "annual_return": 0.0,
                "annual_volatility": 0.0,
                "max_drawdown": 0.0,
                "sharpe_ratio": 0.0,
                "win_rate": 0.0,
                "profit_loss_ratio": 0.0,
                "n_trades": 0,
                "daily_records": daily_df,
                "orders": self.portfolio_orders,
            }

        daily_df.set_index("date", inplace=True)

        # 计算日收益率
        daily_df["return"] = daily_df["nav"].pct_change()
        # 首日收益率设为 0
        daily_df["return"].iloc[0] = 0.0

        # 计算累计收益率
        daily_df["cumulative_return"] = (1 + daily_df["return"]).cumprod() - 1

        # 计算最大回撤
        cumulative = (1 + daily_df["return"]).cumprod()
        rolling_max = cumulative.expanding().max()
        drawdown = (cumulative - rolling_max) / rolling_max
        max_drawdown = drawdown.min()

        # 计算年化收益率
        n_days = len(daily_df)
        if n_days > 1 and daily_df["cumulative_return"].iloc[-1] != 0:
            annual_return = (
                (1 + daily_df["cumulative_return"].iloc[-1]) ** (252 / n_days) - 1
            )
        else:
            annual_return = 0.0

        # 计算年化波动率
        annual_volatility = daily_df["return"].std() * np.sqrt(252)

        # 计算夏普比率（假设无风险利率为 3%）
        risk_free_rate = 0.03
        sharpe_ratio = (
            (annual_return - risk_free_rate) / annual_volatility
            if annual_volatility > 0
            else 0.0
        )

        # 计算卡玛比率
        calmar_ratio = (
            annual_return / abs(max_drawdown) if max_drawdown != 0 else 0.0
        )

        # 统计交易次数
        trades_df = pd.DataFrame(self.trades)
        n_successful = len(trades_df[trades_df["direction"] != "failed"]) if len(trades_df) > 0 else 0
        n_failed = len(trades_df[trades_df["direction"] == "failed"]) if len(trades_df) > 0 else 0

        # 计算交易类指标（胜率 win_rate / 盈亏比 profit_loss_ratio）
        # Why：组合回测（含单资产走 run_portfolio 的路径）此前结果字典缺失这两个键，
        # 导致上层 service/schema 输出恒为 0，严重误导用户对策略盈亏结构的判断。
        # 反黑盒：直接复用 MetricsCalculator.calculate_trade_metrics，它在内部已显式
        # 过滤 direction=='failed' 的失败行（metrics.py:104-105），并对无匹配买盘的
        # 卖单做 continue 跳过（比 _calculate_result 内联逻辑更鲁棒，不会抛 IndexError）。
        # 空 trades / 异常一律兜底 0.0，保证结果字典契约稳定。
        win_rate = 0.0
        profit_loss_ratio = 0.0
        if len(trades_df) > 0:
            try:
                trade_metrics = MetricsCalculator.calculate_trade_metrics(trades_df)
                win_rate = float(trade_metrics.get("win_rate", 0.0) or 0.0)
                profit_loss_ratio = float(trade_metrics.get("profit_loss_ratio", 0.0) or 0.0)
            except Exception:
                # 极端兜底：trades_df 结构异常或 NaN/Inf 时不应让整个回测崩掉
                win_rate = 0.0
                profit_loss_ratio = 0.0

        result = {
            "initial_capital": self.initial_capital,
            "final_nav": self.nav,
            "total_return": (self.nav - self.initial_capital) / self.initial_capital,
            "annual_return": annual_return,
            "annual_volatility": annual_volatility,
            "max_drawdown": max_drawdown,
            "sharpe_ratio": sharpe_ratio,
            "calmar_ratio": calmar_ratio,
            "win_rate": win_rate,
            "profit_loss_ratio": profit_loss_ratio,
            "n_trades": n_successful,
            "n_failed_trades": n_failed,
            "trades": trades_df,
            "daily_records": daily_df,
            "orders": self.portfolio_orders,
        }

        return result