"""回测引擎模块单元测试

覆盖范围：
- 成本模型（佣金、印花税、滑点）
- 压力测试（涨跌停、流动性枯竭、熔断、黑天鹅）
- 回测引擎（事件驱动）
- 指标计算（收益指标、交易指标、因子归因）
"""
import pytest
import numpy as np
import pandas as pd
from datetime import datetime

from backtest import CostModel, StressTester
from backtest import BacktestEngine, MetricsCalculator


@pytest.fixture
def sample_df():
    """生成示例 OHLCV 数据"""
    dates = pd.date_range("2023-01-01", periods=100, freq="D", tz="Asia/Shanghai")
    np.random.seed(42)

    base_price = 100
    trend = np.linspace(0, 10, 100)
    noise = np.random.randn(100) * 2
    prices = base_price + trend + noise

    df = pd.DataFrame({
        "open": prices + np.random.randn(100) * 0.5,
        "high": prices + np.random.rand(100) * 1,
        "low": prices - np.random.rand(100) * 1,
        "close": prices,
        "volume": np.random.randint(100000, 1000000, 100),
        "amount": prices * np.random.randint(100000, 1000000, 100),
    }, index=dates)

    df["high"] = df[["open", "high", "close"]].max(axis=1)
    df["low"] = df[["open", "low", "close"]].min(axis=1)

    return df


@pytest.fixture
def sample_signal(sample_df):
    """生成示例信号"""
    # 构造简单信号：前半段做多，后半段空仓
    signal = pd.Series(0.5, index=sample_df.index)
    signal.iloc[:50] = 0.8
    signal.iloc[50:] = 0.2

    return signal


class TestCostModel:
    """测试成本模型"""

    @pytest.fixture
    def cost_model(self):
        """初始化成本模型"""
        return CostModel(
            commission_rate=0.0003,
            stamp_duty=0.0005,
            min_commission=5.0,
        )

    def test_calculate_commission_formula(self, cost_model):
        """测试佣金计算公式"""
        amount = 100000
        commission = cost_model.calculate_commission(amount)

        # 佣金 = 成交金额 × 佣金率
        expected = amount * 0.0003
        assert commission == expected

    def test_calculate_commission_min_commission(self, cost_model):
        """测试最低佣金"""
        amount = 10000  # 较小成交金额
        commission = cost_model.calculate_commission(amount)

        # 应满足最低佣金
        assert commission >= 5.0

    def test_calculate_stamp_duty_sell_only(self, cost_model):
        """测试印花税仅收取卖出"""
        amount = 100000

        # 买入无印花税
        stamp_duty_buy = cost_model.calculate_stamp_duty(amount, is_sell=False)
        assert stamp_duty_buy == 0.0

        # 卖出有印花税
        stamp_duty_sell = cost_model.calculate_stamp_duty(amount, is_sell=True)
        assert stamp_duty_sell > 0.0

    def test_calculate_transfer_fee_sh_only(self, cost_model):
        """测试过户费仅上海市场"""
        amount = 100000

        # 上海市场（6 开头）
        transfer_fee_sh = cost_model.calculate_transfer_fee(amount, "600000.SH")
        assert transfer_fee_sh > 0.0

        # 深圳市场（0 开头）
        transfer_fee_sz = cost_model.calculate_transfer_fee(amount, "000001.SZ")
        assert transfer_fee_sz == 0.0

    def test_calculate_slippage_buy_increases_price(self):
        """测试买入滑点提高价格"""
        cost_model = CostModel(slippage_rate=0.001)

        price = 100
        slippage_price = cost_model.calculate_slippage(
            price, 1000, 100000, True, 1.0
        )

        assert slippage_price > price

    def test_calculate_slippage_sell_decreases_price(self):
        """测试卖出滑点降低价格"""
        cost_model = CostModel(slippage_rate=0.001)

        price = 100
        slippage_price = cost_model.calculate_slippage(
            price, 1000, 100000, False, 1.0
        )

        assert slippage_price < price

    def test_calculate_slippage_clamped(self):
        """测试滑点率被限制"""
        cost_model = CostModel(slippage_rate=0.001)

        # 异常大的订单
        slippage_price = cost_model.calculate_slippage(
            100, 1000000, 1000, True, 10.0
        )

        # 滑点不应超过 10%
        assert slippage_price <= 100 * 1.1

    def test_calculate_liquidity_factor_low_volume(self):
        """测试低成交量流动性因子放大"""
        cost_model = CostModel(liquidity_threshold=0.02)

        current_volume = 100
        avg_volume = 100000

        factor = cost_model.calculate_liquidity_factor(current_volume, avg_volume)

        # 流动性因子应该放大
        assert factor > 1.0

    def test_calculate_liquidity_factor_normal_volume(self):
        """测试正常成交量流动性因子正常"""
        cost_model = CostModel(liquidity_threshold=0.02)

        current_volume = 100000
        avg_volume = 100000

        factor = cost_model.calculate_liquidity_factor(current_volume, avg_volume)

        # 流动性因子应该为 1
        assert factor == 1.0

    def test_calculate_total_cost_returns_dict(self, cost_model):
        """测试总成本计算返回字典"""
        cost_info = cost_model.calculate_total_cost(
            price=100,
            volume=1000,
            avg_volume=100000,
            symbol="600000.SH",
            is_sell=False,
        )

        assert isinstance(cost_info, dict)

    def test_calculate_total_cost_includes_all_fields(self, cost_model):
        """测试总成本包含所有字段"""
        cost_info = cost_model.calculate_total_cost(
            price=100,
            volume=1000,
            avg_volume=100000,
            symbol="600000.SH",
            is_sell=False,
        )

        required_fields = ["amount", "commission", "stamp_duty", "transfer_fee", "total_cost"]
        for field in required_fields:
            assert field in cost_info

    def test_calculate_total_cost_sell_higher_than_buy(self, cost_model):
        """测试卖出成本高于买入（印花税）"""
        cost_buy = cost_model.calculate_total_cost(
            price=100, volume=1000, avg_volume=100000, symbol="600000.SH", is_sell=False
        )
        cost_sell = cost_model.calculate_total_cost(
            price=100, volume=1000, avg_volume=100000, symbol="600000.SH", is_sell=True
        )

        # 卖出成本应该更高（印花税）
        assert cost_sell["total_cost"] > cost_buy["total_cost"]


