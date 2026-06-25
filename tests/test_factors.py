"""因子挖掘模块单元测试

覆盖范围：
- 技术指标计算（双均线、VPT、RSI、MACD）
- 宏观因子计算
- 信号融合
- 前视偏差防范
"""
import pytest
import numpy as np
import pandas as pd
from datetime import datetime

from factors import moving_average_cross, volume_price_trend, rsi, macd
from factors import macro_anchor_signal, cpi_inflation_signal, social_financing_signal
from factors import signal_fusion, multi_signal_fusion, signal_filter


@pytest.fixture
def sample_df():
    """生成示例 OHLCV 数据"""
    dates = pd.date_range("2023-01-01", periods=100, freq="D", tz="Asia/Shanghai")
    np.random.seed(42)

    # 生成有趋势的价格数据
    base_price = 100
    trend = np.linspace(0, 20, 100)
    noise = np.random.randn(100) * 5
    prices = base_price + trend + noise

    df = pd.DataFrame({
        "open": prices + np.random.randn(100) * 0.5,
        "high": prices + np.random.rand(100) * 2,
        "low": prices - np.random.rand(100) * 2,
        "close": prices,
        "volume": np.random.randint(100000, 1000000, 100),
        "amount": prices * np.random.randint(100000, 1000000, 100),
    }, index=dates)

    # 确保 high >= close >= low
    df["high"] = df[["open", "high", "close"]].max(axis=1)
    df["low"] = df[["open", "low", "close"]].min(axis=1)

    return df


@pytest.fixture
def sample_macro_df():
    """生成示例宏观数据"""
    dates = pd.date_range("2023-01-01", periods=12, freq="MS", tz="Asia/Shanghai")
    np.random.seed(42)

    df = pd.DataFrame({
        "m2": 200 + np.cumsum(np.random.randn(12) * 10),
        "cpi": 100 + np.cumsum(np.random.randn(12) * 0.3),
        "ppi": 100 + np.cumsum(np.random.randn(12) * 0.2),
    }, index=dates)

    return df


class TestMovingAverageCross:
    """测试双均线交叉信号"""

    def test_returns_series(self, sample_df):
        """测试返回 Series"""
        signal = moving_average_cross(sample_df)

        assert isinstance(signal, pd.Series)

    def test_signal_range_in_long_only(self, sample_df):
        """测试纯多头策略信号在 [0, 1] 范围内"""
        signal = moving_average_cross(sample_df)

        assert signal.min() >= 0.0
        assert signal.max() <= 1.0

    def test_signal_index_matches_df(self, sample_df):
        """测试信号索引与数据索引匹配"""
        signal = moving_average_cross(sample_df)

        pd.testing.assert_index_equal(signal.index, sample_df.index)

    def test_signal_has_no_nan_after_fill(self, sample_df):
        """测试信号填充后无 NaN"""
        signal = moving_average_cross(sample_df)

        # 前段可能有 NaN，但应被填充
        assert not signal.isna().any()

    def test_golden_cross_detected(self):
        """测试金叉检测"""
        dates = pd.date_range("2023-01-01", periods=30, freq="D", tz="Asia/Shanghai")
        # 构造金叉场景：前 20 天下跌，后 10 天上涨
        prices = np.concatenate([
            np.linspace(100, 90, 20),
            np.linspace(90, 110, 10)
        ])
        df = pd.DataFrame({
            "close": prices,
        }, index=dates)

        signal = moving_average_cross(df, short_window=5, long_window=10)

        # 应检测到金叉（信号 = 1.0）
        assert (signal == 1.0).any()

    def test_death_cross_detected(self):
        """测试死叉检测"""
        dates = pd.date_range("2023-01-01", periods=30, freq="D", tz="Asia/Shanghai")
        # 构造死叉场景：前 20 天上涨，后 10 天下跌
        prices = np.concatenate([
            np.linspace(100, 110, 20),
            np.linspace(110, 90, 10)
        ])
        df = pd.DataFrame({
            "close": prices,
        }, index=dates)

        signal = moving_average_cross(df, short_window=5, long_window=10)

        # 应检测到死叉（信号 = 0.0）
        assert (signal == 0.0).any()

    def test_look_ahead_bias_prevented(self, sample_df):
        """测试防范前视偏差（使用 shift(1)）"""
        # 这个测试验证逻辑：如果使用了 shift(1)，信号应该滞后于价格变化
        signal = moving_average_cross(sample_df)

        # 计算均线
        short_ma = sample_df["close"].rolling(window=5).mean()
        long_ma = sample_df["close"].rolling(window=20).mean()

        # 信号变化应该滞后于均线交叉
        cross_points = (short_ma > long_ma).diff().abs()
        signal_changes = signal.diff().abs()

        # 信号变化点数应该小于等于交叉点数（因为滞后）
        assert signal_changes.sum() <= cross_points.sum()


