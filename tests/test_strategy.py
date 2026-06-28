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


from strategies.base import BaseStrategy, StrategyContext
from strategies.ma_cross_strategy import MaCrossStrategy, MaCrossParams


@pytest.fixture
def single_price_data():
    """单标的 100 日 OHLCV"""
    symbol = "600000.SH"
    dates = pd.date_range("2023-01-01", periods=100, freq="D", tz="Asia/Shanghai")
    np.random.seed(42)
    prices = 100 + np.cumsum(np.random.randn(100))
    df = pd.DataFrame({
        "open": prices, "high": prices + 1, "low": prices - 1,
        "close": prices, "volume": 1e6, "amount": 1e8,
    }, index=dates)
    return {symbol: df}


class TestMaCrossStrategy:
    """测试 MACD 双均线策略"""

    def test_is_base_strategy(self):
        strat = MaCrossStrategy(universe=["600000.SH"])
        assert isinstance(strat, BaseStrategy)

    def test_has_name_and_params_model(self):
        assert MaCrossStrategy.name == "ma_cross"
        assert MaCrossStrategy.params_model is MaCrossParams

    def test_default_params_valid(self):
        """默认参数合法"""
        p = MaCrossParams()
        assert p.fast == 12 and p.slow == 26 and p.signal == 9

    def test_params_out_of_range_rejected(self):
        """超范围参数被 Pydantic 拒绝"""
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            MaCrossParams(fast=1)       # ge=2
        with pytest.raises(ValidationError):
            MaCrossParams(slow=500)     # le=120

    def test_custom_params_take_effect(self, single_price_data):
        """自定义参数注入后生效"""
        strat = MaCrossStrategy(universe=["600000.SH"], params=MaCrossParams(fast=5, slow=20, signal=5))
        assert strat.params.fast == 5

    def test_fit_is_noop(self, single_price_data):
        strat = MaCrossStrategy(universe=["600000.SH"])
        strat.fit(single_price_data)  # 不抛异常

    def test_generate_returns_signals(self, single_price_data):
        strat = MaCrossStrategy(universe=["600000.SH"])
        ctx = StrategyContext(timestamp=pd.Timestamp("2023-01-01", tz="Asia/Shanghai"))
        signals = strat.generate_target_weights(single_price_data, ctx)
        assert isinstance(signals, list)
        assert len(signals) > 0
        from factors.fusion import TargetWeightSignal
        assert all(isinstance(s, TargetWeightSignal) for s in signals)

    def test_weights_in_zero_one(self, single_price_data):
        strat = MaCrossStrategy(universe=["600000.SH"])
        ctx = StrategyContext(timestamp=pd.Timestamp("2023-01-01", tz="Asia/Shanghai"))
        signals = strat.generate_target_weights(single_price_data, ctx)
        for s in signals:
            for w in s.weights.values():
                assert 0.0 <= w <= 1.0
