"""可视化模块单元测试

覆盖范围：
- 交互式图表（净值曲线、滚动指标、信号对比、热力图）
- 报告生成（HTML、文本）
"""
import pytest
import numpy as np
import pandas as pd
from datetime import datetime

from viz import InteractiveChart, ReportGenerator


@pytest.fixture
def sample_daily_df():
    """生成示例每日记录"""
    dates = pd.date_range("2023-01-01", periods=100, freq="D", tz="Asia/Shanghai")
    np.random.seed(42)

    # 生成净值曲线（使用 list 避免 numpy 数组问题）
    nav_values = pd.Series(1_000_000 * (1 + np.cumsum(np.random.randn(100) * 0.01)))

    df = pd.DataFrame({
        "nav": nav_values,
        "cash": nav_values * 0.5,
        "position": np.random.randint(0, 1000, 100),
        "position_value": nav_values * 0.5,
        "price": np.random.rand(100) * 100 + 100,
        "signal": np.random.rand(100),
    }, index=dates)

    return df


@pytest.fixture
def sample_rolling_df():
    """生成示例滚动指标"""
    dates = pd.date_range("2023-01-01", periods=100, freq="D", tz="Asia/Shanghai")
    np.random.seed(42)

    df = pd.DataFrame({
        "rolling_return": np.random.randn(100) * 0.02,
        "rolling_volatility": np.random.rand(100) * 0.2,
        "rolling_sharpe": np.random.randn(100) * 0.5,
        "rolling_drawdown": -np.random.rand(100) * 0.1,
    }, index=dates)

    return df


@pytest.fixture
def sample_factor_attribution():
    """生成示例因子归因"""
    dates = pd.date_range("2023-01-01", periods=100, freq="D", tz="Asia/Shanghai")
    np.random.seed(42)

    return {
        "tech_rolling_corr": pd.Series(np.random.randn(100) * 0.3, index=dates),
        "macro_rolling_corr": pd.Series(np.random.randn(100) * 0.2, index=dates),
        "fused_rolling_corr": pd.Series(np.random.randn(100) * 0.4, index=dates),
        "tech_correlation": 0.3,
        "macro_correlation": 0.2,
        "fused_correlation": 0.4,
        "tech_contribution": 0.21,
        "macro_contribution": 0.06,
    }


@pytest.fixture
def sample_result():
    """生成示例回测结果"""
    dates = pd.date_range("2023-01-01", periods=100, freq="D", tz="Asia/Shanghai")
    np.random.seed(42)

    nav_values = 1_000_000 * (1 + np.cumsum(np.random.randn(100) * 0.01))

    daily_df = pd.DataFrame({
        "nav": nav_values,
        "cash": nav_values * 0.5,
        "position": np.random.randint(0, 1000, 100),
        "position_value": nav_values * 0.5,
        "price": np.random.rand(100) * 100 + 100,
        "signal": np.random.rand(100),
    }, index=dates)

    trades_df = pd.DataFrame({
        "date": pd.date_range("2023-01-01", periods=20),
        "direction": ["buy"] * 10 + ["sell"] * 10,
        "shares": [1000] * 20,
        "price": [100] * 20,
        "amount": [100000] * 20,
        "cost": [30] * 20,
        "symbol": ["600000.SH"] * 20,
    })

    return {
        "initial_capital": 1_000_000,
        "final_nav": float(nav_values[-1]),
        "total_return": 0.1,
        "annual_return": 0.12,
        "annual_volatility": 0.15,
        "max_drawdown": -0.05,
        "sharpe_ratio": 0.6,
        "calmar_ratio": 2.4,
        "win_rate": 0.6,
        "profit_loss_ratio": 1.5,
        "n_trades": 20,
        "n_failed_trades": 0,
        "trades": trades_df,
        "daily_records": daily_df,
    }


