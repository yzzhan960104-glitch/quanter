"""技术+宏观融合策略（单资产默认策略）

逻辑搬迁自现 server/services/backtest_service.run_single_backtest 步骤 3-5：
MA 双均线 + VPT 等权 → 技术信号；与宏观锚点信号按 tech_weight 加权融合。
封装为策略后，service 层只负责取数 + 实例化 + run_portfolio。

参数经 TechMacroFusionParams 声明，前端可动态调节（消除原 service 层硬编码）。
"""
from typing import ClassVar, Dict, List, Optional

import pandas as pd
from pydantic import BaseModel, Field

from factors.fusion import TargetWeightSignal, SignalDirection, signal_fusion
from factors.macro import macro_anchor_signal
from factors.technical import moving_average_cross, volume_price_trend
from .base import BaseStrategy, StrategyContext


class TechMacroFusionParams(BaseModel):
    """技术+宏观融合策略可调参数（JSON Schema 真相源）"""

    ma_short: int = Field(
        5, ge=1, le=60,
        description="短均线周期（SMA，天）",
        json_schema_extra={"ui": {"control": "slider", "group": "均线", "step": 1}},
    )
    ma_long: int = Field(
        20, ge=5, le=250,
        description="长均线周期（SMA，天）",
        json_schema_extra={"ui": {"control": "slider", "group": "均线"}},
    )
    vpt_window: int = Field(
        20, ge=5, le=120,
        description="量价趋势(VPT)窗口（天）",
        json_schema_extra={"ui": {"control": "slider", "group": "量价"}},
    )
    macro_threshold: float = Field(
        0.02, ge=0.0, le=0.2,
        description="宏观锚点阈值（M2 环比增速）",
        json_schema_extra={"ui": {"control": "slider", "group": "宏观", "step": 0.005}},
    )
    macro_window: int = Field(
        3, ge=1, le=12,
        description="宏观连续超阈值期数",
        json_schema_extra={"ui": {"control": "slider", "group": "宏观"}},
    )
    tech_weight: float = Field(
        0.7, ge=0.0, le=1.0,
        description="技术信号融合权重（宏观权重 = 1 − tech_weight）",
        json_schema_extra={"ui": {"control": "slider", "group": "融合", "step": 0.05}},
    )


class TechMacroFusionStrategy(BaseStrategy):
    """技术+宏观融合策略（单资产默认）"""

    name: ClassVar[str] = "tech_macro_fusion"
    label: ClassVar[str] = "技术+宏观融合"
    params_model: ClassVar[type[BaseModel]] = TechMacroFusionParams
    # 层级三·拓扑白盒：技术(MA+VPT)+宏观锚点 加权融合
    composition: ClassVar[dict] = {
        "factors": ["moving_average_cross", "volume_price_trend", "macro_anchor_signal"],
        "datasets": ["daily", "macro"],
    }
    rhythm: ClassVar[str] = "日频"
    capital_allocation: ClassVar[str] = "技术(MA+VPT)与宏观锚点按 tech_weight 加权融合 → 单标的 0~1 权重"

    def __init__(self, universe: List[str], params: Optional[TechMacroFusionParams] = None):
        super().__init__(universe, params or TechMacroFusionParams())
        if len(self.universe) != 1:
            raise ValueError(
                f"TechMacroFusionStrategy 仅支持单标的，当前 universe: {self.universe}"
            )
        self._symbol = self.universe[0]
        self._macro_df: Optional[pd.DataFrame] = None

    def fit(
        self,
        price_data: Dict[str, pd.DataFrame],
        macro_data: Optional[pd.DataFrame] = None,
    ) -> None:
        """存储宏观数据供 generate 使用（无训练）"""
        self._macro_df = macro_data

    def generate_target_weights(
        self,
        price_data: Dict[str, pd.DataFrame],
        ctx: StrategyContext,
    ) -> List[TargetWeightSignal]:
        """MA+VPT 技术信号 与 宏观锚点信号 融合 → 目标权重信号"""
        p = self.params
        df = price_data[self._symbol]

        # 技术信号：双均线 + VPT 等权（与原 service 步骤 3 一致）
        ma_signal = moving_average_cross(df, short_window=p.ma_short, long_window=p.ma_long)
        vpt_signal = volume_price_trend(df, window=p.vpt_window)
        tech_signal = (ma_signal + vpt_signal) / 2

        # 宏观融合：有 macro_df 则融合，失败/缺失则退化为纯技术（保留原 service 容错）
        try:
            if self._macro_df is not None and not self._macro_df.empty:
                macro_signal = macro_anchor_signal(
                    self._macro_df,
                    indicator="m2",
                    threshold=p.macro_threshold,
                    window=p.macro_window,
                )
                aligned_index = tech_signal.index.intersection(macro_signal.index)
                if len(aligned_index) > 0:
                    fused = signal_fusion(
                        tech_signal.loc[aligned_index],
                        macro_signal.loc[aligned_index],
                        weights={"tech": p.tech_weight, "macro": 1.0 - p.tech_weight},
                    )
                else:
                    fused = tech_signal.clip(0.0, 1.0)
            else:
                fused = tech_signal.clip(0.0, 1.0)
        except (ValueError, KeyError):
            # 宏观信号计算失败时退化为纯技术信号（防范异常中断回测）
            fused = tech_signal.clip(0.0, 1.0)

        return [
            TargetWeightSignal(
                timestamp=ts,
                weights={self._symbol: float(w)},
                directions={self._symbol: SignalDirection.BUY},
            )
            for ts, w in fused.items()
        ]
