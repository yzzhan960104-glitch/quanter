"""MyTT 通达信指标库单元测试

覆盖：EMA / MA 基础函数的纯向量化行为与边界。
风格对齐 tests/test_factors.py（class 组织、中文 docstring）。
"""
import numpy as np
import pandas as pd
import pytest

from factors.mytt import EMA, MA


@pytest.fixture
def close_series():
    """构造带趋势的收盘价序列（100 期，tz-aware 索引）"""
    dates = pd.date_range("2023-01-01", periods=100, freq="D", tz="Asia/Shanghai")
    np.random.seed(42)
    prices = 100 + np.cumsum(np.random.randn(100))
    return pd.Series(prices, index=dates, name="close")


class TestEMA:
    """测试指数移动平均（通达信 adjust=False 递归式）"""

    def test_returns_series_with_same_index(self, close_series):
        """返回 Series 且索引与输入一致"""
        ema = EMA(close_series, n=12)
        assert isinstance(ema, pd.Series)
        pd.testing.assert_index_equal(ema.index, close_series.index)

    def test_first_value_equals_first_input(self):
        """adjust=False：EMA 首值 = 输入首值"""
        s = pd.Series([10.0, 20.0, 30.0, 40.0])
        ema = EMA(s, n=5)
        assert ema.iloc[0] == pytest.approx(10.0)

    def test_no_nan_in_output(self, close_series):
        """EMA 递归式全程有值，无 NaN"""
        ema = EMA(close_series, n=12)
        assert not ema.isna().any()

    def test_smoother_than_raw(self, close_series):
        """EMA 比原始序列更平滑（标准差更小）"""
        ema = EMA(close_series, n=12)
        assert ema.std() < close_series.std()


class TestMA:
    """测试简单移动平均"""

    def test_returns_series_with_same_index(self, close_series):
        """返回 Series 且索引与输入一致"""
        ma = MA(close_series, n=5)
        assert isinstance(ma, pd.Series)
        pd.testing.assert_index_equal(ma.index, close_series.index)

    def test_window_mean_exact(self):
        """MA(3) 第 3 个值 = 前 3 个值的均值"""
        s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
        ma = MA(s, n=3)
        assert ma.iloc[2] == pytest.approx(2.0)

    def test_has_nan_before_window(self):
        """MA(3) 前 2 个值为 NaN（窗口未满）"""
        s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
        ma = MA(s, n=3)
        assert ma.iloc[:2].isna().all()
