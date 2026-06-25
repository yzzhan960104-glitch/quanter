# -*- coding: utf-8 -*-
"""
多周期信号融合与 HMM 状态映射

职责：
1. 融合技术信号与宏观信号
2. HMM 状态概率到 ETF 权重的映射
3. 迟滞滤波（防范高频换手）

设计原则：
- 纯向量化实现（加权平均）
- 检测 NaN 不盲目填充
- 纯多头策略（信号范围 [0, 1]）
- 迟滞阈值防范无效换手

迟滞滤波核心设计决策：
- weights 字段存储的是"理论最优权重"（概率向量 × 映射矩阵的内积，和恒为 1）
- directions 字段控制哪些标的需要实际调仓（|目标权重 - 当前权重| > buffer_threshold）
- 引擎仅对 direction != HOLD 的标的生成订单，HOLD 标的保持现有仓位不动
- 此设计避免了"HOLD 资产被归一化扭曲"的经典问题

作者：量化交易团队
日期：2026-06-26
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from dataclasses import dataclass
from typing import Dict, Optional, List
from enum import Enum


class SignalDirection(Enum):
    """信号方向枚举"""
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"


# ============ 信号融合函数（保留原有逻辑） ============


def signal_fusion(
    tech_signal: pd.Series,
    macro_signal: pd.Series,
    weights: Optional[Dict[str, float]] = None
) -> pd.Series:
    """
    多周期信号融合逻辑

    融合策略：
    - 短期信号（技术指标）：高权重，快速响应
    - 长期信号（宏观锚点）：低权重，稳定方向

    公式：
    fused_signal = weights['tech'] * tech_signal + weights['macro'] * macro_signal

    防范融合错误：
    - 确保两个信号的时间索引对齐（resample）
    - 检测到任一信号为 NaN 时，融合结果为 NaN（不盲目填充）
    - 确保权重和为 1

    参数：
        tech_signal: 技术信号（index 为 DatetimeIndex）
        macro_signal: 宏观信号（index 为 DatetimeIndex）
        weights: 权重字典（如 {'tech': 0.7, 'macro': 0.3}）

    返回：
        融合后的信号（index 为两个信号索引的交集）

    异常：
        ValueError: 任一信号包含 NaN
        ValueError: 权重和不等于 1
    """
    # 1. 默认权重
    if weights is None:
        weights = {"tech": 0.7, "macro": 0.3}

    # 2. 验证权重和为 1
    weight_sum = sum(weights.values())
    if not np.isclose(weight_sum, 1.0, atol=1e-6):
        raise ValueError(f"权重和不等于 1: {weight_sum}")

    # 3. 对齐索引（防范时间错位）
    aligned_index = tech_signal.index.intersection(macro_signal.index)

    if len(aligned_index) == 0:
        raise ValueError("两个信号的时间索引无交集，无法融合")

    tech_aligned = tech_signal.loc[aligned_index]
    macro_aligned = macro_signal.loc[aligned_index]

    # 4. 检测 NaN（防范异常值传播）
    if tech_aligned.isna().any():
        nan_count = tech_aligned.isna().sum()
        raise ValueError(f"技术信号包含 {nan_count} 个 NaN，请检查数据清洗")

    if macro_aligned.isna().any():
        nan_count = macro_aligned.isna().sum()
        raise ValueError(f"宏观信号包含 {nan_count} 个 NaN，请检查数据清洗")

    # 5. 加权融合（纯向量化）
    fused_signal = (
        weights["tech"] * tech_aligned +
        weights["macro"] * macro_aligned
    )

    # 6. 纯多头：确保信号在 [0, 1] 范围内
    fused_signal = np.clip(fused_signal, 0.0, 1.0)

    return fused_signal


def multi_signal_fusion(
    signals: Dict[str, pd.Series],
    weights: Optional[Dict[str, float]] = None
) -> pd.Series:
    """
    多信号融合（支持 2 个以上信号）

    参数：
        signals: 信号字典（如 {'ma': ma_signal, 'rsi': rsi_signal, 'macro': macro_signal}）
        weights: 权重字典（如 {'ma': 0.4, 'rsi': 0.3, 'macro': 0.3}）

    返回：
        融合后的信号（index 为所有信号索引的交集）
    """
    # 1. 默认权重（均分）
    if weights is None:
        n = len(signals)
        weights = {k: 1.0 / n for k in signals.keys()}

    # 2. 验证权重和为 1
    weight_sum = sum(weights.values())
    if not np.isclose(weight_sum, 1.0, atol=1e-6):
        raise ValueError(f"权重和不等于 1: {weight_sum}")

    # 3. 验证信号与权重匹配
    if set(signals.keys()) != set(weights.keys()):
        raise ValueError("信号与权重不匹配")

    # 4. 对齐索引
    aligned_index = signals[next(iter(signals.keys()))].copy().index
    for signal in signals.values():
        aligned_index = aligned_index.intersection(signal.index)

    if len(aligned_index) == 0:
        raise ValueError("信号的时间索引无交集，无法融合")

    # 5. 检测 NaN
    for name, signal in signals.items():
        signal_aligned = signal.loc[aligned_index]
        if signal_aligned.isna().any():
            nan_count = signal_aligned.isna().sum()
            raise ValueError(f"信号 '{name}' 包含 {nan_count} 个 NaN")

    # 6. 加权融合
    fused_signal = pd.Series(0.0, index=aligned_index)
    for name, signal in signals.items():
        fused_signal += weights[name] * signal.loc[aligned_index]

    # 7. 纯多头：确保信号在 [0, 1] 范围内
    fused_signal = np.clip(fused_signal, 0.0, 1.0)

    return fused_signal


def signal_filter(
    signal: pd.Series,
    min_hold: int = 1,
    threshold: float = 0.3
) -> pd.Series:
    """
    信号过滤（防范频繁交易）

    过滤规则：
    - 信号 > threshold 才买入
    - 信号 < threshold 才卖出
    - 最少持有 min_hold 期（防范频繁交易）

    参数：
        signal: 原始信号
        min_hold: 最少持有期数
        threshold: 信号阈值

    返回：
        过滤后的信号
    """
    # 1. 阈值过滤
    filtered = signal.copy()
    filtered[filtered < threshold] = 0.0
    filtered[filtered > 1 - threshold] = 1.0

    # 2. 最少持有期过滤（防范频繁交易）
    # 检测信号变化
    signal_change = filtered.diff().abs()

    # 持有期计数器
    hold_counter = 0
    last_position = 0.0

    for i in range(len(filtered)):
        if i == 0:
            hold_counter = 0
            last_position = filtered.iloc[i]
        elif signal_change.iloc[i] > 0.5:  # 信号翻转（买卖切换）
            if hold_counter >= min_hold:
                # 允许切换
                hold_counter = 0
                last_position = filtered.iloc[i]
            else:
                # 禁止切换，保持上一期信号
                filtered.iloc[i] = last_position
                hold_counter += 1
        else:
            # 信号未翻转
            if filtered.iloc[i] > 0:
                hold_counter += 1
            else:
                hold_counter = 0
            last_position = filtered.iloc[i]

    return filtered


# ============ HMM 状态映射与迟滞滤波模块 ============


@dataclass
class TargetWeightSignal:
    """
    目标权重信号（数据类）

    回测引擎接收的核心信号结构，包含：
    1. 各标的代码的理论最优目标权重（概率向量 × 映射矩阵的内积，和恒为 1）
    2. 信号时间戳
    3. 各标的的调仓方向（基于迟滞滤波判定）

    核心设计决策（消除归一化扭曲问题）：
    ┌───────────────────────────────────────────────────────────────────┐
    │ weights 字段始终存储"理论最优权重"（和恒为 1），不做迟滞后的归一化 │
    │ directions 字段控制引擎是否对该标的生成调仓订单                    │
    │ 引擎对 direction=HOLD 的标的保持现有仓位不变，不生成订单          │
    │                                                                   │
    │ 此设计的物理含义：                                                 │
    │ - 迟滞滤波容忍小幅偏离（实际仓位权重 vs 理论权重）                 │
    │ - 当偏离超过 buffer_threshold 时，才执行交易以回归理论权重         │
    │ - 实际组合权重与理论权重的偏差，由价格漂移 + 迟滞阈值共同决定      │
    └───────────────────────────────────────────────────────────────────┘

    属性：
        timestamp: 信号时间戳
        weights: 理论最优目标权重字典（symbol -> target_weight），和为 1
        directions: 调仓方向字典（symbol -> SignalDirection）
    """
    timestamp: pd.Timestamp
    weights: Dict[str, float]
    directions: Dict[str, SignalDirection]

    def __post_init__(self):
        """
        初始化后验证

        检查项：
        1. 权重和为 1（允许浮点误差 1e-6）
        2. 权重范围 [0, 1]（纯多头策略，不做空）
        3. 权重与方向的标的集合一致
        """
        # 验证权重和为 1（允许浮点误差）
        weight_sum = sum(self.weights.values())
        if not np.isclose(weight_sum, 1.0, atol=1e-6):
            raise ValueError(f"权重和不等于 1: {weight_sum:.6f}")

        # 验证权重范围 [0, 1]（纯多头：不允许负权重/做空）
        for symbol, weight in self.weights.items():
            if weight < -1e-8 or weight > 1.0 + 1e-8:
                raise ValueError(
                    f"标的 '{symbol}' 的权重 {weight} 超出范围 [0, 1]"
                )

        # 验证标的与方向键一致（防止遗漏或多余的方向标注）
        if set(self.weights.keys()) != set(self.directions.keys()):
            raise ValueError(
                f"权重与方向的标的集合不匹配："
                f"权重含 {set(self.weights.keys())}，"
                f"方向含 {set(self.directions.keys())}"
            )

    def get_rebalance_symbols(self) -> List[str]:
        """
        获取需要调仓的标的代码列表

        返回：
            方向不为 HOLD 的标的代码列表
        """
        return [s for s, d in self.directions.items() if d != SignalDirection.HOLD]

    def is_hold_only(self) -> bool:
        """
        判断是否为纯持有状态（无调仓）

        返回：
            True = 所有标的均为 HOLD，不需要任何交易
        """
        return len(self.get_rebalance_symbols()) == 0

    def get_rebalance_weights(self) -> Dict[str, float]:
        """
        获取需要调仓的标的及其目标权重

        返回：
            仅包含需要调仓标的的权重子字典
        """
        rebalance_symbols = self.get_rebalance_symbols()
        return {s: self.weights[s] for s in rebalance_symbols}


@dataclass
class AssetWeightConfig:
    """
    单一资产权重配置

    属性：
        symbol: ETF 代码（如 "510300.SH" 沪深 300 ETF）
        base_name: 基准名称（如 "宽基权益"）
    """
    symbol: str
    base_name: str


class HMMStateMapper:
    """
    HMM 状态概率到 ETF 权重的映射器

    核心功能：
    1. 状态映射：将 HMM 概率向量映射为 ETF 基准权重组合
       - 理论权重 = 概率向量 · 基准权重矩阵（矩阵向量内积）
    2. 迟滞滤波：防范高频换手（仅当 |目标权重 - 当前权重| > 阈值时才调仓）
    3. 方向判定：BUY（增仓）/ SELL（减仓）/ HOLD（保持不动）

    迟滞滤波的物理含义：
    - HMM 概率每日微幅抖动是常态，如果每次都调仓，换手率会爆炸
    - buffer_threshold 定义了"容忍区间"：只有偏离超过此阈值才执行交易
    - 典型值 5%（0.05）：若某资产理论权重从 30% 变为 33%，不调仓；
      变为 36%，才触发调仓

    示例：
        >>> mapper = HMMStateMapper(
        ...     states=3,
        ...     assets=[
        ...         AssetWeightConfig("510300.SH", "宽基权益"),
        ...         AssetWeightConfig("511010.SH", "国债"),
        ...     ],
        ...     state_weights={
        ...         "State_0": {"510300.SH": 0.8, "511010.SH": 0.2},  # 扩张期
        ...         "State_1": {"510300.SH": 0.2, "511010.SH": 0.8},  # 衰退期
        ...         "State_2": {"510300.SH": 0.5, "511010.SH": 0.5},  # 平稳期
        ...     },
        ...     buffer_threshold=0.05,
        ... )
        >>> # 单日调用（与事件驱动引擎对接）
        >>> signal = mapper.map_single_day(
        ...     state_probabilities={"State_0": 0.1, "State_1": 0.7, "State_2": 0.2},
        ...     current_weights={"510300.SH": 0.3, "511010.SH": 0.7},
        ... )
    """

    def __init__(
        self,
        states: int,
        assets: List[AssetWeightConfig],
        state_weights: Dict[str, Dict[str, float]],
        buffer_threshold: float = 0.05,
    ):
        """
        初始化 HMM 状态映射器

        参数：
            states: HMM 状态数量（通常为 3：扩张/衰退/平稳）
            assets: ETF 资产配置列表
            state_weights: 各状态的基准权重配置
                格式：{ "State_0": {symbol: weight, ...}, ... }
                每个状态的权重和必须为 1
            buffer_threshold: 迟滞阈值（默认 5%）
                低于此值不调仓，防范高频换手

        注意：
            - state_weights 中每个状态的权重和必须为 1
            - buffer_threshold 过大会导致组合偏离理论权重过远
            - buffer_threshold 过小会导致换手率过高（失去迟滞保护效果）
        """
        self.states = states
        self.assets = assets
        self.state_weights = state_weights
        self.buffer_threshold = buffer_threshold

        # 当前实际权重（用于迟滞滤波比较基准）
        # 初始化为全 0（空仓状态），首次调用时所有资产都会触发调仓
        self.current_weights: Dict[str, float] = {
            asset.symbol: 0.0 for asset in assets
        }

        # 验证配置
        self._validate_config()

    def _validate_config(self) -> None:
        """
        验证配置合法性

        检查项：
        1. 状态数量与配置匹配
        2. 各状态权重和为 1
        3. 各状态覆盖所有配置资产
        4. 资产代码唯一性
        """
        # 1. 验证状态数量
        expected_states = [f"State_{i}" for i in range(self.states)]
        actual_states = list(self.state_weights.keys())
        if set(expected_states) != set(actual_states):
            raise ValueError(
                f"状态配置不匹配：期望 {expected_states}，实际 {actual_states}"
            )

        # 2. 验证各状态权重和为 1
        for state_name, weights in self.state_weights.items():
            weight_sum = sum(weights.values())
            if not np.isclose(weight_sum, 1.0, atol=1e-6):
                raise ValueError(
                    f"状态 '{state_name}' 的权重和不等于 1: {weight_sum}"
                )

        # 3. 验证各状态覆盖所有配置资产
        configured_symbols = {asset.symbol for asset in self.assets}
        for state_name, weights in self.state_weights.items():
            state_symbols = set(weights.keys())
            if state_symbols != configured_symbols:
                raise ValueError(
                    f"状态 '{state_name}' 的资产集合与配置不匹配："
                    f"配置含 {configured_symbols}，状态含 {state_symbols}"
                )

        # 4. 验证资产代码唯一性
        symbol_list = [asset.symbol for asset in self.assets]
        if len(symbol_list) != len(set(symbol_list)):
            raise ValueError("资产代码存在重复")

    def compute_theoretical_weights(
        self,
        state_probabilities: Dict[str, float],
    ) -> Dict[str, float]:
        """
        计算理论最优权重（概率向量 × 映射矩阵的内积）

        公式：
            w[symbol] = Σ (prob[state] × base_weight[state][symbol])
            即：理论权重 = 概率向量 · 基准权重矩阵

        物理含义：
            若 HMM 认为 70% 概率处于"扩张期"（权益高配 80% + 国债低配 20%），
            30% 概率处于"衰退期"（权益低配 20% + 国债高配 80%），
            则理论权重 = 0.7×[0.8, 0.2] + 0.3×[0.2, 0.8] = [0.62, 0.38]

        参数：
            state_probabilities: HMM 状态概率字典
                格式：{ "State_0": 0.1, "State_1": 0.7, "State_2": 0.2 }

        返回：
            理论最优权重字典（symbol -> theoretical_weight）

        异常：
            ValueError: 概率键与状态配置不匹配
            ValueError: 概率和不等于 1
            ValueError: 概率包含负值或 NaN
        """
        # 1. 验证概率键与状态配置匹配
        expected_keys = set(f"State_{i}" for i in range(self.states))
        actual_keys = set(state_probabilities.keys())
        if actual_keys != expected_keys:
            raise ValueError(
                f"概率键与状态配置不匹配：期望 {expected_keys}，实际 {actual_keys}"
            )

        # 2. 验证概率和为 1（防范 HMM 输出异常）
        prob_sum = sum(state_probabilities.values())
        if not np.isclose(prob_sum, 1.0, atol=1e-6):
            raise ValueError(f"概率和不等于 1: {prob_sum}")

        # 3. 验证概率非负且非 NaN
        for state_name, prob in state_probabilities.items():
            if np.isnan(prob):
                raise ValueError(f"状态 '{state_name}' 的概率为 NaN")
            if prob < 0:
                raise ValueError(f"状态 '{state_name}' 的概率为负: {prob}")

        # 4. 计算理论权重：w[symbol] = Σ (prob[state] × base_weight[state][symbol])
        theoretical_weights: Dict[str, float] = {
            asset.symbol: 0.0 for asset in self.assets
        }

        for state_name, prob in state_probabilities.items():
            # 取出该状态下的基准权重字典
            state_asset_weights = self.state_weights[state_name]
            for asset in self.assets:
                theoretical_weights[asset.symbol] += (
                    prob * state_asset_weights[asset.symbol]
                )

        return theoretical_weights

    def _apply_hysteresis_filter(
        self,
        theoretical_weights: Dict[str, float],
        current_weights: Dict[str, float],
    ) -> Dict[str, SignalDirection]:
        """
        迟滞滤波：判定各资产的调仓方向

        核心逻辑：
            对每个资产，比较理论权重与当前权重的绝对差值：
            - |理论权重 - 当前权重| > buffer_threshold → 需要调仓
              - 理论权重 > 当前权重 → BUY（增仓）
              - 理论权重 < 当前权重 → SELL（减仓）
            - |理论权重 - 当前权重| ≤ buffer_threshold → HOLD（保持不动）

        物理含义：
            迟滞滤波的目的是容忍组合权重的微小偏离，避免因 HMM 概率
            每日微幅抖动而频繁换手。只有当偏离足够大（超过阈值）时，
            才执行交易以回归理论权重。

        防御性检查：
            - current_weights 中的 NaN 值会被视为 0.0（空仓）
            - 理论权重与当前权重完全相同时（如初始化阶段），不会误触发调仓

        参数：
            theoretical_weights: 理论最优权重字典
            current_weights: 当前实际权重字典

        返回：
            调仓方向字典（symbol -> SignalDirection）
        """
        directions: Dict[str, SignalDirection] = {}

        for asset in self.assets:
            target = theoretical_weights[asset.symbol]
            # 防御：NaN 权重视为空仓（权重为 0）
            current = current_weights.get(asset.symbol, 0.0)
            if np.isnan(current):
                current = 0.0

            diff = abs(target - current)

            if diff > self.buffer_threshold:
                # 偏离超过阈值，需要调仓
                if target > current:
                    directions[asset.symbol] = SignalDirection.BUY
                else:
                    directions[asset.symbol] = SignalDirection.SELL
            else:
                # 偏离在容忍区间内，保持不动
                directions[asset.symbol] = SignalDirection.HOLD

        return directions

    def map_single_day(
        self,
        state_probabilities: Dict[str, float],
        current_weights: Dict[str, float],
        timestamp: Optional[pd.Timestamp] = None,
    ) -> TargetWeightSignal:
        """
        单日 HMM 概率映射为目标权重信号

        此方法是与事件驱动引擎逐日对接的核心接口。
        HMM 模块在 T 日输出概率字典，本方法将其转化为
        包含理论权重与调仓方向的信号对象。

        数据流：
            HMM 概率字典 → 理论权重计算 → 迟滞滤波 → TargetWeightSignal

        参数：
            state_probabilities: HMM 状态概率字典
                格式：{ "State_0": 0.1, "State_1": 0.7, "State_2": 0.2 }
            current_weights: 当前实际权重字典
                由引擎根据持仓市值计算，格式：{ "510300.SH": 0.3, "511010.SH": 0.7 }
            timestamp: 信号时间戳（可选，默认为当前时间）

        返回：
            TargetWeightSignal 实例
            - weights: 理论最优权重（和恒为 1）
            - directions: BUY/SELL/HOLD（由迟滞滤波判定）

        异常：
            ValueError: 概率键与状态配置不匹配
            ValueError: 概率和不等于 1
        """
        # 1. 计算理论最优权重
        theoretical_weights = self.compute_theoretical_weights(state_probabilities)

        # 2. 迟滞滤波，判定调仓方向
        directions = self._apply_hysteresis_filter(
            theoretical_weights, current_weights
        )

        # 3. 更新内部状态（记录最近一次的实际权重，用于批量模式的连续调用）
        self.current_weights = current_weights.copy()

        # 4. 生成信号
        if timestamp is None:
            timestamp = pd.Timestamp.now()

        signal = TargetWeightSignal(
            timestamp=timestamp,
            weights=theoretical_weights,
            directions=directions,
        )

        return signal

    def map_states_to_weights(
        self,
        prob_matrix: pd.DataFrame,
        current_weights: Optional[Dict[str, float]] = None,
    ) -> List[TargetWeightSignal]:
        """
        将 HMM 概率矩阵批量映射为目标权重信号

        映射逻辑：
        1. T 日理论权重 = Σ(概率[i] × 基准权重矩阵[i])
        2. 计算理论权重与当前实际权重的绝对差值
        3. 仅当差值 > buffer_threshold 时标记为 BUY/SELL
        4. 权重始终为理论最优权重（和恒为 1，无需归一化）

        参数：
            prob_matrix: HMM 概率矩阵（n_rows × n_states），每行为各状态概率
            current_weights: 初始当前权重（可选，默认使用内部状态）
                注意：批量模式下，后续日期的当前权重使用前一日的
                理论权重（而非实际持仓权重），这是一种简化近似。
                精确计算应由引擎传入每日实际权重。

        返回：
            目标权重信号列表（每个日期一个信号）

        异常：
            ValueError: 概率矩阵列名不匹配
            ValueError: 概率矩阵行和不为 1
        """
        # 1. 使用传入的当前权重或内部状态
        if current_weights is not None:
            self.current_weights = current_weights.copy()

        # 2. 验证概率矩阵列名
        expected_columns = [f"State_{i}" for i in range(self.states)]
        actual_columns = list(prob_matrix.columns)

        # 兼容 hmm_macro.py 的输出列名格式（"state_0_prob" → "State_0"）
        if actual_columns != expected_columns:
            # 尝试自动映射 hmm_macro 输出格式
            if all(c.startswith("state_") and c.endswith("_prob") for c in actual_columns):
                rename_map = {
                    c: f"State_{c.replace('state_', '').replace('_prob', '')}"
                    for c in actual_columns
                }
                prob_matrix = prob_matrix.rename(columns=rename_map)
                actual_columns = list(prob_matrix.columns)

        if set(actual_columns) != set(expected_columns):
            raise ValueError(
                f"概率矩阵列名不匹配：期望 {expected_columns}，"
                f"实际 {list(prob_matrix.columns)}"
            )

        # 3. 验证概率和为 1（防范归一化错误）
        prob_sums = prob_matrix.sum(axis=1)
        invalid_rows = prob_sums[~np.isclose(prob_sums, 1.0, atol=1e-6)]
        if len(invalid_rows) > 0:
            raise ValueError(
                f"概率矩阵行和不为 1，发现 {len(invalid_rows)} 行异常，"
                f"首行：{invalid_rows.index[0]}，行和：{invalid_rows.iloc[0]:.6f}"
            )

        # 4. 逐日计算目标权重
        signals: List[TargetWeightSignal] = []

        for timestamp, prob_row in prob_matrix.iterrows():
            # 构建单日概率字典
            state_probs = {col: prob_row[col] for col in prob_matrix.columns}

            # 调用单日映射方法（复用逻辑，保证一致性）
            signal = self.map_single_day(
                state_probabilities=state_probs,
                current_weights=self.current_weights,
                timestamp=timestamp,
            )
            signals.append(signal)

            # 更新当前权重为理论权重（批量模式的简化近似）
            # 注意：实际回测中应由引擎传入每日真实权重
            self.current_weights = signal.weights.copy()

        return signals

    def reset_weights(self) -> None:
        """
        重置当前实际权重（重新初始化为全 0，即空仓状态）

        适用场景：
        - 重新开始一轮回测
        - 切换到不同的回测区间
        """
        self.current_weights = {asset.symbol: 0.0 for asset in self.assets}