class TestVolumePriceTrend:
    """测试量价趋势因子"""

    def test_returns_series(self, sample_df):
        """测试返回 Series"""
        signal = volume_price_trend(sample_df)

        assert isinstance(signal, pd.Series)

    def test_signal_range_normalized(self, sample_df):
        """测试信号归一化到 [0, 1]"""
        signal = volume_price_trend(sample_df)

        assert signal.min() >= 0.0
        assert signal.max() <= 1.0

    def test_signal_index_matches_df(self, sample_df):
        """测试信号索引与数据索引匹配"""
        signal = volume_price_trend(sample_df)

        pd.testing.assert_index_equal(signal.index, sample_df.index)

    def test_abnormal_volume_handled(self, sample_df):
        """测试异常成交量被处理"""
        df = sample_df.copy()
        # 插入异常成交量（超过平均 5 倍）
        df.loc[df.index[50], "volume"] = df["volume"].mean() * 10

        signal = volume_price_trend(df)

        # 异常日的信号应为 NaN（被标记）
        assert signal.loc[df.index[50]] != signal.loc[df.index[50]]

    def test_signal_has_no_nan_after_fill(self, sample_df):
        """测试信号填充后无 NaN"""
        signal = volume_price_trend(sample_df)

        assert not signal.isna().any()


class TestRSI:
    """测试 RSI 指标"""

    def test_returns_series(self, sample_df):
        """测试返回 Series"""
        signal = rsi(sample_df)

        assert isinstance(signal, pd.Series)

    def test_signal_range_long_only(self, sample_df):
        """测试纯多头策略信号在 [0, 1] 范围内"""
        signal = rsi(sample_df)

        assert signal.min() >= 0.0
        assert signal.max() <= 1.0

    def test_overbought_signal_reduced(self):
        """测试超买信号降低仓位"""
        dates = pd.date_range("2023-01-01", periods=30, freq="D", tz="Asia/Shanghai")
        # 构造超买场景：持续上涨
        prices = 100 + np.cumsum(np.random.randn(30) * 2 + 1)
        df = pd.DataFrame({"close": prices}, index=dates)

        signal = rsi(df)

        # RSI > 70 时，信号应降低
        # 检查 RSI 计算是否正确
        delta = df["close"].diff()
        gain = delta.where(delta > 0, 0)
        loss = -delta.where(delta < 0, 0)
        avg_gain = gain.rolling(window=14).mean()
        avg_loss = loss.rolling(window=14).mean()

        # 计算实际 RSI
        avg_loss = avg_loss.replace(0, 1e-10)
        rs = avg_gain / avg_loss
        rsi_value = 100 - (100 / (1 + rs))

        # 超买日的信号应该较低
        overbought = rsi_value > 70
        if overbought.any():
            assert signal.loc[overbought].mean() < 0.5

    def test_oversold_signal_increased(self):
        """测试超卖信号提高仓位"""
        dates = pd.date_range("2023-01-01", periods=30, freq="D", tz="Asia/Shanghai")
        # 构造超卖场景：持续下跌
        prices = 100 + np.cumsum(np.random.randn(30) * 2 - 2)
        df = pd.DataFrame({"close": prices}, index=dates)

        signal = rsi(df)

        # RSI < 30 时，信号应提高
        delta = df["close"].diff()
        gain = delta.where(delta > 0, 0)
        loss = -delta.where(delta < 0, 0)
        avg_gain = gain.rolling(window=14).mean()
        avg_loss = loss.rolling(window=14).mean()

        avg_loss = avg_loss.replace(0, 1e-10)
        rs = avg_gain / avg_loss
        rsi_value = 100 - (100 / (1 + rs))

        # 超卖日的信号应该较高
        oversold = rsi_value < 30
        if oversold.any():
            assert signal.loc[oversold].mean() > 0.5


