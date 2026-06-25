"""技术指标因子（纯向量化实现）

职责：
1. 计算移动平均线信号
2. 计算量价趋势因子（VPT）
3. 计算其他技术指标（RSI、MACD 等，按需扩展）

设计原则：
- 纯向量化实现（无 for 循环）
- 防范前视偏差（使用 shift(1)）
- 纯多头策略（信号范围 [0, 1]）
- 显式处理异常值
"""
import numpy as np
import pandas as pd
from typing import Tuple, Optional


def moving_average_cross(
    df: pd.DataFrame,
    short_window: int = 5,
    long_window: int = 20
) -> pd.Series:
    """
    双均线交叉信号（纯多头策略）

    信号定义：
    - 1.0: 短均线上穿长均线（金叉，买入信号）
    - 0.5: 短均线 > 长均线（持仓状态）
    - 0.0: 短均线下穿长均线（死叉，卖出信号）

    防范前视偏差的关键：
    - 使用 shift(1) 确保只使用历史数据计算信号
    - 信号在 t+1 开盘时生效，而非 t 日收盘时

    参数：
        df: OHLCV 数据（需包含 'close' 列）
        short_window: 短均线周期
        long_window: 长均线周期

    返回：
        信号序列（index 与 df 一致，值在 [0, 1] 范围内）
    """
    # 1. 计算均线（纯向量化，无 for 循环）
    short_ma = df["close"].rolling(window=short_window).mean()
    long_ma = df["close"].rolling(window=long_window).mean()

    # 2. 检测金叉与死叉（使用 shift(1) 防范前视偏差）
    # 金叉：昨日短<长，今日短>长
    golden_cross = (
        (short_ma.shift(1) < long_ma.shift(1)) &
        (short_ma > long_ma)
    )

    # 死叉：昨日短>长，今日短<长
    death_cross = (
        (short_ma.shift(1) > long_ma.shift(1)) &
        (short_ma < long_ma)
    )

    # 3. 构建信号序列
    signal = pd.Series(0.0, index=df.index)

    # 金叉：满仓
    signal[golden_cross] = 1.0

    # 死叉：空仓
    signal[death_cross] = 0.0

    # 持仓状态：当前短>长（非金叉死叉日）
    holding = (short_ma > long_ma) & ~golden_cross & ~death_cross
    signal[holding] = 0.5

    # 4. 填充前段 NaN（均线计算需要足够历史数据）
    # 使用前向填充，但最多填充 long_window-1 天（Pandas 2.x 使用 ffill()）
    signal = signal.ffill(limit=long_window - 1)
    signal = signal.fillna(0.0)  # 仍为 NaN 的部分填充为 0（空仓）

    return signal


def volume_price_trend(
    df: pd.DataFrame,
    window: int = 20,
    abnormal_threshold: float = 5.0
) -> pd.Series:
    """
    量价趋势因子（VPT，Volume Price Trend）

    逻辑：
    - 价格上涨时成交量放大为正向信号
    - 价格下跌时放量为负向信号

    纯多头策略：归一化到 [0, 1]

    参数：
        df: OHLCV 数据（需包含 'close', 'volume' 列）
        window: VPT 计算窗口
        abnormal_threshold: 异常成交量阈值（倍数标准差）

    返回：
        信号序列（index 与 df 一致，值在 [0, 1] 范围内）
    """
    # 1. 计算价格变化率与成交量变化率
    price_change = df["close"].pct_change()
    volume_change = df["volume"].pct_change()

    # 2. 检测异常成交量（防范极端行情影响信号）
    volume_std = df["volume"].rolling(window=window).std()
    volume_mean = df["volume"].rolling(window=window).mean()

    # 避免除以零
    volume_std = volume_std.replace(0, 1)
    abnormal_volume = (df["volume"] - volume_mean).abs() > abnormal_threshold * volume_std

    # 3. 计算 VPT（价格变化 × 成交量变化，滚动累加）
    vpt = (price_change * volume_change).rolling(window=window).sum()

    # 4. 异常日不参与计算（防范极端行情）
    vpt[abnormal_volume] = np.nan

    # 5. 归一化到 [0, 1]（纯多头策略）
    # 使用滚动窗口内的 min-max 归一化，防范未来函数
    vpt_min = vpt.rolling(window=window * 2, min_periods=window).min()
    vpt_max = vpt.rolling(window=window * 2, min_periods=window).max()

    # 避免除以零
    vpt_range = vpt_max - vpt_min
    vpt_range = vpt_range.replace(0, 1)

    vpt_norm = (vpt - vpt_min) / vpt_range

    # 6. 填充 NaN（Pandas 2.x 使用 ffill()）
    vpt_norm = vpt_norm.ffill(limit=window)
    vpt_norm = vpt_norm.fillna(0.5)  # 默认中等仓位

    return vpt_norm


