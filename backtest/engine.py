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
import logging
import math
from datetime import datetime
from dataclasses import dataclass, field
from enum import Enum, auto

import numpy as np
import pandas as pd
from typing import Dict, Any, Optional, List, Callable

from .cost_model import CostModel
from .metrics import MetricsCalculator
from factors.fusion import TargetWeightSignal, SignalDirection

# 模块级 logger：run_portfolio 完成时记录 n_trades/final_nav，便于本地日志定位回测产出。
# 记录经 root logger → RingBufferLogHandler 流前端 + FileHandler 落盘（见 main.py）。
logger = logging.getLogger(__name__)


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


# ============ T+1 底仓冻结感知（分钟级回测专用纯函数） ============
#
# 设计意图（Why）：
# A 股实行 T+1 交收制度——当日新买入的证券当日不可卖出，必须冻结至次一交易日方可卖出。
# 这意味着在分钟级回测中，"持仓"并非铁板一块，而必须显式拆分为两层：
#   1. 底仓（sellable）：昨日及更早建仓的份额，可在日内任意分钟卖出；
#   2. 冻结仓（frozen）：当日新买入的份额，必须冻结至次日才能转化为底仓。
#
# 语义辨析（关键约定，与 brief 测试一致）：
#   _split_t1(current_held, today_bought, prev_held) -> (sellable, frozen)
#   其中：sellable = prev_held（昨日收盘持仓 = 底仓）
#         frozen  = today_bought（今日新买 = 冻结）
#   current_held 参数在此语义下为冗余参数——保留签名仅为调用方便与未来扩展
#   （例如未来若要支持日内已卖部分，则 current_held 可用于校验一致性），
#   但真实物理判定只依赖 prev_held 与 today_bought 两个语义清晰的输入。
#
#   这与 brief 中给出的"min(prev_held, current_held)"写法不同——
#   brief 的写法在 current_held=0 且 prev_held=200 的测试用例下会得到 sellable=0，
#   与测试断言 (200, 100) 矛盾。本实现采用 prev_held/today_bought 直接映射语义，
#   既满足 brief 测试断言，又更贴近 A 股 T+1 的真实物理规则。


