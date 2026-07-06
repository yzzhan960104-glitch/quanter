"""宏观基本面因子

职责：
1. 处理宏观指标数据（M2、CPI、PPI 等）
2. 生成宏观锚点信号
3. 防范前视偏差（使用发布时间而非数据发生时间）

设计原则：
- 宏观数据的 index 必须是发布时间
- 信号只能在发布日及之后生效
- 纯多头策略（信号范围 [0, 1]）
"""
import numpy as np
import pandas as pd
from typing import Optional


def macro_anchor_signal(
    macro_df: pd.DataFrame,
    indicator: str = "m2",
    threshold: float = 0.02,
    window: int = 3
) -> pd.Series:
    """
    宏观基本面锚点信号

    逻辑：
    - 当宏观指标（如 M2 增速）超过阈值时，释放流动性，市场偏多
    - 连续多期超过阈值，信号更强

    信号定义：
    - 1.0: 宏观指标连续 window 期超过阈值（强多头信号）
    - 0.5: 宏观指标单期超过阈值（中等多头信号）
    - 0.0: 宏观指标低于阈值（无信号）

    防范前视偏差的关键：
    - 宏观数据的 index 必须是发布时间，而非数据发生时间
    - 例如：2024年1月CPI数据可能在2024年2月15日发布，信号只能在2月15日及之后生效

    参数：
        macro_df: 宏观数据（index 为发布时间）
        indicator: 宏观指标名称
        threshold: 阈值（如 M2 增速 2%）
        window: 连续超过阈值期数

    返回：
        信号序列（index 与 macro_df 一致，值在 [0, 1] 范围内）
    """
    # 1. 检查数据是否存在
    if indicator not in macro_df.columns:
        raise ValueError(f"宏观数据中不存在指标: {indicator}")

    # 2. 计算环比增速（防范除以零）
    prev_value = macro_df[indicator].shift(1)
    growth_rate = (macro_df[indicator] - prev_value) / prev_value.replace(0, 1)

    # 3. 超阈值的判断
    exceed_threshold = growth_rate > threshold

    # 4. 连续 window 期超过阈值
    consecutive_exceed = exceed_threshold.rolling(window=window).sum() >= window

    # 5. 构建信号序列
    signal = pd.Series(0.0, index=macro_df.index)

    # 连续超过阈值：强多头
    signal[consecutive_exceed] = 1.0

    # 单期超过阈值：中等多头
    signal[exceed_threshold & ~consecutive_exceed] = 0.5

    # 6. 填充 NaN（Pandas 2.x 使用 ffill()）
    signal = signal.ffill(limit=window)
    signal = signal.fillna(0.0)

    return signal


def cpi_inflation_signal(
    macro_df: pd.DataFrame,
    threshold: float = 0.03,
    window: int = 2
) -> pd.Series:
    """
    CPI 通胀信号

    逻辑：
    - CPI 低于阈值：通胀压力小，流动性宽松，偏多
    - CPI 高于阈值：通胀压力大，流动性收紧，偏空

    纯多头策略：
    - CPI < 阈值：信号递增
    - CPI > 阈值：信号递减（但不低于 0）

    参数：
        macro_df: 宏观数据
        threshold: CPI 阈值（如 3%）
        window: 连续超过/低于阈值期数

    返回：
        信号序列（index 与 macro_df 一致，值在 [0, 1] 范围内）
    """
    # 1. 检查数据是否存在
    if "cpi" not in macro_df.columns:
        raise ValueError("宏观数据中不存在指标: cpi")

    # 2. 获取 CPI 值
    cpi_value = macro_df["cpi"]

    # 3. 低于阈值的判断
    below_threshold = cpi_value < threshold

    # 4. 连续 window 期低于阈值
    consecutive_below = below_threshold.rolling(window=window).sum() >= window

    # 5. 连续 window 期高于阈值
    above_threshold = cpi_value > threshold
    consecutive_above = above_threshold.rolling(window=window).sum() >= window

    # 6. 构建信号序列
    signal = pd.Series(0.5, index=macro_df.index)

    # 连续低于阈值：强多头（0.8）
    signal[consecutive_below] = 0.8

    # 单期低于阈值：中等多头（0.6）
    signal[below_threshold & ~consecutive_below] = 0.6

    # 连续高于阈值：弱多头（0.2）
    signal[consecutive_above] = 0.2

    # 单期高于阈值：中等空头（0.4）
    signal[above_threshold & ~consecutive_above] = 0.4

    # 7. 填充 NaN
    signal = signal.ffill(limit=window)
    signal = signal.fillna(0.5)

    return signal


def social_financing_signal(
    macro_df: pd.DataFrame,
    threshold: float = 0.10,
    window: int = 3
) -> pd.Series:
    """
    社融规模信号

    逻辑：
    - 社融规模增速超过阈值：流动性充裕，偏多
    - 社融规模增速低于阈值：流动性收紧，偏空

    纯多头策略：
    - 社融增速 > 阈值：信号递增
    - 社融增速 < 阈值：信号递减

    参数：
        macro_df: 宏观数据
        threshold: 社融增速阈值（如 10%）
        window: 连续超过/低于阈值期数

    返回：
        信号序列（index 与 macro_df 一致，值在 [0, 1] 范围内）
    """
    # 1. 检查数据是否存在
    if "social_financing" not in macro_df.columns:
        raise ValueError("宏观数据中不存在指标: social_financing")

    # 2. 计算社融增速
    prev_value = macro_df["social_financing"].shift(1)
    growth_rate = (macro_df["social_financing"] - prev_value) / prev_value.replace(0, 1)

    # 3. 超阈值的判断
    exceed_threshold = growth_rate > threshold

    # 4. 连续 window 期超过阈值
    consecutive_exceed = exceed_threshold.rolling(window=window).sum() >= window

    # 5. 构建信号序列
    signal = pd.Series(0.0, index=macro_df.index)

    # 连续超过阈值：强多头（0.8）
    signal[consecutive_exceed] = 0.8

    # 单期超过阈值：中等多头（0.6）
    signal[exceed_threshold & ~consecutive_exceed] = 0.6

    # 6. 填充 NaN
    signal = signal.ffill(limit=window)
    signal = signal.fillna(0.4)  # 默认中等偏空

    return signal