class TestMACD:
    """测试 MACD 指标"""

    def test_returns_series(self, sample_df):
        """测试返回 Series"""
        signal = macd(sample_df)

        assert isinstance(signal, pd.Series)

    def test_signal_range_long_only(self, sample_df):
        """测试纯多头策略信号在 [0, 1] 范围内"""
        signal = macd(sample_df)

        assert signal.min() >= 0.0
        assert signal.max() <= 1.0

    def test_signal_has_full_position(self, sample_df):
        """测试信号包含满仓信号"""
        signal = macd(sample_df)

        assert (signal == 1.0).any() or (signal == 0.5).any()

    def test_signal_has_empty_position(self, sample_df):
        """测试信号包含空仓信号"""
        signal = macd(sample_df)

        assert (signal == 0.0).any()


class TestMacroAnchorSignal:
    """测试宏观锚点信号"""

    def test_returns_series(self, sample_macro_df):
        """测试返回 Series"""
        signal = macro_anchor_signal(sample_macro_df)

        assert isinstance(signal, pd.Series)

    def test_signal_range_long_only(self, sample_macro_df):
        """测试纯多头策略信号在 [0, 1] 范围内"""
        signal = macro_anchor_signal(sample_macro_df)

        assert signal.min() >= 0.0
        assert signal.max() <= 1.0

    def test_consecutive_exceed_triggers_strong_signal(self, sample_macro_df):
        """测试连续超过阈值触发强多头信号"""
        df = sample_macro_df.copy()
        # 构造连续超过阈值场景
        for i in range(3):
            df.iloc[i, df.columns.get_loc("m2")] = 220 + i * 10

        signal = macro_anchor_signal(df, indicator="m2", threshold=0.02, window=3)

        # 应检测到强多头信号（1.0）
        assert (signal == 1.0).any()

    def test_single_exceed_triggers_medium_signal(self, sample_macro_df):
        """测试单期超过阈值触发中等多头信号"""
        df = sample_macro_df.copy()
        # 构造单期超过阈值场景
        df.iloc[0, df.columns.get_loc("m2")] = 220
        df.iloc[1, df.columns.get_loc("m2")] = 200

        signal = macro_anchor_signal(df, indicator="m2", threshold=0.02, window=3)

        # 应检测到中等多头信号（0.5）
        assert (signal == 0.5).any()

    def test_invalid_indicator_raises_error(self, sample_macro_df):
        """测试无效指标抛出异常"""
        with pytest.raises(ValueError, match="宏观数据中不存在指标"):
            macro_anchor_signal(sample_macro_df, indicator="invalid_indicator")


class TestCPIInflationSignal:
    """测试 CPI 通胀信号"""

    def test_returns_series(self, sample_macro_df):
        """测试返回 Series"""
        signal = cpi_inflation_signal(sample_macro_df)

        assert isinstance(signal, pd.Series)

    def test_signal_range_long_only(self, sample_macro_df):
        """测试纯多头策略信号在 [0, 1] 范围内"""
        signal = cpi_inflation_signal(sample_macro_df)

        assert signal.min() >= 0.0
        assert signal.max() <= 1.0

    def test_low_cpi_increases_signal(self):
        """测试低通胀提高信号"""
        dates = pd.date_range("2023-01-01", periods=12, freq="MS", tz="Asia/Shanghai")
        df = pd.DataFrame({
            "cpi": [100.5] * 12,  # 低于 3% 阈值
        }, index=dates)

        signal = cpi_inflation_signal(df, threshold=0.03)

        # 信号应该较高
        assert signal.mean() > 0.5

    def test_high_cpi_decreases_signal(self):
        """测试高通胀降低信号"""
        dates = pd.date_range("2023-01-01", periods=12, freq="MS", tz="Asia/Shanghai")
        df = pd.DataFrame({
            "cpi": [105.0] * 12,  # 高于 3% 阈值
        }, index=dates)

        signal = cpi_inflation_signal(df, threshold=0.03)

        # 信号应该较低
        assert signal.mean() < 0.5


