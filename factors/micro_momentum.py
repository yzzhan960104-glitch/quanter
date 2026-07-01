"""微观动量爆发 + ATR 波动率 + Risk Parity 头寸。

ATR = mean((high-low).rolling(window))；头寸 ∝ 1/ATR，控单笔回撤。
均线密集发散：短长期 MA 聚拢后突破 → 信号。

【物理意图】宏观 CTA 切到微观 1m/5m 执行层时，关注两类微观结构：
1. 爆发点（micro-momentum breakout）：价格在低波动均线密集区蓄势后，
   短期均线挣脱长期均线 → 趋势启动的早期窗口，是较好的入场时机。
2. 波动率定权（Risk Parity）：用 ATR 度量单位时间内的真实波动幅度，
   按反比分配头寸，使不同标的的单笔风险预算近似恒定，避免高波动品种
   因等额下注而出现单笔回撤失控。
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def breakout_signal(df: pd.DataFrame, fast: int = 5, slow: int = 20) -> pd.Series:
    """均线密集后突破信号（向量量化，零黑盒）。

    物理意图：趋势启动前常见价格“压缩”——短长期均线高度聚拢（蓄势），
    随后短期均线突破长期均线即爆发。本函数用 t-1 时刻的“密集状态”作为
    前置条件（shift(1) 防前视偏差：信号只看过去是否已密集），叠加 t 时刻
    的方向（ma_f > ma_s）合成 0/1 信号，仅在上行突破时给 1。

    参数：
        df:   含 close 列的 K 线 DataFrame（1m/5m 等高频周期）。
        fast: 短期均线窗口（默认 5）。
        slow: 长期均线窗口（默认 20），同时用于密集阈值的标准差估计。
    返回：与 df 等长的 int 序列（0/1，上行突破为 1）。
    """
    ma_f = df["close"].rolling(fast).mean()
    ma_s = df["close"].rolling(slow).mean()
    # 密集判据：短长期均线绝对偏差 < 长周期标准差的 20%——表示价格“压缩”
    dense = (ma_f - ma_s).abs() < (df["close"].rolling(slow).std() * 0.2)
    # 方向：短期均线在长期均线之上（上行）
    cross = (ma_f > ma_s).astype(int)
    # 仅当“前一 bar 已密集”且“当前上行”时给信号；shift(1, fill_value=False) 杜绝前视偏差
    return (dense.shift(1, fill_value=False) & (ma_f > ma_s)).astype(int)


def atr(df: pd.DataFrame, window: int = 14) -> pd.Series:
    """平均真实波幅 ATR（显式向量化实现）。

    ATR = mean((high - low).rolling(window))。
    物理意图：以单位时间内的高低差均值衡量该标的的“原生噪声水平”，
    作为 Risk Parity 头寸的反比分母。这里采用 high-low 近似（1m/5m 微观
    K 线通常 gap 极小，省略传统 TR 的 close 跳空项以保持极简与零幻觉）。

    防除零：滚动均值可能因停牌/涨跌停等场景退化到 0，下游 1/ATR 将爆 Inf。
    故凡 ATR ≤ 1e-9 处统一抬到 1e-9（与 ε 兜底一致），确保头寸数值有限。
    """
    tr = (df["high"] - df["low"]).abs()
    a = tr.rolling(window).mean()
    # 防 Inf 且不掩盖 warm-up NaN：仅对【非 NaN 且 ≤ ε】的值抬到 ε（防 1/ATR 爆 Inf）；
    #   rolling warm-up 期（前 window-1 根）的 NaN 保持 NaN——绝不静默替换成 1e-9，
    #   否则下游会拿到伪 ATR(1e-9) 污染 Risk Parity 头寸与移动止损线(/factors 端点
    #   另有 len<window + pd.isna 守卫兜底)。
    return a.mask(a.notna() & (a <= 1e-9), 1e-9)


def risk_parity_weight(atr_value: float, budget: float, min_atr: float = 1e-9) -> float:
    """Risk Parity 头寸：头寸 ∝ 1/ATR。

    物理意图：在固定风险预算（budget，如 1e6 元·波动单位）下，让每个标的
    的“单位头寸 × 波动率”近似相等——波动越大（ATR 大）下注越少，波动越小
    下注越多，从而把单笔回撤拉平到统一量级，避免高波动品种吞噬组合风险额度。

    参数：
        atr_value: 该标的的 ATR 值（来自 atr()）。
        budget:    风险预算（名义资金 / 风险单位）。
        min_atr:   分母下界，兜底防 0 与负值产生 Inf / 负头寸。
    返回：建议头寸规模（float）。
    """
    a = max(atr_value, min_atr)
    return float(budget / a)   # ∝ 1/ATR