def _split_t1(current_held: int, today_bought: int, prev_held: int) -> tuple[int, int]:
    """A 股 T+1 底仓冻结感知：拆分当前持仓为 (底仓可卖, 今日新仓冻结)。

    参数：
        current_held: 当前总持仓（冗余参数，保留签名兼容；真实判定不依赖）。
        today_bought: 今日新买入量（将冻结至次日）。
        prev_held:    昨日收盘持仓（构成今日的底仓，可日内卖）。

    返回：
        (sellable, frozen) 元组：
            sellable = prev_held（底仓可卖）
            frozen   = today_bought（今日新仓冻结）

    边界说明：
        防御性约定 prev_held / today_bought 均为非负整数。若传入负值（非法状态），
        本函数不主动 raise——交由调用方（_close/_buy）的整手校验兜底，保持纯函数无副作用。
    """
    # 底仓 = 昨日持仓（直接映射语义，不与 current_held 耦合）
    sellable = prev_held
    # 冻结 = 今日新买（次日开盘后由引擎日切逻辑转为基础底仓）
    frozen = today_bought
    return sellable, frozen


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
        symbol: str = "600000.SH",
        event_emitter: Callable[[dict], None] | None = None,
    ) -> Dict[str, Any]:
        """
        执行回测

        参数：
            df: OHLCV 数据
            signal: 信号序列（值在 [0, 1] 范围内）
            symbol: 交易标的代码
            event_emitter: SSE 事件回调（可选，默认 None）。
                Why 默认 None：保证所有既有调用方（service / API / 旧测试）零改动、
                零行为变化、零性能开销（事件发射完全被 None 短路）。
                非 None 时，逐日循环会发射 dict 事件：
                  - {"type":"progress","date":str,"i":int,"n":int,"nav":float}
                  - {"type":"trade","date":str,"direction":"buy"|"sell",
                     "shares":int,"price":float,"symbol":str}
                  - {"type":"risk","level":"WARN","date":str,"reason":str,
                     "shares":int,"price":float,"symbol":str}
                Why 不深入 _execute_trade 内部传参：避免改造成交状态机签名、
                破坏既有路径。改在循环层用 self.trades 长度变化 + 最后一条记录
                的 direction 字段判定事件类型，最小侵入、零回归。

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

        # 事件检测基线：循环开始前 trades 为空（_reset_state 已清零），
        # 后续用 _prev_n_trades 比对当日是否有新成交记录追加。
        _prev_n_trades = len(self.trades)

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

            # ============ SSE 事件发射（默认 None 时完全短路，零开销） ============
            # Why 放在循环末尾：此时本日 nav/cash/position/trades 已全部更新到
            # 最终态，progress 事件的 nav 字段才是真实收盘后净值，前端进度条/
            # 净值曲线渲染才不会与最终回测结果产生不一致。
            if event_emitter is not None:
                # 1) 成交 / 风控事件：通过比对 trades 长度变化捕获（_execute_trade
                #    内可能 append 0~多条：正常成交 1 条 / 失败成交 1 条）。
                #    Why 用 dict 取值而非 getattr：self.trades 存的是 dict（见
                #    _record_trade / _record_failed_trade），getattr 会回退默认值
                #    掩盖真实字段缺失，dict.get 显式可控。
                new_trades = self.trades[_prev_n_trades:]
                for t in new_trades:
                    if t.get("direction") == "failed":
                        # 失败成交：涨跌停 / 资金不足（_execute_trade 内三类分支
                        # 均走 _record_failed_trade 落库，这里干净捕获，不改成交逻辑）
                        event_emitter({
                            "type": "risk",
                            "level": "WARN",
                            "date": str(date),
                            "reason": t.get("reason", "unknown"),
                            "shares": t.get("shares", 0),
                            "price": t.get("price", price),
                            "symbol": symbol,
                        })
                    else:
                        # 正常成交（buy / sell）
                        event_emitter({
                            "type": "trade",
                            "date": str(date),
                            "direction": t.get("direction", "buy"),
                            "shares": t.get("shares", 0),
                            "price": t.get("price", price),
                            "symbol": symbol,
                        })
                _prev_n_trades = len(self.trades)

                # 2) 进度事件：每日一发，推送真实收盘后净值
                # Why nav 走 math.isfinite 兜底（与 result 帧 _safe_float/np.isfinite 对称）：
                # 极端除零/持仓价异常场景 self.nav 可能 NaN/Inf，SSE 序列化为 JSON 时
                # NaN/Inf 是非法 JSON 值（JSON.stringify(NaN) → null / 浏览器解析错乱），
                # 前端 progress 行会显示 null 或整流中断。兜底为 0.0 保证流稳定（不掩盖
                # 业务异常——result 帧的最终 nav 仍由上层 _safe_float 显式处理并报警）。
                _nav = self.nav if math.isfinite(self.nav) else 0.0
                event_emitter({
                    "type": "progress",
                    "date": str(date),
                    "i": i,
                    "n": len(aligned_df),
                    "nav": _nav,
                })

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

        # 计算交易次数（空 trades 守卫：无 trades 时 trades_df 无 direction 列，直接索引 KeyError）
        trades_df = pd.DataFrame(self.trades)
        if len(trades_df) > 0:
            successful_trades = trades_df[trades_df["direction"] != "failed"]
            n_trades = len(successful_trades)
            n_failed_trades = len(trades_df[trades_df["direction"] == "failed"])

            # 计算胜率
            buy_trades = successful_trades[successful_trades["direction"] == "buy"]
            sell_trades = successful_trades[successful_trades["direction"] == "sell"]
        else:
            successful_trades = trades_df
            n_trades = 0
            n_failed_trades = 0
            buy_trades = trades_df
            sell_trades = trades_df

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
        event_emitter: Callable[[dict], None] | None = None,
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
            event_emitter: SSE 事件回调（可选，默认 None）。
                Why 默认 None：保证所有既有调用方（service / API / 旧测试）零改动、
                零行为变化、零性能开销（事件发射完全被 None 短路）。
                Why 这里也要布点（与 run() 同语义）：service.run_single_backtest 走的
                是 run_portfolio 路径，若只有 run() 布点而 portfolio 不布点，则 SSE
                实时流中途无 progress/trade 帧，前端终端回测期间会空白（Epic 4 核心
                价值未达成）。布点完全复用 run() 的事件契约：
                  - {"type":"progress","date":str,"i":int,"n":int,"nav":float}
                  - {"type":"trade","date":str,"direction":"buy"|"sell",
                     "shares":int,"price":float,"symbol":str}
                  - {"type":"risk","level":"WARN","date":str,"reason":str,
                     "shares":int,"price":float,"symbol":str}
                Why 用单一 if 守卫 + trades 切片法：最小侵入，不改既有调仓/净值/成本
                逻辑（process_target_weight_signal / calculate_aum 等签名零变化）。
                风控帧来源：process_target_weight_signal 现金不足丢弃买入订单时已调用
                _record_failed_trade(direction="failed", reason="资金不足...")，
                这里复用 run() 的同款 failed 切片分流即可发射 risk 帧，零新增分支。

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

        # 事件检测基线：循环开始前 trades 已被 _reset_state 清零，
        # 后续用 _prev_n_trades 比对当日是否有新成交记录追加（与 run() 同范式）。
        _prev_n_trades = len(self.trades)

        # 逐日遍历（事件驱动核心循环）
        for i, date in enumerate(all_dates):
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

            # ============ SSE 事件发射（默认 None 时完全短路，零开销） ============
            # Why 放在循环末尾：此时本日 nav/cash/position/trades 已全部更新到
            # 最终态，progress 的 nav 字段才是真实收盘后净值（与 run() 同语义）。
            # Why 用 self.trades 切片：portfolio 调仓走 _execute_portfolio_order →
            # _record_trade，每条成交追加 1 条 dict 到 self.trades；这里捕获切片
            # 即得到当日所有成交，无需改 process_target_weight_signal 签名。
            # 风控帧来源（I-2）：process_target_weight_signal 现金不足丢弃买入订单时
            # 已 _record_failed_trade(direction="failed", reason="资金不足...")，
            # 此处复用 run() 的同款 failed 切片分流即可发射 risk 帧，与单资产路径
            # run() 完全对称（字段名同为 reason，前端 toLogEntry 一致消费）。
            if event_emitter is not None:
                # 1) 成交 / 风控事件：当日新增 trades 切片分流（与 run() 同范式）
                new_trades = self.trades[_prev_n_trades:]
                for t in new_trades:
                    if t.get("direction") == "failed":
                        # 失败成交（组合路径目前仅"资金不足"分支；涨跌停未在组合
                        # 路径建模——_execute_portfolio_order 不含 high/low 检查，
                        # 与单资产 _execute_trade 不同。复用既有 _record_failed_trade，
                        # 不新增任何调仓/成交逻辑，零回归）
                        event_emitter({
                            "type": "risk",
                            "level": "WARN",
                            "date": str(date),
                            "reason": t.get("reason", "unknown"),
                            "shares": t.get("shares", 0),
                            "price": t.get("price", 0.0),
                            "symbol": t.get("symbol", "N/A"),
                        })
                    else:
                        # 正常成交（buy / sell）
                        event_emitter({
                            "type": "trade",
                            "date": str(date),
                            "direction": t.get("direction", "buy"),
                            "shares": t.get("shares", 0),
                            "price": t.get("price", 0.0),
                            "symbol": t.get("symbol", ""),
                        })
                _prev_n_trades = len(self.trades)

                # 2) 进度事件：每日一发，推送真实收盘后净值
                # Why nav 走 math.isfinite 兜底：与 run() 的 progress 帧 + result 帧的
                # _safe_float/np.isfinite 完全对称，防止极端除零场景下 self.nav 为
                # NaN/Inf 经 SSE 透传成非法 JSON（前端解析失败/流中断）。兜底 0.0，
                # 不掩盖业务异常——最终 nav 仍由上层 _safe_float 序列化。
                _nav = self.nav if math.isfinite(self.nav) else 0.0
                event_emitter({
                    "type": "progress",
                    "date": str(date),
                    "i": i,
                    "n": len(all_dates),
                    "nav": _nav,
                })

        # ============ 计算最终结果 ============
        result = self._calculate_portfolio_result()

        # 回测完成留痕：n_trades/final_nav 是定位「回测是否有产出」的关键指标，
        # 写本地日志便于事后核对（无需复现）。极端值（nav=0/n_trades 异常）在此暴露。
        logger.info(
            "run_portfolio 完成：n_trades=%d final_nav=%.2f daily_records=%d",
            result.get("n_trades", 0),
            result.get("final_nav", 0.0),
            len(result.get("daily_records", [])),
        )
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
        # 显式 .loc 原位赋值（修复 chained assignment 在 Copy-on-Write 下不生效）：
        # 原写法 daily_df["return"].iloc[0] = 0.0 作用于 pct_change 返回 Series 的副本，
        # pandas CoW 下原 daily_df 不变 → 首行 return 残留 NaN → 流入 SSE result 帧
        # （json.dumps 输出字面 NaN）→ 浏览器 JSON.parse 失败 → 前端 K 线不显示。
        # .loc[行标签, 列] 是单步原位赋值，CoW 安全。
        daily_df.loc[daily_df.index[0], "return"] = 0.0

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

    # ============================================================
    # 分钟级回测核心方法（Task 15）
    # ============================================================
    #
    # 与 run()（日级）/ run_portfolio()（组合日级）并列的第三条回测路径：
    #   run_minute —— 单资产、分钟级、T+1 底仓冻结、止损/止盈/移动止损、event_emitter
    #
    # 设计意图（Why 分钟级）：
    # - 日级回测用日开盘价撮合，无法刻画盘中穿越止损线即触发的物理时序——
    #   实盘止损单是 stop order（触碰即触发），不是收盘价触发。分钟级回放才能
    #   真实反映"价格盘中击穿止损线→即刻平仓"的时序，避免回测高估止盈/低估止损。
    # - 宏观 CTA 策略（Epic 3）依赖 ATR 移动止损，ATR 是分钟级波幅统计量，
    #   必须在分钟级回测中才能正确更新与触发。
    #
    # 与既有 run/run_portfolio 零耦合：
    # - 复用 _reset_state / _calculate_result / _record_trade / self.trades / self.nav /
    #   self.cash / self.position（机制完全一致），不重写既有 _execute_trade 等内部方法。
    # - 分钟级撮合走专用的 _entry / _close / _buy / _update_minute_nav 辅助方法，
    #   保持简单记账（不走 cost_model 的复杂滑点，分钟级滑点需逐 tick 数据，留后续模块）。
    # - 默认 event_emitter=None → 零行为变化、零开销（与 run/run_portfolio 对称）。
    # ============================================================

    def run_minute(
        self,
        df: pd.DataFrame,
        signal: pd.Series,
        symbol: str = "000001.SZ",
        atr_window: int = 14,
        sl_pct: float = 0.05,
        tp_pct: float = 0.05,
        trail_k: float = 2.0,
        event_emitter: Callable[[dict], None] | None = None,
    ) -> Dict[str, Any]:
        """分钟级回测 + T+1 底仓冻结 + 止损止盈移动止损 + event_emitter。

        参数：
            df: 分钟级 OHLCV 数据（索引为 pd.DatetimeIndex，含 open/high/low/close/volume）。
            signal: 信号序列（值在 [0, 1] 范围内，与 df 索引对齐）。
            symbol: 交易标的代码。
            atr_window: ATR 计算窗口（默认 14，分钟级波幅统计量）。
            sl_pct: 固定止损百分比（默认 0.05 = 跌 5% 止损）。
            tp_pct: 固定止盈百分比（默认 0.05 = 涨 5% 止盈）。
            trail_k: ATR 移动止损乘数（默认 2.0，止损线离高价的"呼吸距离"）。
            event_emitter: SSE 事件回调（可选，默认 None）。
                Why 默认 None：与 run/run_portfolio 完全对称，保证所有既有调用方零改动、
                零行为变化、零性能开销（事件发射完全被 None 短路）。
                非 None 时，逐分钟循环发射：
                  - {"type":"progress","date":str,"i":int,"n":int,"nav":float}
                  - {"type":"trade","date":str,"direction":"buy"|"sell",
                     "shares":int,"price":float,"symbol":str}
                  - {"type":"risk","level":"WARN","date":str,"reason":str,
                     "shares":int,"price":float,"symbol":str}

        返回：
            回测结果字典（复用 _calculate_result 的完整契约）。

        物理时序约定：
        - 每根 K 线用开盘价撮合建仓/平仓（与 run() 一致），用收盘价更新净值。
        - T+1：日切时把"昨日持仓"转为基础底仓，当日新买的份额冻结至次日。
        - 止损/止盈判定用最新价（这里用 open 作为简化判定价），
          与 trading.order_state.check_stop_loss/check_take_profit 的"触碰即触发"语义一致。
        """
        from trading.order_state import (
            check_stop_loss,
            check_take_profit,
            update_trailing_stop,
        )

        # 重置状态（复用既有机制，与 run/run_portfolio 同款）
        self._reset_state()

        # 对齐数据与信号（与 run() 同款：以 signal 索引为准）
        aligned = df.loc[signal.index]

        # ATR 计算：分钟级高低差均值（真实波幅的简化版，未含跳空）
        # Why fillna(1e-9)：前 atr_window 根无足够样本，rolling 返回 NaN，
        # 用极小正值兜底避免后续 update_trailing_stop 除零/异常。1e-9 而非 0
        # 是为了让 new_stop = high - atr*k 在 atr≈0 时不退化为 high（会立即触发止损）。
        atr_s = (
            (aligned["high"] - aligned["low"])
            .rolling(atr_window)
            .mean()
            .fillna(1e-9)
        )

        # T+1 状态追踪
        prev_held = 0          # 昨日收盘持仓（构成今日底仓）
        today_bought = 0       # 今日累计新买入量（冻结至次日）
        # 移动止损线（只上移不下移，跨日持续）
        trailing_stop = 0.0
        # 入场均价（用于固定止损/止盈判定）
        entry_price: float = 0.0
        # 持仓期间的最高价（用于移动止损更新）
        running_high: float = 0.0

        _prev_n_trades = len(self.trades)  # 事件检测基线（与 run() 同范式）

        # 逐分钟遍历（事件驱动核心循环）
        for i, (ts, row) in enumerate(aligned.iterrows()):
            sig = signal.loc[ts]
            today = ts.date()

            # ============ 日切：T+1 底仓转换 ============
            # 检测日期切换：当上一根 K 线的日期与当前不同时，视为新交易日开始。
            # 此时把"昨日累计持仓"整体转为基础底仓，今日新买计数归零。
            # Why prev_held = position：日切瞬间 self.position 就是昨日收盘的总持仓，
            # 无论其中多少是昨日新买的——T+1 制度下，跨过夜后就解冻为底仓。
            if i > 0 and aligned.index[i - 1].date() != today:
                prev_held = self.position
                today_bought = 0
                # 跨日后，前一日累计的 running_high/entry_price 保留（趋势跟踪止损
                # 不因日切重置——这是趋势跟踪策略的核心机制）

            # T+1 底仓拆分：sellable=prev_held（底仓），frozen=today_bought（今日新仓）
            # 注意：sellable 只代表"物理上可卖的底仓上限"，实际卖出还受 self.position 约束
            sellable_cap, _frozen = _split_t1(self.position, today_bought, prev_held)

            price = row["open"]  # 用开盘价撮合（与 run() 一致）

            # ============ 止损 / 止盈 / 移动止损（仅对底仓可卖部分生效） ============
            # T+1 物理约束：今日新买的份额冻结，不能卖——所以风控平仓只能动用底仓。
            # 防御：self.position 可能在日内因卖出已减少，实际可卖 = min(sellable_cap, position)
            actual_sellable = min(sellable_cap, self.position)

            if actual_sellable > 0 and entry_price > 0:
                # 1) 固定止损：最新价跌穿 entry*(1-sl_pct) 即触发
                if check_stop_loss(entry_price, price, sl_pct):
                    self._close(
                        actual_sellable, price, ts, symbol,
                        reason="触及止损", event_emitter=event_emitter,
                    )
                # 2) 固定止盈：最新价涨破 entry*(1+tp_pct) 即触发
                elif check_take_profit(entry_price, price, tp_pct):
                    self._close(
                        actual_sellable, price, ts, symbol,
                        reason="触及止盈", event_emitter=event_emitter,
                    )
                # 3) 移动止损：价格回落跌破既有止损线即触发（用【旧】止损线判定本根，
                #    新止损线在下方更新块按本根 high 抬升供下一根——避免本根自触自发）。
                elif trailing_stop > 0 and price <= trailing_stop:
                    self._close(
                        actual_sellable, price, ts, symbol,
                        reason="移动止损", event_emitter=event_emitter,
                    )

            # ============ 全平后重置基准（I-1 修复：杜绝新仓沿用旧 trailing_stop/entry） ============
            # Why 必须在「止损/止盈/移动止损 _close 块之后」+「移动止损更新块之前」：
            #   - _close 全平后 self.position 归 0，但 entry_price/running_high/trailing_stop
            #     仍是上一轮建仓的旧值。若同日稍后信号再次 _buy，新仓会用旧偏高 entry 混入
            #     加权成本 → 止损止盈阈值偏移；更致命的是 trailing_stop 不重置 → 新仓建立后
            #     下一根 update 块会把旧 trailing_stop（可能高于新仓建仓价）作为 prev_stop，
            #     导致新仓一建仓就被「移动止损」误触发平仓（趋势跟踪策略的隐藏杀手）。
            # Why 仅 position==0 才重置（部分平仓/底仓减半不重置）：
            #   - 部分平仓时 entry_price 仍是该批底仓的真实建仓成本，running_high/
            #     trailing_stop 也是该持仓期间的真实高点与止损线——重置会丢失趋势跟踪
            #     语义（移动止损应只上移不下移，跨持仓期间持续累积）。
            #   - 仅当底仓【全部】平掉（position 归 0）才视为「这一轮持仓周期结束」，
            #     下次建仓开启新周期，基准从 0 重新起算。
            if self.position == 0:
                entry_price = 0.0
                running_high = 0.0
                trailing_stop = 0.0

            # ============ 移动止损更新（持仓期间持续抬升止损线） ============
            # Why 在止损/止盈判定之后更新：先用旧止损线判定本根是否触发，
            # 再用本根 high 更新止损线供下一根使用——避免本根自己触发自己。
            if self.position > 0:
                if row["high"] > running_high:
                    running_high = row["high"]
                # ATR 取当前根的值（前 atr_window 根为 1e-9 兜底）
                current_atr = atr_s.loc[ts]
                if current_atr > 1e-9:
                    trailing_stop = update_trailing_stop(
                        running_high, current_atr, trail_k, trailing_stop
                    )

            # ============ 信号建仓（分钟级简化：sig>0 买入至目标仓位） ============
            # 目标持仓 = sig × 净值 / 价格，向下取整为 100 股整手（A 股规则）
            # Why 复用 _calculate_target_position 的同款整手逻辑：保持与 run() 一致的
            # 仓位规模语义，避免两套取整规则造成回测结果不可比。
            if sig > 0 and price > 0:
                target = int(sig * self.nav / price / 100) * 100
                delta = target - self.position
                if delta >= 100:  # 仅在新增 ≥1 手时买入（碎股过滤）
                    self._buy(
                        delta, price, ts, symbol,
                        event_emitter=event_emitter,
                    )
                    # 更新入场均价（加权平均，兼容分批建仓）与今日新买计数
                    new_shares = delta
                    old_value = entry_price * (self.position - new_shares)
                    new_value = price * new_shares
                    entry_price = (old_value + new_value) / self.position if self.position > 0 else price
                    today_bought += new_shares
                    # running_high 重置为本次建仓后的 high（移动止损重新起算）
                    running_high = max(running_high, row["high"])

            # ============ 更新分钟级净值（用收盘价重估持仓市值） ============
            self._update_minute_nav(ts, row)

            # ============ 记录每日状态（复用 _record_daily_state 同款结构） ============
            # Why 用分钟时间戳记录：结果字典的 daily_records 将是分钟级净值曲线，
            # 前端可视化（Epic 4/5）按时间序列渲染。字段与 run() 保持兼容。
            daily_record = {
                "date": ts,
                "nav": self.nav,
                "cash": self.cash,
                "position": self.position,
                "position_value": self.position * row["close"],
                "price": row["close"],
                "signal": sig,
            }
            self.daily_records.append(daily_record)

            # ============ SSE 事件发射（默认 None 时完全短路，零开销） ============
            # 与 run/run_portfolio 同范式：progress 每分钟一发，trade/risk 按成交切片分流。
            if event_emitter is not None:
                new_trades = self.trades[_prev_n_trades:]
                for t in new_trades:
                    if t.get("direction") == "failed":
                        event_emitter({
                            "type": "risk",
                            "level": "WARN",
                            "date": str(ts),
                            "reason": t.get("reason", "unknown"),
                            "shares": t.get("shares", 0),
                            "price": t.get("price", price),
                            "symbol": symbol,
                        })
                    else:
                        event_emitter({
                            "type": "trade",
                            "date": str(ts),
                            "direction": t.get("direction", "buy"),
                            "shares": t.get("shares", 0),
                            "price": t.get("price", price),
                            "symbol": symbol,
                        })
                _prev_n_trades = len(self.trades)

                # progress 帧：nav 走 math.isfinite 兜底（与 run/run_portfolio 对称，
                # 防 NaN/Inf 经 SSE 透传成非法 JSON）
                _nav = self.nav if math.isfinite(self.nav) else 0.0
                event_emitter({
                    "type": "progress",
                    "date": str(ts),
                    "i": i,
                    "n": len(aligned),
                    "nav": _nav,
                })

        # 计算最终结果（复用 _calculate_result，与 run() 同款契约）
        return self._calculate_result()

    # ----- 分钟级辅助方法 -----
    #
    # 设计原则（Why 独立于 _execute_trade）：
    # - 分钟级撮合需要精确控制"卖多少 / 买多少"，且需要附带 reason 字段
    #   （止损/止盈/移动止损）以便 emitter 发射语义化 risk 帧。
    # - 既有 _execute_trade 耦合了 target_position 整手判定、涨跌停、资金不足缩减等
    #   日级专属逻辑，强行复用会引入不必要的副作用与回归风险。
    # - 这里采用最简显式记账：直接修改 cash/position/trades，每步留中文 why 注解。

    def _buy(
        self,
        shares: int,
        price: float,
        ts: pd.Timestamp,
        symbol: str,
        event_emitter: Callable[[dict], None] | None = None,
    ) -> None:
        """分钟级买入：扣减现金、增加持仓、记录交易。

        参数：
            shares: 买入股数（调用方保证 ≥100 且为 100 整数倍）。
            price:  成交价（开盘价）。
            ts:     时间戳（分钟级）。
            symbol: 标的代码。
            event_emitter: 未使用（保留签名对称，便于未来扩展）。

        边界：
            资金不足时不成交但记录失败交易（与 _execute_trade 的资金不足分支对称），
            防止回测因单根 K 线资金不够而崩掉。
        """
        amount = shares * price
        # 简化成本：佣金万三最低 5 元（与 CostModel 默认一致），不含滑点（分钟级滑点留后续）
        commission = max(amount * 0.0003, 5.0)
        total_cost = amount + commission

        if self.cash < total_cost:
            # 资金不足：缩减至可承受的整手数，仍不足 100 则记失败
            affordable = int((self.cash - commission) / price / 100) * 100
            if affordable < 100:
                self._record_failed_trade(
                    date=ts, reason="资金不足", shares=shares, price=price
                )
                return
            shares = affordable
            amount = shares * price
            commission = max(amount * 0.0003, 5.0)
            total_cost = amount + commission

        self.cash -= total_cost
        self.position += shares
        self._record_trade(
            date=ts, direction="buy", shares=shares,
            price=price, cost=commission, symbol=symbol,
        )

    def _close(
        self,
        shares: int,
        price: float,
        ts: pd.Timestamp,
        symbol: str,
        reason: str = "平仓",
        event_emitter: Callable[[dict], None] | None = None,
    ) -> None:
        """分钟级平仓：增加现金、减少持仓、记录交易（含 reason）。

        参数：
            shares: 平仓股数（受 actual_sellable 约束，不超过底仓可卖上限）。
            price:  成交价。
            ts:     时间戳。
            symbol: 标的代码。
            reason: 平仓原因（"触及止损"/"触及止盈"/"移动止损"等，写入 reason 字段）。
            event_emitter: 未使用（保留签名对称）。

        Why reason 字段：
            分钟级风控平仓是被动触发，必须显式记录原因以便事后归因与 emitter 发射
            语义化 risk 帧（"触及止损/止盈"等中文 reason）。
            _record_trade 不含 reason 字段，这里先调 _record_trade 落正常成交，
            再回填 reason 到最后一条记录（最小侵入，不改 _record_trade 签名）。
        """
        if shares <= 0 or self.position <= 0:
            return  # 防御：无持仓或非法股数直接跳过

        # 实际平仓量不超过当前持仓（防御 T+1 与并发触发叠加）
        actual = min(shares, self.position)
        amount = actual * price
        # 简化成本：佣金万三最低 5 元 + 印花税千一（卖出）
        commission = max(amount * 0.0003, 5.0)
        stamp_duty = amount * 0.001
        total_cost = commission + stamp_duty

        self.cash += amount - total_cost
        self.position -= actual
        self._record_trade(
            date=ts, direction="sell", shares=actual,
            price=price, cost=total_cost, symbol=symbol,
        )
        # 回填 reason 到刚追加的交易记录（用于事后归因，emitter 的 risk 帧也读此字段）
        if self.trades:
            self.trades[-1]["reason"] = reason

        # ============ 风控平仓主动发射 risk 帧 ============
        # Why 在 _close 内发射而非在循环切片里判定：循环切片里 sell 是正常 trade 帧，
        # 无法区分"信号驱动的常规卖出"与"风控驱动的止损/止盈平仓"。这里 reason 已明确
        # 是风控触发（"触及止损"/"触及止盈"/"移动止损"），主动发射 risk 帧最干净。
        # 与 brief 契约一致："触发止盈/止损时 yield risk 事件"。
        if event_emitter is not None:
            event_emitter({
                "type": "risk",
                "level": "WARN",
                "date": str(ts),
                "reason": reason,
                "shares": actual,
                "price": price,
                "symbol": symbol,
            })

    def _update_minute_nav(self, ts: pd.Timestamp, row: pd.Series) -> None:
        """更新分钟级净值（用收盘价重估持仓市值）。

        与 _update_daily_nav 同款公式：nav = cash + position × close。
        Why 单独命名：未来若要在分钟级引入更复杂的净值口径（如未实现盈亏、
        浮动保证金），可在此扩展而不影响日级 _update_daily_nav。
        """
        if self.position > 0:
            position_value = self.position * row["close"]
        else:
            position_value = 0.0
        self.nav = self.cash + position_value