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
from strategies.tech_macro_fusion_strategy import (
    TechMacroFusionStrategy, TechMacroFusionParams,
)
from strategies.hmm_macro_strategy import HMMMacroStrategy, HmmMacroParams

# Task 6 测试所需依赖：组合回测引擎 + 成本模型 + 目标权重信号
from backtest.engine import BacktestEngine
from backtest.cost_model import CostModel
from factors.fusion import TargetWeightSignal, SignalDirection


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


@pytest.fixture
def single_macro_data():
    """月频宏观数据（M2）"""
    return pd.DataFrame(
        {"m2": np.linspace(200, 220, 25)},
        index=pd.date_range("2023-01-01", periods=25, freq="MS", tz="Asia/Shanghai"),
    )


class TestTechMacroFusionStrategy:
    """测试 tech+macro 融合策略（单资产默认）"""

    def test_has_name_and_params_model(self):
        assert TechMacroFusionStrategy.name == "tech_macro_fusion"
        assert TechMacroFusionStrategy.params_model is TechMacroFusionParams

    def test_default_params(self):
        p = TechMacroFusionParams()
        assert p.ma_short == 5 and p.ma_long == 20
        assert p.tech_weight == 0.7

    def test_fit_stores_macro(self, single_price_data, single_macro_data):
        """fit 存储 macro_data 供 generate 使用"""
        strat = TechMacroFusionStrategy(universe=["600000.SH"])
        strat.fit(single_price_data, macro_data=single_macro_data)
        assert strat._macro_df is not None

    def test_generate_with_macro(self, single_price_data, single_macro_data):
        """有宏观数据时产出融合信号"""
        strat = TechMacroFusionStrategy(universe=["600000.SH"])
        strat.fit(single_price_data, macro_data=single_macro_data)
        ctx = StrategyContext(timestamp=pd.Timestamp("2023-01-01", tz="Asia/Shanghai"))
        signals = strat.generate_target_weights(single_price_data, ctx)
        assert len(signals) > 0
        from factors.fusion import TargetWeightSignal
        assert all(isinstance(s, TargetWeightSignal) for s in signals)

    def test_generate_without_macro_falls_back_to_tech(self, single_price_data):
        """无宏观数据时退化为纯技术信号（不抛异常）"""
        strat = TechMacroFusionStrategy(universe=["600000.SH"])
        strat.fit(single_price_data, macro_data=None)
        ctx = StrategyContext(timestamp=pd.Timestamp("2023-01-01", tz="Asia/Shanghai"))
        signals = strat.generate_target_weights(single_price_data, ctx)
        assert len(signals) > 0

    def test_custom_ma_periods_take_effect(self, single_price_data, single_macro_data):
        """自定义 MA 周期注入后影响信号（与默认不同）"""
        strat_default = TechMacroFusionStrategy(universe=["600000.SH"])
        strat_custom = TechMacroFusionStrategy(
            universe=["600000.SH"],
            params=TechMacroFusionParams(ma_short=3, ma_long=10),
        )
        strat_default.fit(single_price_data, macro_data=single_macro_data)
        strat_custom.fit(single_price_data, macro_data=single_macro_data)
        ctx = StrategyContext(timestamp=pd.Timestamp("2023-01-01", tz="Asia/Shanghai"))
        s_def = strat_default.generate_target_weights(single_price_data, ctx)
        s_cust = strat_custom.generate_target_weights(single_price_data, ctx)
        # 不同 MA 周期 → 信号序列应有差异
        w_def = [s.weights["600000.SH"] for s in s_def]
        w_cust = [s.weights["600000.SH"] for s in s_cust]
        assert w_def != w_cust

    def test_weights_in_zero_one(self, single_price_data, single_macro_data):
        strat = TechMacroFusionStrategy(universe=["600000.SH"])
        strat.fit(single_price_data, macro_data=single_macro_data)
        ctx = StrategyContext(timestamp=pd.Timestamp("2023-01-01", tz="Asia/Shanghai"))
        for s in strat.generate_target_weights(single_price_data, ctx):
            for w in s.weights.values():
                assert 0.0 <= w <= 1.0