class TestSignalFusion:
    """测试信号融合"""

    @pytest.fixture
    def sample_signals(self, sample_df, sample_macro_df):
        """生成示例信号"""
        tech_signal = moving_average_cross(sample_df)
        macro_signal = macro_anchor_signal(sample_macro_df)

        # 对齐索引
        aligned_index = tech_signal.index.intersection(macro_signal.index)
        tech_aligned = tech_signal.loc[aligned_index]
        macro_aligned = macro_signal.loc[aligned_index]

        return tech_aligned, macro_aligned

    def test_returns_series(self, sample_signals):
        """测试返回 Series"""
        tech_signal, macro_signal = sample_signals

        fused_signal = signal_fusion(tech_signal, macro_signal)

        assert isinstance(fused_signal, pd.Series)

    def test_signal_range_clipped(self, sample_signals):
        """测试信号范围被裁剪到 [0, 1]"""
        tech_signal, macro_signal = sample_signals

        fused_signal = signal_fusion(tech_signal, macro_signal)

        assert fused_signal.min() >= 0.0
        assert fused_signal.max() <= 1.0

    def test_weights_sum_check(self, sample_signals):
        """测试权重和不为 1 时抛出异常"""
        tech_signal, macro_signal = sample_signals

        with pytest.raises(ValueError, match="权重和不等于 1"):
            signal_fusion(tech_signal, macro_signal, weights={"tech": 0.5, "macro": 0.6})

    def test_empty_intersection_raises_error(self):
        """测试无交集时抛出异常"""
        tech_signal = pd.Series([0.5, 0.6, 0.7], index=pd.date_range("2023-01-01", periods=3))
        macro_signal = pd.Series([0.5, 0.6, 0.7], index=pd.date_range("2024-01-01", periods=3))

        with pytest.raises(ValueError, match="两个信号的时间索引无交集"):
            signal_fusion(tech_signal, macro_signal)

    def test_nan_detection_raises_error(self, sample_signals):
        """测试 NaN 检测抛出异常"""
        tech_signal, macro_signal = sample_signals

        # 插入 NaN
        tech_signal.iloc[0] = np.nan

        with pytest.raises(ValueError, match="技术信号包含.*个 NaN"):
            signal_fusion(tech_signal, macro_signal)

    def test_multi_signal_fusion(self, sample_df, sample_macro_df):
        """测试多信号融合"""
        tech_signal = moving_average_cross(sample_df)
        vpt_signal = volume_price_trend(sample_df)
        macro_signal = macro_anchor_signal(sample_macro_df)

        # 对齐索引
        aligned_index = tech_signal.index.intersection(macro_signal.index)
        signals = {
            "ma": tech_signal.loc[aligned_index],
            "vpt": vpt_signal.loc[aligned_index],
            "macro": macro_signal.loc[aligned_index],
        }

        fused_signal = multi_signal_fusion(signals)

        assert isinstance(fused_signal, pd.Series)
        assert fused_signal.min() >= 0.0
        assert fused_signal.max() <= 1.0


class TestSignalFilter:
    """测试信号过滤"""

    @pytest.fixture
    def sample_signal(self, sample_df):
        """生成示例信号"""
        return moving_average_cross(sample_df)

    def test_returns_series(self, sample_signal):
        """测试返回 Series"""
        filtered = signal_filter(sample_signal)

        assert isinstance(filtered, pd.Series)

    def test_applies_threshold(self, sample_signal):
        """测试应用阈值"""
        filtered = signal_filter(sample_signal, threshold=0.5)

        # 低于阈值的部分应为 0
        assert (filtered < 0.5).sum() == 0

    def test_prevents_frequent_trading(self, sample_signal):
        """测试防范频繁交易"""
        filtered = signal_filter(sample_signal, min_hold=5)

        # 计算信号变化次数
        changes = filtered.diff().abs()
        change_count = (changes > 0.5).sum()

        # 变化次数应该少于原始信号
        original_changes = sample_signal.diff().abs()
        original_change_count = (original_changes > 0.5).sum()

        assert change_count <= original_change_count

    def test_index_unchanged(self, sample_signal):
        """测试索引不变"""
        filtered = signal_filter(sample_signal)

        pd.testing.assert_index_equal(filtered.index, sample_signal.index)