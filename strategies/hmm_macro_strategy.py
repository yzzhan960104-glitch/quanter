"""HMM 宏观状态识别 + 迟滞调仓策略

逻辑搬迁自现 server/services/portfolio_service.run_portfolio_backtest 步骤 2-5：
对齐宏观数据 → 训练 HMM → 预测状态概率 → mapper 映射为目标权重信号。

参数分层：
- HmmMacroParams（策略级，schema 下发）：covariance_type / n_iter / release_lag / max_fill_days
- 结构性配置（请求级 ctor 直传，不进 schema）：n_hmm_states / state_weights / buffer_threshold
  原因：state_weights 是 State_N × symbols 矩阵，行列依赖 n_hmm_states 与 universe，无法静态 schema 化。
"""
from typing import ClassVar, Dict, List, Literal, Optional

import pandas as pd
from pydantic import BaseModel, Field

from factors.fusion import (
    HMMStateMapper, AssetWeightConfig, TargetWeightSignal,
)
from factors.hmm_macro import MacroRegimeHMM
from .base import BaseStrategy, StrategyContext


class HmmMacroParams(BaseModel):
    """HMM 宏观策略可调参数（JSON Schema 真相源，仅标量训练参数）"""

    # Why Literal（而非 str）：spec §5.3/§4.2 规定协方差类型为四值枚举。用 str 会让
    # 非法值（如 "banana"）绕过请求层 Pydantic 校验，延迟到 hmm.fit 内部才报 500；
    # 改 Literal 后非法值在参数解析阶段即抛 ValidationError（路由层转 422），fail-fast。
    covariance_type: Literal["diag", "full", "tied", "spherical"] = Field(
        "diag",
        description="HMM 协方差类型（diag 稳定 / full 灵活易过拟合 / tied / spherical）",
        json_schema_extra={"ui": {
            "control": "select", "group": "HMM训练",
            "options": [
                {"label": "对角(diag)", "value": "diag"},
                {"label": "完全(full)", "value": "full"},
                {"label": "绑定(tied)", "value": "tied"},
                {"label": "球面(spherical)", "value": "spherical"},
            ],
        }},
    )
    n_iter: int = Field(
        100, ge=10, le=500,
        description="EM 算法最大迭代次数",
        json_schema_extra={"ui": {"control": "input-number", "group": "HMM训练"}},
    )
    release_lag: int = Field(
        5, ge=0, le=30,
        description="宏观数据发布滞后（天，防未来函数）",
        json_schema_extra={"ui": {"control": "slider", "group": "数据对齐"}},
    )
    max_fill_days: int = Field(
        90, ge=10, le=365,
        description="宏观前向填充最大天数（超此标记 NaN）",
        json_schema_extra={"ui": {"control": "slider", "group": "数据对齐"}},
    )


class HMMMacroStrategy(BaseStrategy):
    """HMM 宏观状态 → ETF 权重（含迟滞滤波）"""

    name: ClassVar[str] = "hmm_macro"
    label: ClassVar[str] = "HMM宏观状态"
    params_model: ClassVar[type[BaseModel]] = HmmMacroParams
    # 层级三·拓扑白盒：HMM 状态识别 → 状态权重矩阵映射多标的
    composition: ClassVar[dict] = {
        "factors": ["MacroRegimeHMM", "HMMStateMapper"],
        "datasets": ["daily", "macro"],
    }
    rhythm: ClassVar[str] = "日频（迟滞滤波降换手）"
    capital_allocation: ClassVar[str] = "HMM 状态概率 → state_weights 矩阵映射多标的权重，buffer_threshold 迟滞"

    def __init__(
        self,
        universe: List[str],
        params: Optional[HmmMacroParams] = None,
        n_hmm_states: int = 3,
        state_weights: Optional[Dict[str, Dict[str, float]]] = None,
        buffer_threshold: float = 0.05,
    ):
        """
        参数：
            universe: 标的池
            params: HmmMacroParams（策略级训练参数，schema 下发）
            n_hmm_states: HMM 状态数（结构性，请求级，驱动 state_weights 矩阵行数）
            state_weights: 各状态基准权重矩阵（结构性，请求级）
            buffer_threshold: 迟滞阈值（结构性，请求级）
        """
        super().__init__(universe, params or HmmMacroParams())
        self._n_states = n_hmm_states
        self._state_weights = state_weights or {}
        self._buffer = buffer_threshold

        # HMM 模型：n_components/n_states 由结构配置定，训练参数取自 params
        # random_state 不下发（=42 保可复现），由服务层统一管控
        self._hmm = MacroRegimeHMM(
            n_components=n_hmm_states,
            covariance_type=self.params.covariance_type,
            n_iter=self.params.n_iter,
            random_state=42,
        )
        self._mapper: Optional[HMMStateMapper] = None
        self._prob_matrix: Optional[pd.DataFrame] = None

    def fit(
        self,
        price_data: Dict[str, pd.DataFrame],
        macro_data: Optional[pd.DataFrame] = None,
    ) -> None:
        """对齐 + 训练 HMM + 预测概率矩阵"""
        if macro_data is None or macro_data.empty:
            raise ValueError("HMM 宏观策略需要宏观数据（macro_data）")

        base = self.universe[0]
        daily_df = price_data[base][["close"]].rename(columns={"close": f"{base}_close"})
        for s in self.universe[1:]:
            if s in price_data:
                daily_df[f"{s}_close"] = price_data[s]["close"]

        # 对齐宏观数据（严格防未来函数）；release_lag/max_fill_days 取自 params（消除原硬编码）
        aligned = self._hmm.align_macro_data(
            daily_df.dropna(),
            macro_data,
            release_lag=self.params.release_lag,
            max_fill_days=self.params.max_fill_days,
        )
        feature_cols = [c for c in aligned.columns if not c.endswith("_freshness")]
        self._hmm.fit(aligned, feature_columns=feature_cols, drop_na=True)
        self._prob_matrix, _ = self._hmm.predict(aligned, drop_na=False)

        # 初始化 mapper（每次 fit 重置，防跨请求状态污染）
        assets = [AssetWeightConfig(symbol=s, base_name=s) for s in self.universe]
        self._mapper = HMMStateMapper(
            states=self._n_states,
            assets=assets,
            state_weights=self._state_weights,
            buffer_threshold=self._buffer,
        )

    def generate_target_weights(
        self,
        price_data: Dict[str, pd.DataFrame],
        ctx: StrategyContext,
    ) -> List[TargetWeightSignal]:
        """概率矩阵 → 目标权重信号（迟滞滤波）"""
        if self._mapper is None or self._prob_matrix is None:
            raise RuntimeError("策略未训练，请先调用 fit()")
        self._mapper.reset_weights()
        return self._mapper.map_states_to_weights(self._prob_matrix)
