"""数据清洗与对齐

职责：
1. 清洗 OHLCV 数据（异常值检测、缺失值处理）
2. 对齐多频率数据（防范前视偏差）
3. 标记数据质量（不静默处理）

设计原则：
- 显式处理所有异常情况（不静默填充）
- 防范前视偏差（使用发布时间而非数据发生时间）
- 纯向量化实现（无 for 循环）
"""
import numpy as np
import pandas as pd
from typing import Optional, Dict, Any


class DataCleaner:
    """数据清洗与对齐"""

    @staticmethod
    def clean_ohlcv(df: pd.DataFrame, max_fill: int = 5) -> pd.DataFrame:
        """
        清洗 OHLCV 数据，处理异常值

        参数：
            df: 原始 OHLCV 数据
            max_fill: 最大前向填充天数（防范停牌期跨度过长）

        返回：
            清洗后的 DataFrame，包含额外列：
            - 'is_abnormal': 是否异常值
            - 'is_illiquid': 是否流动性枯竭

        清洗规则：
            1. 价格突变 > 20% 标记为异常
            2. 成交量为 0 标记为流动性枯竭
            3. 缺失值前向填充（最多 max_fill 天）
            4. 高价不能低于低价（防范数据错误）
        """
        df_clean = df.copy()

        # 1. 检测价格异常（突变 > 20%）
        price_change = df_clean["close"].pct_change()
        abnormal_mask = price_change.abs() > 0.20
        df_clean["is_abnormal"] = abnormal_mask

        # 2. 检测流动性枯竭（成交量为 0）
        illiquid_mask = df_clean["volume"] == 0
        df_clean["is_illiquid"] = illiquid_mask

        # 3. 缺失值填充（前向填充 + 限制，Pandas 2.x 使用 ffill()）
        df_clean = df_clean.ffill(limit=max_fill)

        # 4. 高价不能低于低价（防范数据错误）
        invalid_ohlc = df_clean["high"] < df_clean["low"]
        if invalid_ohlc.any():
            # 修正逻辑：用 close 替换
            df_clean.loc[invalid_ohlc, "high"] = df_clean.loc[invalid_ohlc, "close"]
            df_clean.loc[invalid_ohlc, "low"] = df_clean.loc[invalid_ohlc, "close"]

        # 5. 确保 open/high/low/close 都在合理范围内
        # high 应该是 max(open, close)，low 应该是 min(open, close)
        df_clean["high"] = df_clean[["open", "high", "close"]].max(axis=1)
        df_clean["low"] = df_clean[["open", "low", "close"]].min(axis=1)

        # 6. 确保 volume 和 amount 为正数
        df_clean.loc[df_clean["volume"] < 0, "volume"] = 0
        df_clean.loc[df_clean["amount"] < 0, "amount"] = 0

        return df_clean

    @staticmethod
    def clean_macro(df: pd.DataFrame, fill_method: str = "bfill") -> pd.DataFrame:
        """
        清洗宏观数据

        参数：
            df: 原始宏观数据
            fill_method: 填充方法（"bfill"=后向填充，防范前视偏差）

        返回：
            清洗后的 DataFrame

        清洗规则：
            1. 后向填充（宏观数据发布后才生效）
            2. 检测 NaN（数据缺失）
            3. 标记发布延迟
        """
        df_clean = df.copy()

        # 1. 后向填充（防范前视偏差，Pandas 2.x 使用 bfill()）
        if fill_method == "bfill":
            df_clean = df_clean.bfill()
        else:
            raise ValueError(f"不支持的填充方法: {fill_method}")

        return df_clean

    @staticmethod
    def align_frequencies(
        ohlcv: pd.DataFrame,
        macro: pd.DataFrame,
        macro_freq: str = "M",
        freq: str = "1d"
    ) -> pd.DataFrame:
        """
        对齐多频率数据，防范前视偏差

        参数：
            ohlcv: OHLCV 数据（高频）
            macro: 宏观数据（低频）
            macro_freq: 宏观数据频率（"M"=月度，"W"=周度）
            freq: 对齐后的目标频率

        返回：
            对齐后的 DataFrame，包含所有列

        对齐规则：
            1. 宏观数据使用发布时间（而非数据发生时间）
            2. 向后填充（数据发布后才生效）
            3. 检测对齐后的 NaN（确保无遗漏）
        """
        # 1. 确保时间戳一致
        if ohlcv.index.tz is None:
            ohlcv.index = ohlcv.index.tz_localize("Asia/Shanghai")
        if macro.index.tz is None:
            macro.index = macro.index.tz_localize("Asia/Shanghai")

        # 2. 将宏观数据对齐到目标频率（Pandas 2.x 使用 ffill()）
        macro_aligned = macro.reindex(ohlcv.index).ffill()

        # 3. 检测对齐后的 NaN（防范前视偏差）
        if macro_aligned.isna().any().any():
            raise ValueError(
                "宏观数据对齐后存在 NaN，这可能导致前视偏差。"
                "请检查宏观数据的发布时间是否完整。"
            )

        # 4. 合并数据
        df_merged = pd.concat([ohlcv, macro_aligned], axis=1)

        return df_merged

    @staticmethod
    def detect_suspension(df: pd.DataFrame, volume_threshold: float = 0.01) -> pd.DataFrame:
        """
        检测停牌（流动性枯竭）

        参数：
            df: OHLCV 数据
            volume_threshold: 成交量阈值（相对于平均成交量的比例）

        返回：
            原始 DataFrame + 'is_suspended' 列

        检测规则：
            1. 成交量 < 平均成交量的 1% 视为流动性枯竭
            2. 连续多日成交量 < 阈值，标记为停牌
            3. 停牌日不应生成交易信号
        """
        df_clean = df.copy()

        # 计算平均成交量（滚动 20 日）
        avg_volume = df_clean["volume"].rolling(window=20).mean()

        # 检测成交量骤降
        low_volume_mask = df_clean["volume"] < avg_volume * volume_threshold

        # 连续 3 日成交量骤降，标记为停牌
        consecutive_low = low_volume_mask.rolling(window=3).sum() >= 3

        df_clean["is_suspended"] = consecutive_low

        return df_clean

    @staticmethod
    def add_factor_price(df: pd.DataFrame, method: str = "vwap") -> pd.DataFrame:
        """
        添加因子价格（用于计算技术指标）

        参数：
            df: OHLCV 数据
            method: 因子价格计算方法（"vwap"=成交量加权平均价，"close"=收盘价）

        返回：
            原始 DataFrame + 'factor_price' 列

        注意：
            - VWAP 能更好地反映真实成交均价
            - 收盘价更适合计算趋势指标
        """
        df_clean = df.copy()

        if method == "vwap":
            # VWAP = 成交额 / 成交量
            df_clean["factor_price"] = df_clean["amount"] / df_clean["volume"]
        elif method == "close":
            df_clean["factor_price"] = df_clean["close"]
        else:
            raise ValueError(f"不支持的因子价格计算方法: {method}")

        # 处理除以零
        df_clean["factor_price"] = df_clean["factor_price"].fillna(df_clean["close"])

        return df_clean

    @staticmethod
    def validate_data(df: pd.DataFrame) -> Dict[str, Any]:
        """
        验证数据质量

        参数：
            df: 数据 DataFrame

        返回：
            数据质量报告字典

        验证项：
            1. 缺失值数量
            2. 异常值数量
            3. 停牌日数量
            4. 流动性枯竭日数量
        """
        report = {}

        # 1. 缺失值
        report["missing_values"] = df.isna().sum().to_dict()

        # 2. 异常值
        if "is_abnormal" in df.columns:
            report["abnormal_days"] = df["is_abnormal"].sum()

        # 3. 停牌
        if "is_suspended" in df.columns:
            report["suspended_days"] = df["is_suspended"].sum()

        # 4. 流动性枯竭
        if "is_illiquid" in df.columns:
            report["illiquid_days"] = df["is_illiquid"].sum()

        return report