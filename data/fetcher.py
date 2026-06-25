"""数据获取统一接口

职责：
1. 定义抽象数据获取接口
2. 实现模拟数据获取器（用于开发与测试）
3. 预留真实数据源接入（如 Wind/同花顺/QMT）

设计原则：
- 第一性原理：返回纯 Pandas DataFrame，无黑盒封装
- 前视偏差防范：宏观数据返回发布时间，而非数据发生时间
- 异常值标记：不静默处理缺失值，而是显式标记
"""
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional
import numpy as np
import pandas as pd


class DataFetcher(ABC):
    """数据获取统一接口，支持多数据源切换"""

    @abstractmethod
    def fetch_ohlcv(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        freq: str = "1d"
    ) -> pd.DataFrame:
        """
        获取 OHLCV 数据

        参数：
            symbol: 交易标的代码（如 "600000.SH"）
            start: 起始时间
            end: 结束时间
            freq: 频率（"1d"/"1h"/"5m"/"1m"）

        返回：
            DataFrame with index: DatetimeIndex (tz-aware, Asia/Shanghai)
            columns: ['open', 'high', 'low', 'close', 'volume', 'amount']

        注意：
            - 必须返回 tz-aware 的时间戳
            - 涨跌停板日的价格应包含实际成交价（而非理论限价）
            - 缺失交易日不应被插值（前视偏差防范）
        """
        pass

    @abstractmethod
    def fetch_macro(
        self,
        indicator: str,
        start: datetime,
        end: datetime
    ) -> pd.DataFrame:
        """
        获取宏观数据

        参数：
            indicator: 宏观指标名称（如 "m2", "cpi", "ppi"）
            start: 起始时间
            end: 结束时间

        返回：
            DataFrame with index: DatetimeIndex（发布时间，防范前视偏差）
            columns: [indicator]

        注意：
            - index 必须是发布时间，而非数据发生时间
            - 例如：2024年1月CPI可能在2024年2月15日发布
            - 信号只能在发布日及之后生效
        """
        pass

    @abstractmethod
    def fetch_factor_data(
        self,
        symbol: str,
        factor_name: str,
        start: datetime,
        end: datetime
    ) -> pd.DataFrame:
        """
        获取因子数据（如 P/E、市净率）

        参数：
            symbol: 交易标的代码
            factor_name: 因子名称
            start: 起始时间
            end: 结束时间

        返回：
            DataFrame with index: DatetimeIndex
            columns: [factor_name]

        注意：
            - 基本面数据存在前视偏差风险（财报发布滞后）
            - 应返回数据的"可见日期"，而非"数据日期"
        """
        pass


class MockDataFetcher(DataFetcher):
    """
    Mock 数据获取器（用于开发与测试）

    生成符合 A 股特征的模拟数据：
    - 涨跌停板限制（10%/20%）
    - 成交量波动
    - 价格趋势（可配置）
    """

    def __init__(self, seed: Optional[int] = 42):
        """
        初始化 Mock 数据生成器

        参数：
            seed: 随机种子（确保可复现）
        """
        self.rng = np.random.default_rng(seed)

    def fetch_ohlcv(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        freq: str = "1d"
    ) -> pd.DataFrame:
        """生成模拟 OHLCV 数据"""
        # 生成日期范围（仅包含交易日，排除周末）
        date_range = pd.date_range(
            start=start,
            end=end,
            freq="B"  # Business days（排除周末）
        )
        date_range = date_range.tz_localize("Asia/Shanghai")

        # 模拟价格（几何布朗运动）
        n = len(date_range)
        returns = self.rng.normal(loc=0.0005, scale=0.02, size=n)
        prices = np.cumprod(1 + returns) * 100  # 起始价格 100 元

        # 模拟 OHLC（开盘价 = 前一日收盘价 ± 小幅随机）
        opens = np.roll(prices, 1)
        opens[0] = prices[0]
        opens += self.rng.normal(loc=0, scale=0.5, size=n)

        # 最高价和最低价（基于开盘价和收盘价）
        highs = np.maximum(opens, prices) + self.rng.uniform(0, 1, size=n)
        lows = np.minimum(opens, prices) - self.rng.uniform(0, 1, size=n)

        # 模拟成交量（对数正态分布）
        volumes = self.rng.lognormal(mean=15, sigma=0.5, size=n)

        # 模拟成交额（成交量 × 平均价）
        amounts = volumes * ((opens + prices) / 2)

        # 构建 DataFrame
        df = pd.DataFrame(
            {
                "open": opens,
                "high": highs,
                "low": lows,
                "close": prices,
                "volume": volumes,
                "amount": amounts,
            },
            index=date_range
        )

        # 应用涨跌停板限制（10%）
        limit_up = 1.10
        limit_down = 0.90

        # 涨停处理
        limit_up_mask = df["close"] >= df["open"].shift(1) * limit_up
        df.loc[limit_up_mask, "close"] = df.loc[limit_up_mask, "open"].shift(1) * limit_up
        df.loc[limit_up_mask, "high"] = df.loc[limit_up_mask, "close"]
        df.loc[limit_up_mask, "low"] = df.loc[limit_up_mask, "open"]

        # 跌停处理
        limit_down_mask = df["close"] <= df["open"].shift(1) * limit_down
        df.loc[limit_down_mask, "close"] = df.loc[limit_down_mask, "open"].shift(1) * limit_down
        df.loc[limit_down_mask, "low"] = df.loc[limit_down_mask, "close"]
        df.loc[limit_down_mask, "high"] = df.loc[limit_down_mask, "open"]

        # 跌停日成交量萎缩
        df.loc[limit_down_mask, "volume"] *= 0.1

        return df

    def fetch_macro(
        self,
        indicator: str,
        start: datetime,
        end: datetime
    ) -> pd.DataFrame:
        """生成模拟宏观数据"""
        # 宏观数据月度发布
        date_range = pd.date_range(
            start=start,
            end=end,
            freq="MS"  # Month start
        )
        date_range = date_range.tz_localize("Asia/Shanghai")

        # 模拟 M2 增速（正态分布，均值 8%）
        if indicator == "m2":
            values = self.rng.normal(loc=0.08, scale=0.02, size=len(date_range))
        # 模拟 CPI（正态分布，均值 2%）
        elif indicator == "cpi":
            values = self.rng.normal(loc=0.02, scale=0.01, size=len(date_range))
        else:
            values = self.rng.normal(loc=0.05, scale=0.05, size=len(date_range))

        df = pd.DataFrame(
            {indicator: values},
            index=date_range
        )

        # 模拟发布延迟（数据在下月 15 日发布，防范前视偏差）
        # 注意：在实际系统中，应从数据源获取真实的发布时间
        df.index = df.index + pd.DateOffset(days=15)

        return df

    def fetch_factor_data(
        self,
        symbol: str,
        factor_name: str,
        start: datetime,
        end: datetime
    ) -> pd.DataFrame:
        """生成模拟因子数据"""
        date_range = pd.date_range(
            start=start,
            end=end,
            freq="B"
        )
        date_range = date_range.tz_localize("Asia/Shanghai")

        # 模拟 P/E 比率（对数正态分布）
        if factor_name == "pe":
            values = self.rng.lognormal(mean=2.5, sigma=0.5, size=len(date_range))
        # 模拟市净率
        elif factor_name == "pb":
            values = self.rng.lognormal(mean=1.0, sigma=0.3, size=len(date_range))
        else:
            values = self.rng.normal(loc=10.0, scale=5.0, size=len(date_range))

        df = pd.DataFrame(
            {factor_name: values},
            index=date_range
        )

        return df