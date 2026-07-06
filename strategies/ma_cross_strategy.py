"""MACD 双均线策略（单资产示例，演示 BaseStrategy + params_model + MyTT 用法）

策略逻辑（与 factors/technical.py.macd 的金叉/死叉一致）：
- MACD 金叉（DIF 上穿 DEA）→ 满仓（weight=1.0）
- MACD 死叉（DIF 下穿 DEA）→ 空仓（weight=0.0）
- 持仓状态（DIF>DEA）→ 维持前值

参数全部经 MaCrossParams 声明，前端可通过 JSON Schema 下发动态调节。
"""
from typing import ClassVar, Dict, List, Optional

import pandas as pd
from pydantic import BaseModel, Field

from factors.fusion import TargetWeightSignal, SignalDirection
from factors.mytt import MACD
from .base import BaseStrategy, StrategyContext


class MaCrossParams(BaseModel):
    """MACD 策略可调参数（JSON Schema 真相源）"""

    fast: int = Field(
        12, ge=2, le=60,
        description="MACD 快线周期（EMA）",
        json_schema_extra={"ui": {"control": "slider", "group": "MACD均线", "step": 1}},
    )
    slow: int = Field(
        26, ge=10, le=120,
        description="MACD 慢线周期（EMA）",
        json_schema_extra={"ui": {"control": "slider", "group": "MACD均线"}},
    )
    signal: int = Field(
        9, ge=3, le=30,
        description="MACD 信号线周期（对 DIF 再求 EMA）",
        json_schema_extra={"ui": {"control": "slider", "group": "MACD均线"}},
    )


class MaCrossStrategy(BaseStrategy):
    """单标的 MACD 金叉/死叉策略"""

    name: ClassVar[str] = "ma_cross"
    label: ClassVar[str] = "MACD双均线"
    params_model: ClassVar[type[BaseModel]] = MaCrossParams
    # 层级三·拓扑白盒（factors/datasets 供执行计划图与因子反查引用消费）
    composition: ClassVar[dict] = {"factors": ["MACD"], "datasets": ["daily"]}
    rhythm: ClassVar[str] = "日频"
    capital_allocation: ClassVar[str] = "MACD 金叉满仓、死叉空仓（权重 0↔1 二值切换），单标的"

    def __init__(self, universe: List[str], params: Optional[MaCrossParams] = None):
        # params 缺省用模型默认值；service 层正常会注入请求参数
        super().__init__(universe, params or MaCrossParams())
        if len(self.universe) != 1:
            raise ValueError(f"MaCrossStrategy 仅支持单标的，当前 universe: {self.universe}")
        self._symbol = self.universe[0]

    def fit(
        self,
        price_data: Dict[str, pd.DataFrame],
        macro_data: Optional[pd.DataFrame] = None,
    ) -> None:
        """无状态策略，无需训练"""
        return None

    def generate_target_weights(
        self,
        price_data: Dict[str, pd.DataFrame],
        ctx: StrategyContext,
    ) -> List[TargetWeightSignal]:
        """MACD 金叉/死叉 → 目标权重信号序列"""
        df = price_data[self._symbol]
        dif, dea, _ = MACD(df["close"], self.params.fast, self.params.slow, self.params.signal)

        # 金叉/死叉判定（shift(1) 防前视偏差）
        golden = (dif.shift(1) < dea.shift(1)) & (dif > dea)
        death = (dif.shift(1) > dea.shift(1)) & (dif < dea)

        weight = pd.Series(0.5, index=df.index)   # 默认半仓（中性）
        weight[golden] = 1.0
        weight[death] = 0.0
        # 持仓状态：DIF>DEA 维持前值
        holding = (dif > dea) & ~golden & ~death
        weight[holding] = weight[holding].shift(1)
        weight = weight.ffill().fillna(0.0).clip(0.0, 1.0)

        # direction 设为 BUY（非 HOLD）使引擎纳入调仓评估；
        # 实际买卖由引擎按 delta 符号 + 整手过滤决定
        return [
            TargetWeightSignal(
                timestamp=ts,
                weights={self._symbol: float(w)},
                directions={self._symbol: SignalDirection.BUY},
            )
            for ts, w in weight.items()
        ]
