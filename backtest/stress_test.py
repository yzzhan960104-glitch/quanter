"""极端场景模拟器

职责：
1. 模拟涨跌停板限制
2. 模拟流动性枯竭
3. 模拟指数熔断
4. 模拟黑天鹅事件

设计原则：
- 每个场景独立可配置
- 场景叠加可模拟复杂情况
- 纯向量化实现
"""
import numpy as np
import pandas as pd
from typing import Literal, Optional, Dict, Any


class StressTester:
    """
    极端场景模拟器

    可模拟场景：
    1. 涨跌停板限制（10%/20%）
    2. 流动性枯竭（成交量骤降）
    3. 指数熔断（全市场暂停交易）
    4. 黑天鹅事件（单日暴跌 > 10%）
    """

    def __init__(self):
        """初始化极端场景模拟器"""
        self.scenarios_applied = []

    def apply_limit_up_down(
        self,
        df: pd.DataFrame,
        limit_rate: float = 0.10,
        limit_type: Literal["both", "up", "down"] = "both"
    ) -> pd.DataFrame:
        """
        应用涨跌停板限制

        参数：
            df: 原始 OHLCV 数据
            limit_rate: 涨跌停板幅度（10% 或 20%）
            limit_type: 限制类型（"both"/"up"/"down"）

        返回：
            应用涨跌停板后的数据

        涨跌停板规则：
        - 涨停：最高价 = 前一日收盘价 × (1 + limit_rate)
        - 跌停：最低价 = 前一日收盘价 × (1 - limit_rate)
        - 收盘价被限制在涨跌停板内
        """
        df_stress = df.copy()
        # volume 可能为 int32，后续 *= 浮点在 pandas 2.x CoW 下抛 LossySetitemError；
        # 压力模拟允许分数成交量，先转 float 解除 dtype 约束。
        df_stress["volume"] = df_stress["volume"].astype(float)

        # 计算前一交易日收盘价
        prev_close = df_stress["close"].shift(1)

        # 计算涨跌停价格
        limit_up_price = prev_close * (1 + limit_rate)
        limit_down_price = prev_close * (1 - limit_rate)

        # 应用涨停限制
        if limit_type in ["both", "up"]:
            # 收盘价不能超过涨停价
            limit_up_mask = df_stress["close"] > limit_up_price
            df_stress.loc[limit_up_mask, "close"] = limit_up_price[limit_up_mask]

            # 最高价不能超过涨停价
            df_stress.loc[limit_up_mask, "high"] = limit_up_price[limit_up_mask]

            # 涨停日成交量萎缩（模拟真实情况）
            df_stress.loc[limit_up_mask, "volume"] *= 0.3

        # 应用跌停限制
        if limit_type in ["both", "down"]:
            # 收盘价不能低于跌停价
            limit_down_mask = df_stress["close"] < limit_down_price
            df_stress.loc[limit_down_mask, "close"] = limit_down_price[limit_down_mask]

            # 最低价不能低于跌停价
            df_stress.loc[limit_down_mask, "low"] = limit_down_price[limit_down_mask]

            # 跌停日成交量萎缩
            df_stress.loc[limit_down_mask, "volume"] *= 0.1

        self.scenarios_applied.append("limit_up_down")

        return df_stress

    def apply_liquidity_crisis(
        self,
        df: pd.DataFrame,
        crisis_dates: Optional[pd.DatetimeIndex] = None,
        crisis_ratio: float = 0.1,
        duration: int = 5
    ) -> pd.DataFrame:
        """
        应用流动性枯竭场景

        参数：
            df: 原始 OHLCV 数据
            crisis_dates: 流动性枯竭起始日期（可选）
            crisis_ratio: 流动性枯竭程度（成交量剩余比例）
            duration: 流动性枯竭持续天数

        返回：
            应用流动性枯竭后的数据

        流动性枯竭效果：
        - 成交量萎缩至危机比例
        - 滑点放大（在成本模型中处理）
        """
        df_stress = df.copy()
        # volume 可能为 int32，后续 *= crisis_ratio（浮点）在 pandas 2.x CoW 下抛
        # LossySetitemError；压力模拟允许分数成交量，先转 float 解除 dtype 约束。
        df_stress["volume"] = df_stress["volume"].astype(float)

        # 如果未指定日期，随机选择
        if crisis_dates is None:
            n = len(df_stress)
            n_crisis = max(1, n // 100)  # 约 1% 的日期触发危机
            crisis_indices = np.random.choice(n, size=n_crisis, replace=False)
            crisis_dates = df_stress.index[crisis_indices]

        # 应用流动性枯竭
        for crisis_date in crisis_dates:
            # 获取危机日期的索引
            idx = df_stress.index.get_loc(crisis_date)

            # 应用持续 duration 天
            for i in range(duration):
                if idx + i < len(df_stress):
                    df_stress.iloc[idx + i, df_stress.columns.get_loc("volume")] *= crisis_ratio

        self.scenarios_applied.append("liquidity_crisis")

        return df_stress

    def apply_circuit_breaker(
        self,
        df: pd.DataFrame,
        threshold: float = 0.07,
        pause_duration: int = 1
    ) -> pd.DataFrame:
        """
        应用指数熔断场景

        参数：
            df: 原始 OHLCV 数据
            threshold: 熔断阈值（如 7%）
            pause_duration: 熔断持续天数

        返回：
            应用熔断后的数据

        熔断规则：
        - 指数跌幅 > 阈值，触发熔断
        - 熔断期间无交易（成交量 = 0）
        """
        df_stress = df.copy()

        # 计算日收益率
        daily_return = df_stress["close"].pct_change()

        # 检测熔断触发
        circuit_breaker_mask = daily_return < -threshold

        # 应用熔断
        for idx in df_stress.index[circuit_breaker_mask]:
            idx_pos = df_stress.index.get_loc(idx)

            # 熔断期间无交易
            for i in range(pause_duration):
                if idx_pos + i < len(df_stress):
                    df_stress.iloc[idx_pos + i, df_stress.columns.get_loc("volume")] = 0

        self.scenarios_applied.append("circuit_breaker")

        return df_stress

    def apply_black_swan(
        self,
        df: pd.DataFrame,
        swan_date: Optional[pd.Timestamp] = None,
        drop_ratio: float = 0.10,
        recovery_days: int = 20
    ) -> pd.DataFrame:
        """
        应用黑天鹅事件

        参数：
            df: 原始 OHLCV 数据
            swan_date: 黑天鹅事件日期（可选，默认随机）
            drop_ratio: 单日跌幅
            recovery_days: 恢复期天数

        返回：
            应用黑天鹅事件后的数据

        黑天鹅效果：
        - 单日暴跌
        - 成交量放大（恐慌性抛售）
        - 后续逐步恢复
        """
        df_stress = df.copy()

        # 如果未指定日期，随机选择
        if swan_date is None:
            swan_idx = np.random.randint(0, len(df_stress) - recovery_days)
            swan_date = df_stress.index[swan_idx]
        else:
            swan_idx = df_stress.index.get_loc(swan_date)

        # 单日暴跌
        close_before = df_stress.iloc[swan_idx, df_stress.columns.get_loc("close")]
        df_stress.iloc[swan_idx, df_stress.columns.get_loc("close")] *= (1 - drop_ratio)
        df_stress.iloc[swan_idx, df_stress.columns.get_loc("low")] *= (1 - drop_ratio)
        df_stress.iloc[swan_idx, df_stress.columns.get_loc("high")] *= (1 - drop_ratio * 0.5)
        df_stress.iloc[swan_idx, df_stress.columns.get_loc("open")] *= (1 - drop_ratio * 0.8)

        # 成交量放大（恐慌性抛售）
        df_stress.iloc[swan_idx, df_stress.columns.get_loc("volume")] *= 3.0

        # 恢复期（逐步反弹）
        for i in range(1, recovery_days + 1):
            if swan_idx + i < len(df_stress):
                recovery_rate = (drop_ratio / recovery_days) * (i / recovery_days)
                df_stress.iloc[swan_idx + i, df_stress.columns.get_loc("close")] *= (1 + recovery_rate)

        self.scenarios_applied.append("black_swan")

        return df_stress

    def get_applied_scenarios(self) -> list:
        """
        获取已应用的极端场景

        返回：
            已应用场景列表
        """
        return self.scenarios_applied

    def reset(self):
        """
        重置场景记录
        """
        self.scenarios_applied = []

    def generate_stress_report(self, original_df: pd.DataFrame, stressed_df: pd.DataFrame) -> Dict[str, Any]:
        """
        生成极端场景压力测试报告

        参数：
            original_df: 原始数据
            stressed_df: 应用极端场景后的数据

        返回：
            压力测试报告
        """
        report = {
            "scenarios_applied": self.get_applied_scenarios(),
            "original_return": original_df["close"].pct_change().sum(),
            "stressed_return": stressed_df["close"].pct_change().sum(),
            "return_diff": stressed_df["close"].pct_change().sum() - original_df["close"].pct_change().sum(),
            "original_volatility": original_df["close"].pct_change().std(),
            "stressed_volatility": stressed_df["close"].pct_change().std(),
            "max_drawdown_original": self._calculate_max_drawdown(original_df),
            "max_drawdown_stressed": self._calculate_max_drawdown(stressed_df),
        }

        return report

    def _calculate_max_drawdown(self, df: pd.DataFrame) -> float:
        """
        计算最大回撤

        参数：
            df: OHLCV 数据

        返回：
            最大回撤
        """
        cumulative = (1 + df["close"].pct_change()).cumprod()
        rolling_max = cumulative.expanding().max()
        drawdown = (cumulative - rolling_max) / rolling_max
        return drawdown.min()