class TestStressTester:
    """测试极端场景模拟器"""

    @pytest.fixture
    def stress_tester(self):
        """初始化压力测试器"""
        return StressTester()

    def test_apply_limit_up_down_returns_dataframe(self, stress_tester, sample_df):
        """测试涨跌停板限制返回 DataFrame"""
        df_stress = stress_tester.apply_limit_up_down(sample_df)

        assert isinstance(df_stress, pd.DataFrame)

    def test_apply_limit_up_clips_price(self, stress_tester, sample_df):
        """测试涨停限制价格"""
        # 构造涨停场景
        df = sample_df.copy()
        df.iloc[0, df.columns.get_loc("close")] = df.iloc[0, df.columns.get_loc("close")] * 1.3

        df_stress = stress_tester.apply_limit_up_down(df, limit_rate=0.10)

        # 价格应被限制
        prev_close = df.iloc[0, df.columns.get_loc("close")]
        current_close = df_stress.iloc[0, df_stress.columns.get_loc("close")]

        assert abs(current_close - prev_close * 1.1) < 1e-6

    def test_apply_limit_down_clips_price(self, stress_tester, sample_df):
        """测试跌停限制价格"""
        # 构造跌停场景
        df = sample_df.copy()
        df.iloc[0, df.columns.get_loc("close")] = df.iloc[0, df.columns.get_loc("close")] * 0.7

        df_stress = stress_tester.apply_limit_up_down(df, limit_rate=0.10)

        # 价格应被限制
        prev_close = df.iloc[0, df.columns.get_loc("close")]
        current_close = df_stress.iloc[0, df_stress.columns.get_loc("close")]

        assert abs(current_close - prev_close * 0.9) < 1e-6

    def test_apply_liquidity_crisis_reduces_volume(self, stress_tester, sample_df):
        """测试流动性枯竭降低成交量"""
        df_stress = stress_tester.apply_liquidity_crisis(sample_df, crisis_ratio=0.1)

        # 部分日期的成交量应该降低
        assert (df_stress["volume"] < sample_df["volume"]).any()

    def test_apply_circuit_breaker_zeroes_volume(self, stress_tester, sample_df):
        """测试熔断零化成交量"""
        # 构造熔断场景（大幅下跌）
        df = sample_df.copy()
        df.iloc[50, df.columns.get_loc("close")] = df.iloc[49, df.columns.get_loc("close")] * 0.9

        df_stress = stress_tester.apply_circuit_breaker(df, threshold=0.05)

        # 熔断日成交量应该为零
        assert df_stress.iloc[50, df_stress.columns.get_loc("volume")] == 0

    def test_apply_black_swan_drops_price(self, stress_tester, sample_df):
        """测试黑天鹅事件降低价格"""
        df_before = sample_df.copy()

        df_stress = stress_tester.apply_black_swan(sample_df, drop_ratio=0.10)

        # 黑天鹅日价格应该下跌
        swan_idx = stress_tester.get_applied_scenarios().index("black_swan")

        # 检查是否存在价格下跌
        price_drop = (df_before["close"] - df_stress["close"]).abs()
        assert price_drop.max() > 0

    def test_get_applied_scenarios_returns_list(self, stress_tester):
        """测试获取已应用场景返回列表"""
        scenarios = stress_tester.get_applied_scenarios()

        assert isinstance(scenarios, list)

    def test_reset_clears_scenarios(self, stress_tester, sample_df):
        """测试重置清空场景记录"""
        stress_tester.apply_limit_up_down(sample_df)
        stress_tester.reset()

        scenarios = stress_tester.get_applied_scenarios()

        assert len(scenarios) == 0

    def test_generate_stress_report_returns_dict(self, stress_tester, sample_df):
        """测试生成压力测试报告返回字典"""
        df_stress = stress_tester.apply_limit_up_down(sample_df)

        report = stress_tester.generate_stress_report(sample_df, df_stress)

        assert isinstance(report, dict)

    def test_generate_stress_report_includes_all_keys(self, stress_tester, sample_df):
        """测试压力测试报告包含所有关键字段"""
        df_stress = stress_tester.apply_limit_up_down(sample_df)

        report = stress_tester.generate_stress_report(sample_df, df_stress)

        required_keys = [
            "scenarios_applied",
            "original_return",
            "stressed_return",
            "return_diff",
            "original_volatility",
            "stressed_volatility",
            "max_drawdown_original",
            "max_drawdown_stressed",
        ]
        for key in required_keys:
            assert key in report