def rsi(
    df: pd.DataFrame,
    window: int = 14
) -> pd.Series:
    """
    相对强弱指标（RSI，Relative Strength Index）

    纯多头策略：
    - RSI > 70: 超买，降低仓位
    - RSI < 30: 超卖，提高仓位

    参数：
        df: OHLCV 数据（需包含 'close' 列）
        window: RSI 计算窗口（默认 14）

    返回：
        信号序列（index 与 df 一致，值在 [0, 1] 范围内）
    """
    # 1. 计算价格变化
    delta = df["close"].diff()

    # 2. 分离上涨和下跌（纯向量化）
    gain = delta.where(delta > 0, 0)
    loss = -delta.where(delta < 0, 0)

    # 3. 计算平均上涨和平均下跌
    avg_gain = gain.rolling(window=window).mean()
    avg_loss = loss.rolling(window=window).mean()

    # 4. 避免除以零
    avg_loss = avg_loss.replace(0, 1e-10)

    # 5. 计算 RSI
    rs = avg_gain / avg_loss
    rsi_value = 100 - (100 / (1 + rs))

    # 6. 转换为信号（纯多头策略）
    # RSI > 70: 信号递减（0.3）
    # RSI < 30: 信号递增（0.7）
    # 30 <= RSI <= 70: 线性映射到 [0.3, 0.7]
    signal = pd.Series(0.5, index=df.index)

    overbought_mask = rsi_value > 70
    oversold_mask = rsi_value < 30
    normal_mask = (~overbought_mask) & (~oversold_mask)

    # 超买区域：线性递减到 0.3
    signal[overbought_mask] = 0.5 - 0.2 * (rsi_value[overbought_mask] - 70) / 30

    # 超卖区域：线性递增到 0.7
    signal[oversold_mask] = 0.5 + 0.2 * (30 - rsi_value[oversold_mask]) / 30

    # 正常区域：保持 0.5
    signal[normal_mask] = 0.5

    # 7. 填充 NaN（Pandas 2.x 使用 ffill()）
    signal = signal.ffill(limit=window)
    signal = signal.fillna(0.5)

    return signal


def macd(
    df: pd.DataFrame,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9
) -> pd.Series:
    """
    MACD 信号（Moving Average Convergence Divergence）

    纯多头策略：
    - MACD 上穿信号线：买入
    - MACD 下穿信号线：卖出

    参数：
        df: OHLCV 数据（需包含 'close' 列）
        fast: 快线周期
        slow: 慢线周期
        signal: 信号线周期

    返回：
        信号序列（index 与 df 一致，值在 [0, 1] 范围内）
    """
    # 1. 计算指数移动平均（EMA）
    ema_fast = df["close"].ewm(span=fast, adjust=False).mean()
    ema_slow = df["close"].ewm(span=slow, adjust=False).mean()

    # 2. 计算 MACD 线
    macd_line = ema_fast - ema_slow

    # 3. 计算信号线
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()

    # 4. 计算柱状图（Histogram）
    histogram = macd_line - signal_line

    # 5. 生成信号（使用 shift(1) 防范前视偏差）
    # MACD 上穿信号线：买入
    cross_up = (
        (macd_line.shift(1) < signal_line.shift(1)) &
        (macd_line > signal_line)
    )

    # MACD 下穿信号线：卖出
    cross_down = (
        (macd_line.shift(1) > signal_line.shift(1)) &
        (macd_line < signal_line)
    )

    # 6. 构建信号序列
    signal = pd.Series(0.0, index=df.index)

    # 金叉：满仓
    signal[cross_up] = 1.0

    # 死叉：空仓
    signal[cross_down] = 0.0

    # 持仓状态：MACD > 信号线
    holding = (macd_line > signal_line) & ~cross_up & ~cross_down
    signal[holding] = 0.5

    # 7. 填充 NaN（Pandas 2.x 使用 ffill()）
    signal = signal.ffill(limit=slow)
    signal = signal.fillna(0.0)

    return signal