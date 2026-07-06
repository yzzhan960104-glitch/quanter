"""因子挖掘模块：技术指标与宏观因子融合

职责：
1. 技术指标计算（移动平均线、VPT 等）
2. 宏观基本面因子处理
3. 多周期信号融合
4. 纯向量化实现（无 for 循环）
5. HMM 宏观状态识别（混频数据对齐、概率预测）
6. HMM 状态映射与迟滞滤波（防范高频换手）
"""

from .base import register_factor, FactorMeta, FactorLoader
from .technical import moving_average_cross, volume_price_trend, rsi, macd
from .macro import macro_anchor_signal, cpi_inflation_signal, social_financing_signal
from .fusion import (
    signal_fusion, multi_signal_fusion, signal_filter,
    TargetWeightSignal, AssetWeightConfig, HMMStateMapper, SignalDirection
)
from .hmm_macro import MacroRegimeHMM, test_hmm_macro_module

__all__ = [
    "moving_average_cross",
    "volume_price_trend",
    "rsi",
    "macd",
    "macro_anchor_signal",
    "cpi_inflation_signal",
    "social_financing_signal",
    "signal_fusion",
    "multi_signal_fusion",
    "signal_filter",
    "TargetWeightSignal",
    "AssetWeightConfig",
    "HMMStateMapper",
    "SignalDirection",
    "MacroRegimeHMM",
    "test_hmm_macro_module",
    # 因子注册表（层级二）
    "register_factor",
    "FactorMeta",
    "FactorLoader",
]