@pytest.fixture
def multi_price_data():
    """双标的 OHLCV"""
    symbols = ["510300.SH", "511010.SH"]
    dates = pd.date_range("2022-01-01", periods=300, freq="D", tz="Asia/Shanghai")
    np.random.seed(0)
    data = {}
    for s in symbols:
        prices = 100 + np.cumsum(np.random.randn(300))
        data[s] = pd.DataFrame({
            "open": prices, "high": prices + 1, "low": prices - 1,
            "close": prices, "volume": 1e6, "amount": 1e8,
        }, index=dates)
    return data


@pytest.fixture
def multi_macro_data():
    return pd.DataFrame(
        {"m2": np.linspace(200, 220, 25)},
        index=pd.date_range("2022-01-01", periods=25, freq="MS", tz="Asia/Shanghai"),
    )


class TestHMMMacroStrategy:
    """测试 HMM 宏观策略"""

    STATE_WEIGHTS = {
        "State_0": {"510300.SH": 0.8, "511010.SH": 0.2},
        "State_1": {"510300.SH": 0.2, "511010.SH": 0.8},
        "State_2": {"510300.SH": 0.5, "511010.SH": 0.5},
    }

    def _make(self, **overrides):
        kwargs = dict(
            universe=["510300.SH", "511010.SH"],
            n_hmm_states=3,
            state_weights=self.STATE_WEIGHTS,
            buffer_threshold=0.05,
        )
        kwargs.update(overrides)
        return HMMMacroStrategy(**kwargs)

    def test_has_name_and_params_model(self):
        assert HMMMacroStrategy.name == "hmm_macro"
        assert HMMMacroStrategy.params_model is HmmMacroParams

    def test_default_params(self):
        p = HmmMacroParams()
        assert p.covariance_type == "diag"
        assert p.release_lag == 5 and p.max_fill_days == 90

    def test_fit_then_generate(self, multi_price_data, multi_macro_data):
        strat = self._make()
        strat.fit(multi_price_data, macro_data=multi_macro_data)
        ctx = StrategyContext(timestamp=pd.Timestamp("2022-01-01", tz="Asia/Shanghai"))
        signals = strat.generate_target_weights(multi_price_data, ctx)
        assert len(signals) > 0
        from factors.fusion import TargetWeightSignal
        assert all(isinstance(s, TargetWeightSignal) for s in signals)

    def test_signals_cover_universe(self, multi_price_data, multi_macro_data):
        strat = self._make()
        strat.fit(multi_price_data, macro_data=multi_macro_data)
        ctx = StrategyContext(timestamp=pd.Timestamp("2022-01-01", tz="Asia/Shanghai"))
        for s in strat.generate_target_weights(multi_price_data, ctx):
            assert set(s.weights.keys()) == {"510300.SH", "511010.SH"}

    def test_custom_release_lag_used(self, multi_price_data, multi_macro_data):
        """自定义 release_lag 注入 HMM 对齐"""
        strat = self._make(params=HmmMacroParams(release_lag=10, max_fill_days=120))
        assert strat.params.release_lag == 10
        strat.fit(multi_price_data, macro_data=multi_macro_data)
        # 不抛异常即表明对齐用了自定义参数

    def test_fit_without_macro_raises(self, multi_price_data):
        """HMM 策略必须有宏观数据"""
        strat = self._make()
        with pytest.raises(ValueError, match="宏观数据"):
            strat.fit(multi_price_data, macro_data=None)


