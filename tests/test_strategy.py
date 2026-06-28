"""策略插件系统单元测试

覆盖范围：
- BaseStrategy 抽象基类的参数注入契约
- StrategyContext 运行时上下文
"""
import numpy as np
import pandas as pd
import pytest


class TestBaseStrategy:
    """测试 BaseStrategy 抽象基类的参数注入契约"""

    def test_params_injected_and_readable(self):
        """__init__ 注入 params，策略内可显式读取"""
        from strategies.base import BaseStrategy, StrategyContext
        from pydantic import BaseModel, Field

        class StubParams(BaseModel):
            period: int = Field(10, ge=1, le=100)

        class StubStrategy(BaseStrategy):
            name = "stub"
            label = "测试策略"
            universe = []
            params_model = StubParams

            def fit(self, price_data, macro_data=None):
                pass

            def generate_target_weights(self, price_data, ctx):
                return []

        s = StubStrategy(universe=["600000.SH"], params=StubParams(period=20))
        assert s.universe == ["600000.SH"]
        assert s.params.period == 20

    def test_strategy_context_defaults(self):
        """StrategyContext 默认值"""
        from strategies.base import StrategyContext

        ctx = StrategyContext(timestamp=pd.Timestamp("2023-01-01"))
        assert ctx.current_weights == {}
        assert ctx.cash == 0.0
        assert ctx.aum == 0.0