class TestBacktestEngine:
    """测试回测引擎"""

    @pytest.fixture
    def engine(self):
        """初始化回测引擎"""
        return BacktestEngine(
            initial_capital=1_000_000,
            signal_freq="1d"
        )

    def test_initial_state(self, engine):
        """测试初始状态"""
        assert engine.initial_capital == 1_000_000
        assert engine.cash == 1_000_000
        assert engine.position == 0
        assert engine.nav == 1_000_000

    def test_run_returns_dict(self, engine, sample_df, sample_signal):
        """测试回测运行返回字典"""
        result = engine.run(sample_df, sample_signal)

        assert isinstance(result, dict)

    def test_run_includes_all_fields(self, engine, sample_df, sample_signal):
        """测试回测结果包含所有字段"""
        result = engine.run(sample_df, sample_signal)

        required_fields = [
            "initial_capital",
            "final_nav",
            "total_return",
            "annual_return",
            "annual_volatility",
            "max_drawdown",
            "sharpe_ratio",
            "calmar_ratio",
            "win_rate",
            "profit_loss_ratio",
            "n_trades",
            "n_failed_trades",
            "trades",
            "daily_records",
        ]
        for field in required_fields:
            assert field in result

    def test_nav_initialized_correctly(self, engine, sample_df, sample_signal):
        """测试净值初始化正确"""
        result = engine.run(sample_df, sample_signal)

        assert result["initial_capital"] == 1_000_000

    def test_max_drawdown_negative_or_zero(self, engine, sample_df, sample_signal):
        """测试最大回撤为负数或零"""
        result = engine.run(sample_df, sample_signal)

        assert result["max_drawdown"] <= 0

    def test_annual_return_can_be_positive_or_negative(self, engine, sample_df, sample_signal):
        """测试年化收益可正可负"""
        result = engine.run(sample_df, sample_signal)

        assert isinstance(result["annual_return"], (int, float))

    def test_win_rate_between_0_and_1(self, engine, sample_df, sample_signal):
        """测试胜率在 [0, 1] 范围内"""
        result = engine.run(sample_df, sample_signal)

        assert 0 <= result["win_rate"] <= 1

    def test_n_trades_non_negative(self, engine, sample_df, sample_signal):
        """测试交易次数非负"""
        result = engine.run(sample_df, sample_signal)

        assert result["n_trades"] >= 0

    def test_trades_dataframe(self, engine, sample_df, sample_signal):
        """测试交易记录为 DataFrame"""
        result = engine.run(sample_df, sample_signal)

        assert isinstance(result["trades"], pd.DataFrame)

    def test_daily_records_dataframe(self, engine, sample_df, sample_signal):
        """测试每日记录为 DataFrame"""
        result = engine.run(sample_df, sample_signal)

        assert isinstance(result["daily_records"], pd.DataFrame)

    def test_reset_state(self, engine):
        """测试重置状态"""
        engine.cash = 500000
        engine.position = 1000

        engine._reset_state()

        assert engine.cash == 1_000_000
        assert engine.position == 0

    def test_no_trades_with_constant_signal(self, engine, sample_df):
        """测试恒定信号无交易"""
        # 恒定 0 信号
        signal = pd.Series(0.0, index=sample_df.index)

        result = engine.run(sample_df, signal)

        # 应该无交易
        assert result["n_trades"] == 0