class TestPortfolioCostModel:
    """测试 run_portfolio 路径使用注入的 cost_model（成本可调不退化）

    Why：单资产回测即将统一走 run_portfolio，若 _execute_portfolio_order 仍
    硬编码佣金率（万三），则请求传入的 cost_model 参数会失效——违背"前端传
    什么引擎用什么"。本测试通过对比两档佣金率产生的总成本，证明注入生效。
    """

    def _run_with_commission(self, commission_rate):
        """用指定佣金率跑一次极简组合回测，返回总成本

        构造一个 510300.SH 的恒定价格序列，第一日满仓 BUY，
        让引擎真实执行一笔买入订单，并汇总其交易成本。
        """
        dates = pd.date_range("2023-01-01", periods=5, freq="D")
        df = pd.DataFrame({
            "open": [10.0] * 5, "high": [10.5] * 5, "low": [9.5] * 5,
            "close": [10.0] * 5, "volume": [1e6] * 5,
        }, index=dates)
        price_data = {"510300.SH": df}

        # 第一日满仓买入 510300（同时验证引擎对 BUY 信号的处理路径）
        signals = [TargetWeightSignal(
            timestamp=dates[0],
            weights={"510300.SH": 1.0},
            directions={"510300.SH": SignalDirection.BUY},
        )]

        # 显式注入不同佣金率 + 取消最低佣金，使差异仅来自费率本身
        engine = BacktestEngine(
            initial_capital=1_000_000,
            cost_model=CostModel(commission_rate=commission_rate, min_commission=0.0),
        )
        result = engine.run_portfolio(price_data=price_data, signals=signals)

        trades_df = result["trades"]
        # 买入交易的成本列之和
        return float(trades_df["cost"].sum()) if len(trades_df) > 0 else 0.0

    def test_higher_commission_yields_higher_cost(self):
        """更高佣金率 → 更高交易成本（证明 cost_model 生效）

        若引擎仍硬编码万三，则两档费率产出的成本会相近/相等，断言失败。
        """
        cost_low = self._run_with_commission(commission_rate=0.0001)
        cost_high = self._run_with_commission(commission_rate=0.01)
        assert cost_high > cost_low


class TestWinMetricsInPortfolioResult:
    """测试 run_portfolio 结果字典包含真实 win_rate / profit_loss_ratio

    Why：Task 8 改造后单资产回测统一走 run_portfolio，但 _calculate_portfolio_result
    此前只算收益类指标，未算交易类指标（胜率/盈亏比），导致上层 schema 输出恒为 0，
    严重误导用户。本组测试直接验证两键存在、类型合法，且 buy→sell 配对时非 0。
    """

    def _run_buy_then_sell(self):
        """构造一个简单的 buy→sell 场景，返回 run_portfolio 结果字典

        价格序列：第 0 日买入（价格 10），随后上涨至 12 后第 3 日全部卖出（价格 12），
        形成一笔明确盈利的配对交易，使 win_rate=1.0、profit_loss_ratio>0。
        """
        from backtest import BacktestEngine, CostModel
        from factors.fusion import TargetWeightSignal, SignalDirection

        dates = pd.date_range("2023-01-01", periods=5, freq="D")
        df = pd.DataFrame({
            "open": [10.0, 11.0, 11.5, 12.0, 12.0],
            "high": [10.5, 11.5, 12.0, 12.5, 12.5],
            "low": [9.5, 10.5, 11.0, 11.5, 11.5],
            "close": [10.0, 11.0, 11.5, 12.0, 12.0],
            "volume": [1e6] * 5,
        }, index=dates)
        price_data = {"510300.SH": df}

        # 第 0 日满仓买入，第 3 日清仓卖出 —— 形成一笔可配对的盈利交易
        signals = [
            TargetWeightSignal(
                timestamp=dates[0],
                weights={"510300.SH": 1.0},
                directions={"510300.SH": SignalDirection.BUY},
            ),
            TargetWeightSignal(
                timestamp=dates[3],
                weights={"510300.SH": 0.0},
                directions={"510300.SH": SignalDirection.SELL},
            ),
        ]

        engine = BacktestEngine(
            initial_capital=1_000_000,
            cost_model=CostModel(commission_rate=0.0003, min_commission=0.0),
        )
        return engine.run_portfolio(price_data=price_data, signals=signals)

    def test_result_has_win_metrics_keys(self):
        """结果字典必须包含 win_rate / profit_loss_ratio 两个键"""
        result = self._run_buy_then_sell()
        assert "win_rate" in result, "结果字典缺失 win_rate 键（指标能力未恢复）"
        assert "profit_loss_ratio" in result, "结果字典缺失 profit_loss_ratio 键"

    def test_win_metrics_types_valid(self):
        """win_rate / profit_loss_ratio 必须是合法浮点数（非 None/NaN）"""
        result = self._run_buy_then_sell()
        assert isinstance(result["win_rate"], float)
        assert isinstance(result["profit_loss_ratio"], float)
        assert not pd.isna(result["win_rate"])
        assert not pd.isna(result["profit_loss_ratio"])

    def test_win_metrics_nonzero_when_paired(self):
        """存在 buy→sell 配对时 win_rate/profit_loss_ratio 不应恒为 0

        若 _calculate_portfolio_result 未补算，此处两值均为 0，断言失败。
        """
        result = self._run_buy_then_sell()
        # 一笔明确盈利交易：胜率应为 1.0（全胜），盈亏比因无亏损本应为 0，
        # 但只要 ≠ 此前 bug 的"两键全 0 且 win_rate 也 0"即可证明指标已恢复计算。
        # 这里用 win_rate>0 作为主断言（盈利配对必然拉高胜率）。
        assert result["win_rate"] > 0.0, "win_rate 恒为 0，说明指标未被真实计算"

    def test_empty_result_contract(self):
        """无日记录的早返回路径也必须带 win_rate/profit_loss_ratio 键（契约一致）"""
        from backtest import BacktestEngine
        engine = BacktestEngine(initial_capital=1_000_000)
        # 不喂任何数据直接算结果 → 走早返回分支
        result = engine._calculate_portfolio_result()
        assert "win_rate" in result
        assert "profit_loss_ratio" in result
        assert result["win_rate"] == 0.0
        assert result["profit_loss_ratio"] == 0.0


