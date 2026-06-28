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

    def test_ema_exact_recursive_values(self):
        """精确值断言：手算递归式逐项校验，捕获 adjust=True 等实现错误

        α = 2/(3+1) = 0.5，递归式 y_0 = x_0; y_t = 0.5·x_t + 0.5·y_{t-1}：
          y_0 = 1.0
          y_1 = 0.5·2.0 + 0.5·1.0   = 1.5
          y_2 = 0.5·3.0 + 0.5·1.5   = 2.25
          y_3 = 0.5·4.0 + 0.5·2.25  = 3.125
          y_4 = 0.5·5.0 + 0.5·3.125 = 4.0625
        若误用 adjust=True，起始处会因归一化权重而偏离这套值。
        """
        s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
        ema = EMA(s, n=3)
        expected = [1.0, 1.5, 2.25, 3.125, 4.0625]
        for got, exp in zip(ema, expected):
            assert got == pytest.approx(exp)

    def test_n_equal_one_equals_input(self):
        """n=1 时 α=1，EMA 完全跟踪输入（逐项相等）

        边界覆盖：ewm(span=1) 不应报错，且结果与输入逐项一致。
        """
        s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
        pd.testing.assert_series_equal(EMA(s, 1), s)

    def test_nan_input_does_not_crash(self):
        """含 NaN 输入不抛异常，返回等长 Series（不固定具体传播值）

        ewm 对 NaN 有其内部传播规则（此处实测为 [1.0, 1.0, 2.5]），
        本断言只保证"不崩 + 等长"，避免对 pandas 内部实现做硬编码。
        """
        s = pd.Series([1.0, np.nan, 3.0])
        result = EMA(s, n=3)  # 不应抛异常
        assert isinstance(result, pd.Series)
        assert len(result) == 3


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

    def test_n_greater_than_len_all_nan(self):
        """n > len 时窗口永远填不满，结果全 NaN

        边界覆盖：rolling(5) 对长度 3 的序列不报错，返回 3 个 NaN。
        """
        s = pd.Series([1.0, 2.0, 3.0])
        result = MA(s, n=5)
        assert result.isna().all()

    def test_empty_series_returns_empty(self):
        """空 Series 输入不抛异常，返回空 Series

        边界覆盖：rolling 对空输入直接返回空，调用方无需特判。
        """
        s = pd.Series([], dtype=float)
        result = MA(s, n=3)  # 不应抛异常
        assert isinstance(result, pd.Series)
        assert len(result) == 0


class TestMacd:
    """测试 MACD 指标（通达信约定）"""

    def _close(self, n=60, seed=42):
        np.random.seed(seed)
        return pd.Series(100 + np.cumsum(np.random.randn(n)))

    def test_returns_three_series(self):
        """返回 DIF/DEA/HIST 三个同索引序列"""
        from factors.mytt import MACD

        close = self._close()
        dif, dea, hist = MACD(close, fast=12, slow=26, signal=9)
        assert len(dif) == len(close)
        assert len(dea) == len(close)
        assert len(hist) == len(close)

    def test_dif_formula(self):
        """DIF = EMA(close,fast) - EMA(close,slow)"""
        from factors.mytt import MACD, EMA

        close = self._close()
        dif, _, _ = MACD(close, 12, 26, 9)
        expected = EMA(close, 12) - EMA(close, 26)
        pd.testing.assert_series_equal(dif, expected, check_names=False)

    def test_dea_is_ema_of_dif(self):
        """DEA = EMA(DIF, signal)——对 DIF 再求 EMA，非对 close"""
        from factors.mytt import MACD, EMA

        close = self._close()
        dif, dea, _ = MACD(close, 12, 26, 9)
        pd.testing.assert_series_equal(dea, EMA(dif, 9), check_names=False)

    def test_hist_double_difference(self):
        """HIST = (DIF - DEA) * 2（通达信约定）"""
        from factors.mytt import MACD

        close = self._close()
        dif, dea, hist = MACD(close, 12, 26, 9)
        expected = (dif - dea) * 2
        pd.testing.assert_series_equal(hist, expected, check_names=False)
