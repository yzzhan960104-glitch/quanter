"""收益指标计算与因子归因

职责：
1. 计算收益指标（年化、波动、回撤等）
2. 计算因子归因（技术信号 vs 宏观信号）
3. 生成滚动指标

设计原则：
- 纯向量化实现
- 防范除以零
- 显式计算每一项指标
"""
import numpy as np
import pandas as pd
from typing import Dict, Any, Optional, List


class MetricsCalculator:
    """
    收益指标计算与因子归因

    支持的指标：
    1. 基础指标：年化收益率、年化波动率、最大回撤
    2. 风险调整指标：夏普比率、卡玛比率、索提诺比率
    3. 交易指标：胜率、盈亏比、交易次数
    4. 归因指标：因子贡献度分解
    """

    @staticmethod
    def calculate_return_metrics(
        daily_returns: pd.Series,
        initial_capital: float = 1_000_000,
        risk_free_rate: float = 0.03
    ) -> Dict[str, Any]:
        """
        计算收益指标

        参数：
            daily_returns: 日收益率序列
            initial_capital: 初始资金
            risk_free_rate: 无风险利率

        返回：
            收益指标字典
        """
        # 累计收益率
        cumulative_return = (1 + daily_returns).cumprod() - 1

        # 年化收益率
        n_days = len(daily_returns)
        if n_days > 0:
            annual_return = (1 + cumulative_return.iloc[-1]) ** (252 / n_days) - 1
        else:
            annual_return = 0.0

        # 年化波动率
        annual_volatility = daily_returns.std() * np.sqrt(252)

        # 最大回撤
        cumulative = (1 + daily_returns).cumprod()
        rolling_max = cumulative.expanding().max()
        drawdown = (cumulative - rolling_max) / rolling_max
        max_drawdown = drawdown.min()
        max_drawdown_duration = drawdown[drawdown < 0].groupby((drawdown[drawdown < 0] != 0).cumsum()).count().max()

        # 夏普比率
        sharpe_ratio = (annual_return - risk_free_rate) / annual_volatility if annual_volatility > 0 else 0.0

        # 卡玛比率
        calmar_ratio = annual_return / abs(max_drawdown) if max_drawdown != 0 else 0.0

        # 索提诺比率（下行波动率）
        downside_returns = daily_returns[daily_returns < 0]
        downside_volatility = downside_returns.std() * np.sqrt(252)
        sortino_ratio = (annual_return - risk_free_rate) / downside_volatility if downside_volatility > 0 else 0.0

        return {
            "cumulative_return": cumulative_return.iloc[-1],
            "annual_return": annual_return,
            "annual_volatility": annual_volatility,
            "max_drawdown": max_drawdown,
            "max_drawdown_duration": max_drawdown_duration,
            "sharpe_ratio": sharpe_ratio,
            "calmar_ratio": calmar_ratio,
            "sortino_ratio": sortino_ratio,
        }

    @staticmethod
    def calculate_trade_metrics(
        trades: pd.DataFrame,
        initial_capital: float = 1_000_000
    ) -> Dict[str, Any]:
        """
        计算交易指标

        参数：
            trades: 交易记录 DataFrame
            initial_capital: 初始资金

        返回：
            交易指标字典
        """
        # 过滤失败交易
        successful_trades = trades[trades["direction"] != "failed"]
        failed_trades = trades[trades["direction"] == "failed"]

        # 交易次数
        n_trades = len(successful_trades)
        n_failed = len(failed_trades)

        # 买入和卖出交易
        buy_trades = successful_trades[successful_trades["direction"] == "buy"]
        sell_trades = successful_trades[successful_trades["direction"] == "sell"]

        # 计算盈亏
        profits = []
        for i, sell_trade in sell_trades.iterrows():
            # 找到对应的买入交易
            matching_buys = buy_trades[
                (buy_trades["date"] < sell_trade["date"]) &
                (buy_trades["symbol"] == sell_trade["symbol"])
            ]

            if len(matching_buys) == 0:
                continue

            # 取最后一笔买入交易（假设先进先出）
            corresponding_buy = matching_buys.iloc[-1]

            # 计算盈亏
            profit = (sell_trade["price"] - corresponding_buy["price"]) * corresponding_buy["shares"] - \
                     (sell_trade["cost"] + corresponding_buy["cost"])

            profits.append(profit)

        # 胜率
        if len(profits) > 0:
            win_count = sum(1 for p in profits if p > 0)
            loss_count = len(profits) - win_count
            win_rate = win_count / len(profits)
        else:
            win_count = 0
            loss_count = 0
            win_rate = 0.0

        # 盈亏比
        if loss_count > 0:
            avg_profit = np.mean([p for p in profits if p > 0]) if win_count > 0 else 0.0
            avg_loss = np.mean([abs(p) for p in profits if p < 0]) if loss_count > 0 else 0.0
            profit_loss_ratio = avg_profit / avg_loss if avg_loss > 0 else 0.0
        else:
            avg_profit = 0.0
            avg_loss = 0.0
            profit_loss_ratio = 0.0

        # 总盈亏
        total_profit = sum([p for p in profits if p > 0])
        total_loss = sum([abs(p) for p in profits if p < 0])

        # 交易频率
        n_days = len(trades["date"].unique())
        trade_frequency = n_trades / n_days if n_days > 0 else 0.0

        return {
            "n_trades": n_trades,
            "n_failed_trades": n_failed,
            "win_rate": win_rate,
            "profit_loss_ratio": profit_loss_ratio,
            "total_profit": total_profit,
            "total_loss": total_loss,
            "avg_profit": avg_profit,
            "avg_loss": avg_loss,
            "trade_frequency": trade_frequency,
        }

    @staticmethod
    def calculate_factor_attribution(
        tech_signal: pd.Series,
        macro_signal: pd.Series,
        fused_signal: pd.Series,
        daily_returns: pd.Series,
        window: int = 20
    ) -> Dict[str, Any]:
        """
        计算因子归因

        参数：
            tech_signal: 技术信号
            macro_signal: 宏观信号
            fused_signal: 融合信号
            daily_returns: 日收益率
            window: 归因窗口

        返回：
            因子归因字典
        """
        # 对齐索引
        aligned_index = tech_signal.index.intersection(macro_signal.index).intersection(fused_signal.index).intersection(daily_returns.index)

        tech_aligned = tech_signal.loc[aligned_index]
        macro_aligned = macro_signal.loc[aligned_index]
        fused_aligned = fused_signal.loc[aligned_index]
        returns_aligned = daily_returns.loc[aligned_index]

        # 计算信号与收益率的相关性
        tech_corr = tech_aligned.corr(returns_aligned)
        macro_corr = macro_aligned.corr(returns_aligned)
        fused_corr = fused_aligned.corr(returns_aligned)

        # 计算滚动相关性
        tech_rolling_corr = tech_aligned.rolling(window=window).corr(returns_aligned)
        macro_rolling_corr = macro_aligned.rolling(window=window).corr(returns_aligned)
        fused_rolling_corr = fused_aligned.rolling(window=window).corr(returns_aligned)

        # 计算信号贡献度（基于权重的简单归因）
        # 假设融合权重为 {'tech': 0.7, 'macro': 0.3}
        tech_contribution = abs(tech_corr) * 0.7
        macro_contribution = abs(macro_corr) * 0.3

        return {
            "tech_correlation": tech_corr,
            "macro_correlation": macro_corr,
            "fused_correlation": fused_corr,
            "tech_rolling_corr": tech_rolling_corr,
            "macro_rolling_corr": macro_rolling_corr,
            "fused_rolling_corr": fused_rolling_corr,
            "tech_contribution": tech_contribution,
            "macro_contribution": macro_contribution,
        }

    @staticmethod
    def calculate_rolling_metrics(
        daily_returns: pd.Series,
        window: int = 20,
        risk_free_rate: float = 0.03
    ) -> pd.DataFrame:
        """
        计算滚动指标

        参数：
            daily_returns: 日收益率
            window: 滚动窗口
            risk_free_rate: 无风险利率

        返回：
            滚动指标 DataFrame
        """
        # 滚动收益率
        rolling_return = daily_returns.rolling(window=window).sum()

        # 滚动波动率
        rolling_volatility = daily_returns.rolling(window=window).std() * np.sqrt(252)

        # 滚动夏普比率
        annual_rf = risk_free_rate / 252
        rolling_sharpe = (rolling_return / window - annual_rf) / (rolling_volatility / np.sqrt(252))

        # 滚动最大回撤
        cumulative = (1 + daily_returns).cumprod()
        rolling_max = cumulative.rolling(window=window).max()
        rolling_drawdown = (cumulative - rolling_max) / rolling_max

        df = pd.DataFrame({
            "rolling_return": rolling_return,
            "rolling_volatility": rolling_volatility,
            "rolling_sharpe": rolling_sharpe,
            "rolling_drawdown": rolling_drawdown,
        })

        return df

    @staticmethod
    def generate_metrics_report(
        return_metrics: Dict[str, Any],
        trade_metrics: Dict[str, Any],
        factor_attribution: Optional[Dict[str, Any]] = None
    ) -> str:
        """
        生成指标报告（文本格式）

        参数：
            return_metrics: 收益指标
            trade_metrics: 交易指标
            factor_attribution: 因子归因（可选）

        返回：
            报告文本
        """
        report = []
        report.append("=" * 60)
        report.append("回测指标报告")
        report.append("=" * 60)

        # 收益指标
        report.append("\n【收益指标】")
        report.append(f"累计收益率: {return_metrics['cumulative_return']:.2%}")
        report.append(f"年化收益率: {return_metrics['annual_return']:.2%}")
        report.append(f"年化波动率: {return_metrics['annual_volatility']:.2%}")
        report.append(f"最大回撤: {return_metrics['max_drawdown']:.2%}")
        report.append(f"最大回撤持续天数: {return_metrics['max_drawdown_duration']:.0f}")

        # 风险调整指标
        report.append("\n【风险调整指标】")
        report.append(f"夏普比率: {return_metrics['sharpe_ratio']:.2f}")
        report.append(f"卡玛比率: {return_metrics['calmar_ratio']:.2f}")
        report.append(f"索提诺比率: {return_metrics['sortino_ratio']:.2f}")

        # 交易指标
        report.append("\n【交易指标】")
        report.append(f"交易次数: {trade_metrics['n_trades']}")
        report.append(f"失败交易次数: {trade_metrics['n_failed_trades']}")
        report.append(f"胜率: {trade_metrics['win_rate']:.2%}")
        report.append(f"盈亏比: {trade_metrics['profit_loss_ratio']:.2f}")
        report.append(f"总盈利: {trade_metrics['total_profit']:.2f}")
        report.append(f"总亏损: {trade_metrics['total_loss']:.2f}")
        report.append(f"交易频率: {trade_metrics['trade_frequency']:.2f} 次/天")

        # 因子归因
        if factor_attribution is not None:
            report.append("\n【因子归因】")
            report.append(f"技术信号相关性: {factor_attribution['tech_correlation']:.4f}")
            report.append(f"宏观信号相关性: {factor_attribution['macro_correlation']:.4f}")
            report.append(f"融合信号相关性: {factor_attribution['fused_correlation']:.4f}")
            report.append(f"技术信号贡献度: {factor_attribution['tech_contribution']:.4f}")
            report.append(f"宏观信号贡献度: {factor_attribution['macro_contribution']:.4f}")

        report.append("\n" + "=" * 60)

        return "\n".join(report)