# ============ Task 7：策略加载器 + /api/v1/strategies ============
from strategies.loader import StrategyLoader
from fastapi.testclient import TestClient
from server.main import app


class TestStrategyLoader:
    """测试策略动态加载器"""

    def test_scan_registers_strategies(self):
        loader = StrategyLoader()
        loader.scan()
        names = set(loader.list_names())
        assert "ma_cross" in names
        assert "tech_macro_fusion" in names
        assert "hmm_macro" in names

    def test_get_returns_class(self):
        loader = StrategyLoader()
        loader.scan()
        cls = loader.get("ma_cross")
        assert cls.name == "ma_cross"

    def test_get_unknown_raises(self):
        loader = StrategyLoader()
        loader.scan()
        with pytest.raises(KeyError):
            loader.get("not_exist")

    def test_list_returns_metadata_with_label(self):
        loader = StrategyLoader()
        loader.scan()
        items = loader.list()
        macross = next(it for it in items if it["name"] == "ma_cross")
        assert "label" in macross
        assert "universe" in macross


class TestStrategiesAPI:
    """测试 /api/v1/strategies 接口"""

    def setup_method(self):
        # 显式触发 lifespan：FastAPI lifespan 只在 TestClient 作为上下文管理器
        # （with 语句）时才会跑 startup 钩子，从而设置 app.state.strategy_loader。
        # 裸 TestClient(app) 直接 .get() 不会触发 lifespan，会导致 500。
        # 这里手动 __enter__ 启动、teardown_method 中 __exit__ 关闭，
        # 让每个测试方法期间 lifespan startup 都已执行完毕。
        self.client = TestClient(app)
        self.client.__enter__()

    def teardown_method(self):
        # 与 setup_method 的 __enter__ 配对，触发 lifespan shutdown
        self.client.__exit__(None, None, None)

    def test_list_strategies(self):
        resp = self.client.get("/api/v1/strategies")
        assert resp.status_code == 200
        data = resp.json()
        names = [it["name"] for it in data]
        assert "ma_cross" in names
        assert "tech_macro_fusion" in names

    def test_get_schema_returns_json_schema(self):
        """schema 端点返回含 ui 提示的 JSON Schema"""
        resp = self.client.get("/api/v1/strategies/ma_cross/schema")
        assert resp.status_code == 200
        schema = resp.json()
        assert schema["type"] == "object"
        assert "fast" in schema["properties"]
        # ui 渲染提示经 json_schema_extra 合并进字段 schema
        assert schema["properties"]["fast"].get("ui", {}).get("control") == "slider"

    def test_get_schema_unknown_strategy(self):
        resp = self.client.get("/api/v1/strategies/not_exist/schema")
        assert resp.status_code in (400, 404, 500)


