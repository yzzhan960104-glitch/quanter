"""探索性动量因子矩阵（纯 Pandas/NumPy 向量化，零黑盒）。

包含：
- 横截面动量：滚动收益在全市场的百分位排名。
- 波动率调整动量：滚动收益 / ATR（ATR 防除零）。
- 赫斯顿指数：R/S 重标极差法估计持续性（逐标量，循环可接受）。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .base import register_factor, FactorMeta


@register_factor(FactorMeta(
    name="cross_sectional_momentum",
    label="横截面动量",
    category="动量",
    author="系统",
    status="live",                 # 唯一已实盘服役的探索性因子（explorer 网格已集成）
    input_kind="returns_panel",
    dataset="daily",
    description="滚动累计收益的逐日横截面百分位排名（0~1），刻画全市场动量强弱的相对位置。",
    default_params={"window": 20},
))
def cross_sectional_momentum(returns: pd.DataFrame, window: int = 20) -> pd.DataFrame:
    """横截面动量：滚动累计收益 → 逐日横截面百分位排名。

    参数：returns 为日收益率 DataFrame（index=date, columns=symbol）。
    返回：同形状的百分位排名（0~1）。
    """
    cum = returns.rolling(window).sum()
    return cum.rank(pct=True, axis=1)


@register_factor(FactorMeta(
    name="vol_adjusted_momentum",
    label="波动率调整动量",
    category="动量",
    status="training",
    input_kind="ohlcv_panel",       # 需 high/low/close 面板算 ATR
    dataset="daily",
    description="滚动累计收益 / ATR，用波动率标准化动量（高波动标的动量打折），ATR→0 时 ε 兜底防除零。",
    default_params={"window": 20, "atr_window": 20},
))
def vol_adjusted_momentum(returns: pd.DataFrame, high: pd.DataFrame, low: pd.DataFrame,
                          close: pd.DataFrame, window: int = 20,
                          atr_window: int = 20) -> pd.DataFrame:
    """波动率调整动量 = 滚动累计收益 / ATR。

    ATR 用 (high-low).rolling(atr_window).mean() 近似（显式，免引 ta-lib 黑盒）；
    ATR→0 时以 ε 兜底防除零产生 Inf。
    """
    atr = (high - low).rolling(atr_window).mean()
    atr_safe = atr.where(atr > 1e-9, 1e-9)
    return (returns.rolling(window).sum()) / atr_safe


def hurst_exponent(series: pd.Series, max_k: int = 50) -> float:
    """R/S 重标极差法估计赫斯顿指数 H。

    对每个 lag k：把序列均分为长 k 的块，计算每块的 R（均值偏离累计极差）/ S（标准差），
    取所有块 R/S 的均值；最后对 (log k, log R/S) 线性回归，斜率即 H。
    H>0.5 持续、H=0.5 随机游走、H<0.5 均值回复。
    """
    arr = np.asarray(series.dropna(), dtype=float)
    n = len(arr)
    if n < 20:
        return float("nan")
    ks = np.arange(2, min(max_k, n // 2))
    rs_values = []
    for k in ks:
        usable = (n // k) * k
        chunks = arr[:usable].reshape(-1, k)
        mean = chunks.mean(axis=1, keepdims=True)
        dev = np.cumsum(chunks - mean, axis=1)
        r = dev.max(axis=1) - dev.min(axis=1)
        s = chunks.std(axis=1, ddof=1)
        valid = s > 0
        if valid.any():
            rs_values.append((r[valid] / s[valid]).mean())
    if len(rs_values) < 2:
        return float("nan")
    log_k = np.log(ks[: len(rs_values)])
    log_rs = np.log(rs_values)
    slope, _ = np.polyfit(log_k, log_rs, 1)
    return float(slope)
