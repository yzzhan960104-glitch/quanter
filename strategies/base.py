"""策略抽象基类与运行时上下文

统一契约：所有策略实现 fit（训练）+ generate_target_weights（产出权重信号）。
单资产策略 = 单标的组合的退化（universe 仅 1 个标的）。

参数 schema 机制（本模块核心）：
- 每个策略类用 ClassVar params_model 声明可调参数的 Pydantic 模型（JSON Schema 真相源）
- 实例化时由 service 层注入已校验的 params 对象（显式 DI，禁 **kwargs）
- 前端经 GET /api/v1/strategies/{name}/schema 拿到 model_json_schema() 动态渲染表单
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import ClassVar, Dict, List, Optional

import pandas as pd
from pydantic import BaseModel

from factors.fusion import TargetWeightSignal


@dataclass
class StrategyContext:
    """策略运行时只读快照（防策略误改账户状态）

    策略只能读 ctx，不能持有/修改引擎的可变账户。
    current_weights 由引擎在调用前注入（迟滞滤波/方向判定基准）。

    属性：
        timestamp: 当前信号时间戳
        current_weights: 当前实际权重 {symbol: weight}
        cash: 可用现金
        aum: 账户总市值
    """
    timestamp: pd.Timestamp
    current_weights: Dict[str, float] = field(default_factory=dict)
    cash: float = 0.0
    aum: float = 0.0


class BaseStrategy(ABC):
    """策略抽象基类

    子类必须声明 ClassVar：
        name: 策略唯一标识（StrategyLoader 注册 key、前端下拉框 value）
        label: 中文显示名（前端下拉框 label）
        universe: 标的池（实例化时注入）
        params_model: 策略可调参数的 Pydantic 模型（JSON Schema 真相源）

    约定：fit 后实例进入只读状态；并发回测每请求 new 一个实例。
    """

    name: ClassVar[str]
    label: ClassVar[str]
    universe: ClassVar[List[str]]
    params_model: ClassVar[type[BaseModel]]

    def __init__(self, universe: List[str], params: BaseModel):
        """
        显式依赖注入。

        参数：
            universe: 标的池
            params: 已由 service 层用 self.params_model 校验过的参数对象
                    （禁 **kwargs；策略内部以 self.params.<field> 显式读取）
        """
        self.universe = list(universe)
        self.params = params

    @abstractmethod
    def fit(
        self,
        price_data: Dict[str, pd.DataFrame],
        macro_data: Optional[pd.DataFrame] = None,
    ) -> None:
        """训练阶段（如 HMM 训练）。无状态策略实现为 pass"""

    @abstractmethod
    def generate_target_weights(
        self,
        price_data: Dict[str, pd.DataFrame],
        ctx: StrategyContext,
    ) -> List[TargetWeightSignal]:
        """产出每日目标权重信号序列（复用 TargetWeightSignal，不新造类型）"""