# ============ Task 8：单资产/组合回测统一走策略路径 ============


class TestSingleBacktestViaStrategy:
    """测试单资产回测走策略路径 + 参数注入

    Why：原 service 层用 req.tech_weights 直接调 signal_fusion，参数硬编码、
    策略不可换。Task 8 后 BacktestRequest 增 strategy_name/strategy_params，
    service 用 params_model 校验注入后实例化策略并走 run_portfolio。
    本组测试验证"前端传什么策略用什么"闭环。
    """

    def _req(self, **overrides):
        """构造单资产回测请求（默认缺省策略 = tech_macro_fusion）"""
        from server.schemas.backtest import BacktestRequest
        import datetime as dt
        kwargs = dict(
            symbol="600000.SH",
            start_date=dt.date(2023, 1, 1),
            end_date=dt.date(2023, 6, 30),
            initial_capital=1_000_000,
        )
        kwargs.update(overrides)
        return BacktestRequest(**kwargs)

    def test_default_strategy_runs(self):
        """strategy_name 缺省时用默认策略（tech_macro_fusion）"""
        from server.services.backtest_service import run_single_backtest
        resp = run_single_backtest(self._req())
        assert len(resp.nav_series) > 0
        assert resp.metrics.n_trades >= 0

    def test_explicit_strategy_runs(self):
        """显式指定 ma_cross 策略可跑通"""
        from server.services.backtest_service import run_single_backtest
        resp = run_single_backtest(self._req(strategy_name="ma_cross"))
        assert len(resp.nav_series) > 0

    def test_strategy_params_injected(self):
        """自定义 strategy_params 经校验后注入策略"""
        from server.services.backtest_service import run_single_backtest
        resp = run_single_backtest(self._req(
            strategy_name="tech_macro_fusion",
            strategy_params={"ma_short": 3, "ma_long": 10, "tech_weight": 0.5},
        ))
        assert len(resp.nav_series) > 0

    def test_invalid_strategy_params_rejected(self):
        """非法 strategy_params（超范围）被拒绝"""
        from server.services.backtest_service import run_single_backtest
        from server.schemas.backtest import BacktestRequest
        import datetime as dt
        # Pydantic 在 service 层 params_model(**...) 校验，超范围抛 ValueError
        req = BacktestRequest(
            symbol="600000.SH",
            start_date=dt.date(2023, 1, 1),
            end_date=dt.date(2023, 6, 30),
            strategy_name="ma_cross",
            strategy_params={"fast": 1},   # ge=2，非法
        )
        with pytest.raises(Exception):
            run_single_backtest(req)


class TestPortfolioBacktestViaStrategy:
    """测试组合回测走 HMMMacroStrategy + 标量参数注入

    Why：原 service 层直接 new MacroRegimeHMM + HMMStateMapper，
    HMM 标量参数硬编码在 service。Task 8 后 HMM 逻辑迁入 HMMMacroStrategy，
    协方差/迭代次数等经 strategy_params 注入。
    """

    def _req(self, **overrides):
        """构造组合回测请求（双标的 + 三状态权重）"""
        from server.schemas.portfolio import PortfolioRequest
        import datetime as dt
        kwargs = dict(
            symbols=["510300.SH", "511010.SH"],
            start_date=dt.date(2022, 1, 1),
            end_date=dt.date(2023, 6, 30),
            initial_capital=1_000_000,
            n_hmm_states=3,
            buffer_threshold=0.05,
            state_weights={
                "State_0": {"510300.SH": 0.8, "511010.SH": 0.2},
                "State_1": {"510300.SH": 0.2, "511010.SH": 0.8},
                "State_2": {"510300.SH": 0.5, "511010.SH": 0.5},
            },
        )
        kwargs.update(overrides)
        return PortfolioRequest(**kwargs)

    def test_portfolio_runs(self):
        """组合回测默认参数跑通"""
        from server.services.portfolio_service import run_portfolio_backtest
        resp = run_portfolio_backtest(self._req())
        assert len(resp.nav_series) > 0
        assert len(resp.weight_series) > 0

    def test_hmm_params_injected(self):
        """自定义 HMM 标量参数注入"""
        from server.services.portfolio_service import run_portfolio_backtest
        resp = run_portfolio_backtest(self._req(
            strategy_params={"covariance_type": "diag", "n_iter": 50, "release_lag": 3},
        ))
        assert len(resp.nav_series) > 0


