# -*- coding: utf-8 -*-
"""
组合回测 Pydantic 模型

职责：
1. 定义组合回测请求的校验规则（多标的、HMM 参数、迟滞阈值）
2. 定义组合回测响应的序列化格式（含权重时序）

设计原则：
- state_weights 矩阵校验：每个状态的权重和为 1，且覆盖所有 symbols
- buffer_threshold 范围约束：过大会导致组合偏离理论权重过远
- 与 backtest.py 的共享模型通过 import 复用，不重复定义
"""
from datetime import date
from typing import Any, Dict, List
from pydantic import BaseModel, Field, field_validator, model_validator

from .backtest import MetricsResponse, NavPoint, DrawdownPoint, TradeRecord


# ============ 请求模型 ============

class PortfolioRequest(BaseModel):
    """
    组合回测请求

    校验规则：
    - symbols 不能为空，每个代码长度 ≥ 1
    - start_date 必须早于 end_date
    - initial_capital 必须为正数
    - n_hmm_states 范围 [2, 10]
    - buffer_threshold 范围 (0, 0.5)
    - state_weights 的键必须为 "State_0", "State_1", ... 格式
    - state_weights 每个状态的权重和为 1，且覆盖所有 symbols
    """
    symbols: List[str] = Field(
        ...,
        min_length=1,
        description="ETF 标的代码列表（如 ['510300.SH', '511010.SH']）"
    )
    start_date: date = Field(
        ...,
        description="回测起始日期"
    )
    end_date: date = Field(
        ...,
        description="回测结束日期"
    )
    initial_capital: float = Field(
        default=1_000_000,
        gt=0,
        description="初始资金（必须为正数）"
    )
    n_hmm_states: int = Field(
        default=3,
        ge=2, le=10,
        description="HMM 隐藏状态数量（建议 3：扩张/衰退/平稳）"
    )
    buffer_threshold: float = Field(
        default=0.05,
        gt=0.0, le=0.5,
        description="迟滞阈值（低于此值不调仓，防范高频换手）"
    )
    state_weights: Dict[str, Dict[str, float]] = Field(
        ...,
        description=(
            "各状态的基准权重配置。"
            "格式：{ 'State_0': {symbol: weight}, ... }，"
            "每个状态的权重和必须为 1"
        )
    )
    strategy_params: Dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "HMM 策略标量参数（covariance_type/n_iter/release_lag/max_fill_days），"
            "由 HmmMacroParams 在 service 层校验注入"
        )
    )

    @field_validator("symbols")
    @classmethod
    def validate_symbols(cls, v: List[str]) -> List[str]:
        """验证标的列表非空且无重复"""
        if len(v) == 0:
            raise ValueError("标的列表不能为空")
        # 去重检查
        if len(v) != len(set(v)):
            raise ValueError("标的列表存在重复代码")
        # 每个代码长度检查
        for s in v:
            if len(s.strip()) == 0:
                raise ValueError("标的代码不能为空字符串")
        return v

    @field_validator("state_weights")
    @classmethod
    def validate_state_weights(cls, v: Dict[str, Dict[str, float]]) -> Dict[str, Dict[str, float]]:
        """
        验证状态权重配置合法性

        检查项：
        1. 键格式为 "State_0", "State_1", ...
        2. 每个状态的权重和为 1
        3. 无负权重
        """
        for state_name, weights in v.items():
            # 权重和检查
            weight_sum = sum(weights.values())
            if abs(weight_sum - 1.0) > 1e-6:
                raise ValueError(
                    f"状态 '{state_name}' 的权重和不等于 1: {weight_sum:.6f}"
                )
            # 负权重检查（纯多头策略，不允许做空）
            for symbol, w in weights.items():
                if w < -1e-8:
                    raise ValueError(
                        f"状态 '{state_name}' 中标的 '{symbol}' 的权重为负: {w}"
                    )
        return v

    @model_validator(mode="after")
    def validate_cross_fields(self) -> "PortfolioRequest":
        """
        跨字段联合校验

        检查项：
        1. 起始日期 < 结束日期
        2. state_weights 的状态数量与 n_hmm_states 一致
        3. state_weights 中每个状态的标的集合与 symbols 一致
        """
        # 日期校验
        if self.start_date >= self.end_date:
            raise ValueError("起始日期必须早于结束日期")

        # 状态数量校验
        expected_states = {f"State_{i}" for i in range(self.n_hmm_states)}
        actual_states = set(self.state_weights.keys())
        if expected_states != actual_states:
            raise ValueError(
                f"状态权重键与 n_hmm_states 不匹配："
                f"期望 {sorted(expected_states)}，实际 {sorted(actual_states)}"
            )

        # 标的集合一致性校验
        configured_symbols = set(self.symbols)
        for state_name, weights in self.state_weights.items():
            state_symbols = set(weights.keys())
            if state_symbols != configured_symbols:
                raise ValueError(
                    f"状态 '{state_name}' 的标的集合与 symbols 不匹配："
                    f"配置含 {sorted(configured_symbols)}，"
                    f"状态含 {sorted(state_symbols)}"
                )

        return self


# ============ 响应模型 ============

class WeightPoint(BaseModel):
    """每日权重快照节点（用于前端绘制权重堆叠面积图）"""
    date: str
    weights: Dict[str, float]   # {symbol: weight}


class PortfolioResponse(BaseModel):
    """
    组合回测完整响应

    与单资产响应相比，额外包含 weight_series（每日权重快照）
    """
    metrics: MetricsResponse
    nav_series: List[NavPoint]
    drawdown_series: List[DrawdownPoint]
    weight_series: List[WeightPoint]
    trades: List[TradeRecord]
