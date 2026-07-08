"""可视化模块单元测试

覆盖范围：
- 交互式图表（净值曲线、滚动指标、信号对比、热力图）

注：通用回测 HTML/文本报告测试（TestReportGenerator）已在蔡森专精化
Phase 1·Task 4 随 viz.report.ReportGenerator 整体删除。
"""
import pytest
import numpy as np
import pandas as pd
from datetime import datetime

from viz import InteractiveChart


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