class TestMetricsCalculator:
    """测试指标计算器"""

    @pytest.fixture
    def sample_returns(self):
        """生成示例收益率"""
        np.random.seed(42)
        returns = pd.Series(np.random.randn(100) * 0.02)
        returns.index = pd.date_range("2023-01-01", periods=100, freq="D")
        return returns

    def test_calculate_return_metrics_returns_dict(self, sample_returns):
        """测试收益指标计算返回字典"""
        metrics = MetricsCalculator.calculate_return_metrics(sample_returns)

        assert isinstance(metrics, dict)

    def test_calculate_return_metrics_includes_all_keys(self, sample_returns):
        """测试收益指标包含所有字段"""
        metrics = MetricsCalculator.calculate_return_metrics(sample_returns)

        required_keys = [
            "cumulative_return",
            "annual_return",
            "annual_volatility",
            "max_drawdown",
            "max_drawdown_duration",
            "sharpe_ratio",
            "calmar_ratio",
            "sortino_ratio",
        ]
        for key in required_keys:
            assert key in metrics

    def test_max_drawdown_negative_or_zero(self, sample_returns):
        """测试最大回撤为负数或零"""
        metrics = MetricsCalculator.calculate_return_metrics(sample_returns)

        assert metrics["max_drawdown"] <= 0

    def test_sharpe_ratio_can_be_negative(self, sample_returns):
        """测试夏普比率可为负"""
        # 构造负收益序列
        negative_returns = pd.Series(-0.01, index=sample_returns.index)

        metrics = MetricsCalculator.calculate_return_metrics(negative_returns)

        assert metrics["sharpe_ratio"] < 0

    def test_calculate_trade_metrics_returns_dict(self):
        """测试交易指标计算返回字典"""
        # 构造交易记录
        trades = pd.DataFrame({
            "date": pd.date_range("2023-01-01", periods=10),
            "direction": ["buy"] * 5 + ["sell"] * 5,
            "shares": [1000] * 10,
            "price": [100] * 10,
            "amount": [100000] * 10,
            "cost": [30] * 10,
            "symbol": ["600000.SH"] * 10,
        })

        metrics = MetricsCalculator.calculate_trade_metrics(trades)

        assert isinstance(metrics, dict)

    def test_calculate_trade_metrics_n_trades(self):
        """测试交易次数正确"""
        trades = pd.DataFrame({
            "date": pd.date_range("2023-01-01", periods=10),
            "direction": ["buy"] * 5 + ["sell"] * 5,
            "shares": [1000] * 10,
            "price": [100] * 10,
            "amount": [100000] * 10,
            "cost": [30] * 10,
            "symbol": ["600000.SH"] * 10,
        })

        metrics = MetricsCalculator.calculate_trade_metrics(trades)

        assert metrics["n_trades"] == 10

    def test_calculate_factor_attribution_returns_dict(self, sample_df):
        """测试因子归因返回字典"""
        tech_signal = pd.Series(np.random.rand(100), index=sample_df.index)
        macro_signal = pd.Series(np.random.rand(100), index=sample_df.index)
        fused_signal = pd.Series(np.random.rand(100), index=sample_df.index)
        daily_returns = sample_df["close"].pct_change().fillna(0)

        attribution = MetricsCalculator.calculate_factor_attribution(
            tech_signal, macro_signal, fused_signal, daily_returns
        )

        assert isinstance(attribution, dict)

    def test_calculate_factor_attribution_correlation_range(self, sample_df):
        """测试因子归因相关性在 [-1, 1] 范围内"""
        tech_signal = pd.Series(np.random.rand(100), index=sample_df.index)
        macro_signal = pd.Series(np.random.rand(100), index=sample_df.index)
        fused_signal = pd.Series(np.random.rand(100), index=sample_df.index)
        daily_returns = sample_df["close"].pct_change().fillna(0)

        attribution = MetricsCalculator.calculate_factor_attribution(
            tech_signal, macro_signal, fused_signal, daily_returns
        )

        assert -1 <= attribution["tech_correlation"] <= 1
        assert -1 <= attribution["macro_correlation"] <= 1
        assert -1 <= attribution["fused_correlation"] <= 1

    def test_calculate_rolling_metrics_returns_dataframe(self, sample_df):
        """测试滚动指标计算返回 DataFrame"""
        daily_returns = sample_df["close"].pct_change().fillna(0)

        rolling_metrics = MetricsCalculator.calculate_rolling_metrics(daily_returns, window=20)

        assert isinstance(rolling_metrics, pd.DataFrame)

    def test_calculate_rolling_metrics_includes_all_columns(self, sample_df):
        """测试滚动指标包含所有列"""
        daily_returns = sample_df["close"].pct_change().fillna(0)

        rolling_metrics = MetricsCalculator.calculate_rolling_metrics(daily_returns, window=20)

        required_columns = ["rolling_return", "rolling_volatility", "rolling_sharpe", "rolling_drawdown"]
        for col in required_columns:
            assert col in rolling_metrics.columns

    def test_generate_metrics_report_returns_string(self, sample_df):
        """测试指标报告生成返回字符串"""
        daily_returns = sample_df["close"].pct_change().fillna(0)

        return_metrics = MetricsCalculator.calculate_return_metrics(daily_returns)
        trade_metrics = MetricsCalculator.calculate_trade_metrics(pd.DataFrame())

        report = MetricsCalculator.generate_metrics_report(return_metrics, trade_metrics)

        assert isinstance(report, str)

    def test_generate_metrics_report_contains_headers(self, sample_df):
        """测试指标报告包含标题"""
        daily_returns = sample_df["close"].pct_change().fillna(0)

        return_metrics = MetricsCalculator.calculate_return_metrics(daily_returns)
        trade_metrics = MetricsCalculator.calculate_trade_metrics(pd.DataFrame())

        report = MetricsCalculator.generate_metrics_report(return_metrics, trade_metrics)

        assert "回测指标报告" in report
        assert "收益指标" in report
        assert "风险调整指标" in report
        assert "交易指标" in report