class TestInteractiveChart:
    """测试交互式图表生成器"""

    @pytest.fixture
    def chart_generator(self):
        """初始化图表生成器"""
        return InteractiveChart(theme="plotly_white")

    def test_initialization(self, chart_generator):
        """测试初始化"""
        assert chart_generator.theme == "plotly_white"

    def test_plot_nav_curve_returns_figure(self, chart_generator, sample_daily_df):
        """测试净值曲线返回 Figure"""
        fig = chart_generator.plot_nav_curve(sample_daily_df, show=False)

        assert fig is not None

    def test_plot_nav_curve_with_show_false(self, chart_generator, sample_daily_df):
        """测试净值曲线不显示"""
        fig = chart_generator.plot_nav_curve(sample_daily_df, show=False)

        # 不应抛出异常
        assert fig is not None

    def test_plot_rolling_metrics_returns_figure(self, chart_generator, sample_rolling_df):
        """测试滚动指标返回 Figure"""
        fig = chart_generator.plot_rolling_metrics(sample_rolling_df, show=False)

        assert fig is not None

    def test_plot_rolling_metrics_with_show_false(self, chart_generator, sample_rolling_df):
        """测试滚动指标不显示"""
        fig = chart_generator.plot_rolling_metrics(sample_rolling_df, show=False)

        # 不应抛出异常
        assert fig is not None

    def test_plot_signal_vs_price_returns_figure(self, chart_generator, sample_daily_df):
        """测试信号与价格对比返回 Figure"""
        signal = pd.Series(np.random.rand(100), index=sample_daily_df.index)
        fig = chart_generator.plot_signal_vs_price(sample_daily_df, signal, show=False)

        assert fig is not None

    def test_plot_signal_vs_price_with_show_false(self, chart_generator, sample_daily_df):
        """测试信号与价格对比不显示"""
        signal = pd.Series(np.random.rand(100), index=sample_daily_df.index)
        fig = chart_generator.plot_signal_vs_price(sample_daily_df, signal, show=False)

        # 不应抛出异常
        assert fig is not None

    def test_plot_factor_correlation_returns_figure(self, chart_generator, sample_factor_attribution):
        """测试因子相关性返回 Figure"""
        fig = chart_generator.plot_factor_correlation(sample_factor_attribution, show=False)

        assert fig is not None

    def test_plot_factor_correlation_with_show_false(self, chart_generator, sample_factor_attribution):
        """测试因子相关性不显示"""
        fig = chart_generator.plot_factor_correlation(sample_factor_attribution, show=False)

        # 不应抛出异常
        assert fig is not None

    def test_plot_heatmap_returns_figure(self, chart_generator, sample_daily_df):
        """测试热力图返回 Figure"""
        fig = chart_generator.plot_heatmap(sample_daily_df[["nav", "price", "signal"]], show=False)

        assert fig is not None

    def test_plot_heatmap_with_show_false(self, chart_generator, sample_daily_df):
        """测试热力图不显示"""
        fig = chart_generator.plot_heatmap(sample_daily_df[["nav", "price", "signal"]], show=False)

        # 不应抛出异常
        assert fig is not None

    def test_save_html_writes_file(self, chart_generator, sample_daily_df, tmp_path):
        """测试保存 HTML 写入文件"""
        fig = chart_generator.plot_nav_curve(sample_daily_df, show=False)

        filepath = tmp_path / "test.html"
        chart_generator.save_html(fig, str(filepath))

        assert filepath.exists()

    def test_save_html_creates_valid_html(self, chart_generator, sample_daily_df, tmp_path):
        """测试保存 HTML 创建有效的 HTML 文件"""
        fig = chart_generator.plot_nav_curve(sample_daily_df, show=False)

        filepath = tmp_path / "test.html"
        chart_generator.save_html(fig, str(filepath))

        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()

        # 检查是否包含 HTML 标签
        assert "<html" in content or "<!DOCTYPE html>" in content
        assert "</html>" in content


class TestReportGenerator:
    """测试报告生成器"""

    @pytest.fixture
    def report_generator(self):
        """初始化报告生成器"""
        return ReportGenerator()

    def test_initialization(self, report_generator):
        """测试初始化"""
        assert report_generator.chart_generator is not None

    def test_generate_html_report_writes_file(self, report_generator, sample_result, tmp_path):
        """测试生成 HTML 报告写入文件"""
        filepath = tmp_path / "test_report.html"
        report_generator.generate_html_report(sample_result, str(filepath), include_charts=False)

        assert filepath.exists()

    def test_generate_html_report_creates_valid_html(self, report_generator, sample_result, tmp_path):
        """测试生成 HTML 报告创建有效的 HTML 文件"""
        filepath = tmp_path / "test_report.html"
        report_generator.generate_html_report(sample_result, str(filepath), include_charts=False)

        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()

        # 检查是否包含 HTML 标签
        assert "<html" in content or "<!DOCTYPE html>" in content
        assert "</html>" in content

    def test_generate_html_report_includes_title(self, report_generator, sample_result, tmp_path):
        """测试 HTML 报告包含标题"""
        filepath = tmp_path / "test_report.html"
        report_generator.generate_html_report(sample_result, str(filepath), include_charts=False)

        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()

        assert "回测报告" in content

    def test_generate_text_report_returns_string(self, report_generator, sample_result):
        """测试生成文本报告返回字符串"""
        report = report_generator.generate_text_report(sample_result)

        assert isinstance(report, str)

    def test_generate_text_report_includes_headers(self, report_generator, sample_result):
        """测试文本报告包含标题"""
        report = report_generator.generate_text_report(sample_result)

        assert "回测指标报告" in report
        assert "收益指标" in report

    def test_generate_text_report_handles_empty_trades(self, report_generator):
        """测试文本报告处理空交易记录"""
        result = {
            "initial_capital": 1_000_000,
            "final_nav": 1_100_000,
            "daily_records": pd.DataFrame({
                "nav": [1_000_000, 1_100_000],
            }),
            "trades": pd.DataFrame(),
        }

        # 不应抛出异常
        report = report_generator.generate_text_report(result)
        assert isinstance(report, str)

    def test_generate_text_report_handles_empty_daily_records(self, report_generator):
        """测试文本报告处理空每日记录"""
        result = {
            "initial_capital": 1_000_000,
            "final_nav": 1_100_000,
            "daily_records": pd.DataFrame(),
            "trades": pd.DataFrame(),
        }

        # 不应抛出异常
        report = report_generator.generate_text_report(result)
        assert isinstance(report, str)

    def test_generate_html_report_with_charts(self, report_generator, sample_result, tmp_path):
        """测试生成包含图表的 HTML 报告"""
        filepath = tmp_path / "test_report_with_charts.html"
        report_generator.generate_html_report(sample_result, str(filepath), include_charts=True)

        # 主报告文件应存在
        assert filepath.exists()

    def test_generate_html_report_without_charts(self, report_generator, sample_result, tmp_path):
        """测试生成不包含图表的 HTML 报告"""
        filepath = tmp_path / "test_report_no_charts.html"
        report_generator.generate_html_report(sample_result, str(filepath), include_charts=False)

        # 主报告文件应存在
        assert filepath.exists()

    def test_generate_html_report_handles_missing_fields(self, report_generator, tmp_path):
        """测试 HTML 报告处理缺失字段"""
        result = {
            "initial_capital": 1_000_000,
            "daily_records": pd.DataFrame(),
            "trades": pd.DataFrame(),
        }

        filepath = tmp_path / "test_report_missing_fields.html"
        # 不应抛出异常
        report_generator.generate_html_report(result, str(filepath), include_charts=False)

        assert filepath.exists()