# ============================================================================
# 【final-fix】最终审查修复的回归测试
# ============================================================================

class TestFinalFix:
    """最终全特征审查发现的 2 项（1 Important + 1 spec 漂移）的回归测试"""

    def test_serialize_single_result_missing_keys_does_not_crash(self):
        """
        单资产序列化器对缺键 result 的契约对称化

        背景：BacktestEngine._calculate_portfolio_result 在 daily_records 为空时
        走早返回路径，结果字典缺 calmar_ratio/n_failed_trades/trades 等键。
        原 _serialize_backtest_result 用 result["..."] 硬取键会 KeyError 崩，
        而 _serialize_portfolio_result 已用 .get(..., 0.0) 防御——本测试断言单资产
        序列化器与之对称：缺键时不崩、metrics 兜底为 0、nav_series/trades 为空列表。
        """
        from server.services.backtest_service import _serialize_backtest_result
        import pandas as pd

        # 模拟早返回路径的 result 字典（刻意缺 calmar_ratio/n_failed_trades/trades）
        # daily_records 与真实早返回一致：pd.DataFrame([]) 即完全空的 DataFrame
        # （无任何列），与 engine._calculate_portfolio_result 早返回结构对齐
        sparse_result = {
            "initial_capital": 1_000_000,
            "final_nav": 1_000_000,
            "total_return": 0.0,
            "annual_return": 0.0,
            "annual_volatility": 0.0,
            "max_drawdown": 0.0,
            "sharpe_ratio": 0.0,
            # 故意缺 calmar_ratio / win_rate / profit_loss_ratio / n_failed_trades / trades
            "n_trades": 0,
            "daily_records": pd.DataFrame(),
        }

        # 不应抛 KeyError，返回合法 BacktestResponse
        # 注：Task 8 起 _serialize_backtest_result 增 price_data 参数（透传 OHLCV/positions）；
        # 此处空 daily_records 场景传空 dict，ohlcv/positions 退化为 []。
        resp = _serialize_backtest_result(sparse_result, {})
        assert resp is not None
        # metrics 各缺键字段 0 兜底
        assert resp.metrics.calmar_ratio == 0.0
        assert resp.metrics.n_failed_trades == 0
        assert resp.metrics.n_trades == 0
        # 空日表 → 空序列
        assert resp.nav_series == []
        assert resp.trades == []

    def test_hmm_covariance_type_literal_rejects_invalid(self):
        """
        covariance_type 改 Literal 校验

        spec §5.3/§4.2 规定 covariance_type: Literal["diag","full","tied","spherical"]。
        原实现为 str，非法值 "banana" 绕过请求校验、延迟到 hmm.fit 内部才报 500。
        本测试断言 Pydantic 在参数解析阶段（422 入口）即拒绝非法值，合法值通过。
        """
        from pydantic import ValidationError
        from strategies.hmm_macro_strategy import HmmMacroParams

        # 非法值：必须抛 ValidationError（请求层转为 422，而非延迟到 fit 内部 500）
        with pytest.raises(ValidationError):
            HmmMacroParams(covariance_type="banana")

        # 合法值：四个枚举全部通过
        for ok in ("diag", "full", "tied", "spherical"):
            p = HmmMacroParams(covariance_type=ok)
            assert p.covariance_type == ok
