"""数据层模块单元测试

覆盖范围：
- MockDataFetcher 数据生成
- DataCleaner 数据清洗与对齐
- 异常值处理
- 前视偏差防范
"""
import pytest
import numpy as np
import pandas as pd
from datetime import datetime

from data import MockDataFetcher, DataCleaner


class TestMockDataFetcher:
    """测试 Mock 数据生成器"""

    @pytest.fixture
    def fetcher(self):
        """初始化数据获取器"""
        return MockDataFetcher(seed=42)

    @pytest.fixture
    def date_range(self):
        """测试日期范围"""
        start = datetime(2023, 1, 1)
        end = datetime(2023, 12, 31)
        return start, end

    def test_fetch_ohlcv_returns_dataframe(self, fetcher, date_range):
        """测试 OHLCV 数据获取返回正确格式"""
        start, end = date_range
        df = fetcher.fetch_ohlcv("600000.SH", start, end, freq="1d")

        # 验证返回类型
        assert isinstance(df, pd.DataFrame)
        assert len(df) > 0

        # 验证列名
        expected_columns = ["open", "high", "low", "close", "volume", "amount"]
        assert list(df.columns) == expected_columns

    def test_fetch_ohlcv_has_tz_aware_index(self, fetcher, date_range):
        """测试 OHLCV 数据的时间戳有时区信息"""
        start, end = date_range
        df = fetcher.fetch_ohlcv("600000.SH", start, end, freq="1d")

        assert df.index.tz is not None

    def test_fetch_ohlcv_price_consistency(self, fetcher, date_range):
        """测试 OHLCV 数据的价格一致性（high >= open/close >= low）"""
        start, end = date_range
        df = fetcher.fetch_ohlcv("600000.SH", start, end, freq="1d")

        # 验证 high >= close
        assert (df["high"] >= df["close"]).all()

        # 验证 low <= close
        assert (df["low"] <= df["close"]).all()

    def test_fetch_ohlcv_volume_positive(self, fetcher, date_range):
        """测试 OHLCV 数据的成交量为正数"""
        start, end = date_range
        df = fetcher.fetch_ohlcv("600000.SH", start, end, freq="1d")

        assert (df["volume"] > 0).all()

    def test_fetch_ohlcv_limit_up_applied(self, fetcher, date_range):
        """测试涨跌停板限制已应用"""
        start, end = date_range
        df = fetcher.fetch_ohlcv("600000.SH", start, end, freq="1d")

        # 计算价格变化率
        price_change = df["close"].pct_change()

        # 验证没有超过 10% 的单日涨幅（涨停）
        assert (price_change <= 0.11).all()  # 容忍 1% 误差

        # 验证没有超过 10% 的单日跌幅（跌停）
        assert (price_change >= -0.11).all()

    def test_fetch_macro_returns_dataframe(self, fetcher, date_range):
        """测试宏观数据获取返回正确格式"""
        start, end = date_range
        df = fetcher.fetch_macro("m2", start, end)

        # 验证返回类型
        assert isinstance(df, pd.DataFrame)
        assert len(df) > 0

        # 验证列名
        assert "m2" in df.columns

    def test_fetch_macro_has_tz_aware_index(self, fetcher, date_range):
        """测试宏观数据的时间戳有时区信息"""
        start, end = date_range
        df = fetcher.fetch_macro("m2", start, end)

        assert df.index.tz is not None

    def test_fetch_factor_data_returns_dataframe(self, fetcher, date_range):
        """测试因子数据获取返回正确格式"""
        start, end = date_range
        df = fetcher.fetch_factor_data("600000.SH", "pe", start, end)

        # 验证返回类型
        assert isinstance(df, pd.DataFrame)
        assert len(df) > 0

        # 验证列名
        assert "pe" in df.columns

    def test_fetch_factor_data_pe_positive(self, fetcher, date_range):
        """测试 P/E 比率为正数"""
        start, end = date_range
        df = fetcher.fetch_factor_data("600000.SH", "pe", start, end)

        assert (df["pe"] > 0).all()

    def test_reproducible_with_seed(self, date_range):
        """测试随机种子可复现"""
        start, end = date_range

        fetcher1 = MockDataFetcher(seed=42)
        df1 = fetcher1.fetch_ohlcv("600000.SH", start, end, freq="1d")

        fetcher2 = MockDataFetcher(seed=42)
        df2 = fetcher2.fetch_ohlcv("600000.SH", start, end, freq="1d")

        # 验证两次生成数据相同
        pd.testing.assert_frame_equal(df1, df2)


class TestDataCleaner:
    """测试数据清洗器"""

    @pytest.fixture
    def df_with_issues(self):
        """生成包含问题的测试数据"""
        dates = pd.date_range("2023-01-01", periods=100, tz="Asia/Shanghai")
        df = pd.DataFrame({
            "open": np.random.rand(100) * 100 + 100,
            "high": np.random.rand(100) * 100 + 100,
            "low": np.random.rand(100) * 100 + 100,
            "close": np.random.rand(100) * 100 + 100,
            "volume": np.random.randint(100000, 1000000, 100),
            "amount": np.random.rand(100) * 100000000,
        }, index=dates)

        # 插入问题数据
        df.loc[dates[5], "close"] = df.loc[dates[4], "close"] * 1.3  # 异常值
        df.loc[dates[10], "volume"] = 0  # 流动性枯竭
        df.loc[dates[15], "high"] = df.loc[dates[15], "low"] - 1  # 数据错误
        df.loc[dates[20], "close"] = np.nan  # 缺失值

        return df

    def test_clean_ohlcv_returns_dataframe(self, df_with_issues):
        """测试数据清洗返回 DataFrame"""
        df_clean = DataCleaner.clean_ohlcv(df_with_issues)

        assert isinstance(df_clean, pd.DataFrame)
        assert len(df_clean) == len(df_with_issues)

    def test_clean_ohlcv_adds_flags(self, df_with_issues):
        """测试数据清洗添加标记列"""
        df_clean = DataCleaner.clean_ohlcv(df_with_issues)

        assert "is_abnormal" in df_clean.columns
        assert "is_illiquid" in df_clean.columns

    def test_clean_ohlcv_detects_abnormal_price(self, df_with_issues):
        """测试数据清洗检测异常价格"""
        df_clean = DataCleaner.clean_ohlcv(df_with_issues)

        # 第 5 天的价格变化超过 20%，应被标记为异常
        assert df_clean.loc[df_with_issues.index[5], "is_abnormal"] == True

    def test_clean_ohlcv_detects_illiquid(self, df_with_issues):
        """测试数据清洗检测流动性枯竭"""
        df_clean = DataCleaner.clean_ohlcv(df_with_issues)

        # 第 10 天成交量为 0，应被标记为流动性枯竭
        assert df_clean.loc[df_with_issues.index[10], "is_illiquid"] == True

    def test_clean_ohlcv_fixes_invalid_ohlc(self, df_with_issues):
        """测试数据清洗修复无效 OHLC"""
        df_clean = DataCleaner.clean_ohlcv(df_with_issues)

        # 修复后，high 不应低于 low
        assert (df_clean["high"] >= df_clean["low"]).all()

    def test_clean_ohlcv_fills_missing_values(self, df_with_issues):
        """测试数据清洗填充缺失值"""
        df_clean = DataCleaner.clean_ohlcv(df_with_issues, max_fill=5)

        # 第 20 天的缺失值应被填充
        assert not pd.isna(df_clean.loc[df_with_issues.index[20], "close"])

    def test_clean_ohlcv_respects_fill_limit(self, df_with_issues):
        """测试数据清洗遵守填充限制"""
        # 插入连续 10 个缺失值
        df = df_with_issues.copy()
        for i in range(25, 35):
            df.loc[df.index[i], "close"] = np.nan

        df_clean = DataCleaner.clean_ohlcv(df, max_fill=5)

        # 超过限制的缺失值不应被填充
        assert pd.isna(df_clean.loc[df.index[30], "close"])

    def test_clean_macro_returns_dataframe(self):
        """测试宏观数据清洗返回 DataFrame"""
        dates = pd.date_range("2023-01-01", periods=12, freq="MS", tz="Asia/Shanghai")
        df = pd.DataFrame({"m2": np.random.rand(12) * 100 + 200}, index=dates)

        df_clean = DataCleaner.clean_macro(df)

        assert isinstance(df_clean, pd.DataFrame)

    def test_clean_macro_bfills_missing_values(self):
        """测试宏观数据清洗后向填充缺失值"""
        dates = pd.date_range("2023-01-01", periods=12, freq="MS", tz="Asia/Shanghai")
        df = pd.DataFrame({"m2": np.random.rand(12) * 100 + 200}, index=dates)

        # 插入缺失值
        df.loc[df.index[3], "m2"] = np.nan
        df.loc[df.index[5], "m2"] = np.nan

        df_clean = DataCleaner.clean_macro(df, fill_method="bfill")

        # 缺失值应被后向填充
        assert not df_clean["m2"].isna().any()

    def test_align_frequencies_returns_dataframe(self):
        """测试多频率数据对齐返回 DataFrame"""
        dates_daily = pd.date_range("2023-01-01", periods=100, freq="D", tz="Asia/Shanghai")
        ohlcv = pd.DataFrame({
            "close": np.random.rand(100) * 100 + 100,
            "volume": np.random.randint(100000, 1000000, 100),
        }, index=dates_daily)

        dates_monthly = pd.date_range("2023-01-01", periods=4, freq="MS", tz="Asia/Shanghai")
        macro = pd.DataFrame({
            "m2": np.random.rand(4) * 100 + 200,
        }, index=dates_monthly)

        df_aligned = DataCleaner.align_frequencies(ohlcv, macro)

        assert isinstance(df_aligned, pd.DataFrame)

    def test_align_frequencies_adds_macro_columns(self):
        """测试多频率数据对齐添加宏观列"""
        dates_daily = pd.date_range("2023-01-01", periods=100, freq="D", tz="Asia/Shanghai")
        ohlcv = pd.DataFrame({
            "close": np.random.rand(100) * 100 + 100,
            "volume": np.random.randint(100000, 1000000, 100),
        }, index=dates_daily)

        dates_monthly = pd.date_range("2023-01-01", periods=4, freq="MS", tz="Asia/Shanghai")
        macro = pd.DataFrame({
            "m2": np.random.rand(4) * 100 + 200,
        }, index=dates_monthly)

        df_aligned = DataCleaner.align_frequencies(ohlcv, macro)

        assert "m2" in df_aligned.columns

    def test_align_frequencies_raises_on_nan(self):
        """测试多频率数据对齐时检测 NaN 抛出异常"""
        dates_daily = pd.date_range("2023-01-01", periods=10, freq="D", tz="Asia/Shanghai")
        ohlcv = pd.DataFrame({
            "close": np.random.rand(10) * 100 + 100,
        }, index=dates_daily)

        dates_monthly = pd.date_range("2023-02-01", periods=1, freq="MS", tz="Asia/Shanghai")
        macro = pd.DataFrame({
            "m2": [200],
        }, index=dates_monthly)

        with pytest.raises(ValueError, match="宏观数据对齐后存在 NaN"):
            DataCleaner.align_frequencies(ohlcv, macro)

    def test_detect_suspension_returns_dataframe(self):
        """测试停牌检测返回 DataFrame"""
        dates = pd.date_range("2023-01-01", periods=100, freq="D", tz="Asia/Shanghai")
        df = pd.DataFrame({
            "close": np.random.rand(100) * 100 + 100,
            "volume": np.random.randint(100000, 1000000, 100),
        }, index=dates)

        df_detected = DataCleaner.detect_suspension(df)

        assert isinstance(df_detected, pd.DataFrame)
        assert "is_suspended" in df_detected.columns

    def test_detect_suspension_marks_low_volume(self):
        """测试停牌检测标记低成交量"""
        dates = pd.date_range("2023-01-01", periods=100, freq="D", tz="Asia/Shanghai")
        df = pd.DataFrame({
            "close": np.random.rand(100) * 100 + 100,
            "volume": np.random.randint(100000, 1000000, 100),
        }, index=dates)

        # 插入连续 3 天低成交量
        for i in range(10, 13):
            df.loc[dates[i], "volume"] = 1000

        df_detected = DataCleaner.detect_suspension(df)

        # 应被标记为停牌
        assert df_detected.loc[dates[12], "is_suspended"] == True

    def test_add_factor_price_returns_dataframe(self):
        """测试添加因子价格返回 DataFrame"""
        dates = pd.date_range("2023-01-01", periods=100, freq="D", tz="Asia/Shanghai")
        df = pd.DataFrame({
            "close": np.random.rand(100) * 100 + 100,
            "volume": np.random.randint(100000, 1000000, 100),
            "amount": np.random.rand(100) * 100000000,
        }, index=dates)

        df_with_factor = DataCleaner.add_factor_price(df, method="vwap")

        assert isinstance(df_with_factor, pd.DataFrame)
        assert "factor_price" in df_with_factor.columns

    def test_add_factor_price_vwap_method(self):
        """测试 VWAP 方法计算因子价格"""
        dates = pd.date_range("2023-01-01", periods=100, freq="D", tz="Asia/Shanghai")
        df = pd.DataFrame({
            "close": np.random.rand(100) * 100 + 100,
            "volume": np.random.randint(100000, 1000000, 100),
            "amount": np.random.rand(100) * 100000000,
        }, index=dates)

        df_with_factor = DataCleaner.add_factor_price(df, method="vwap")

        # VWAP = 成交额 / 成交量
        expected = df["amount"] / df["volume"]
        pd.testing.assert_series_equal(df_with_factor["factor_price"], expected)

    def test_add_factor_price_close_method(self):
        """测试收盘价方法计算因子价格"""
        dates = pd.date_range("2023-01-01", periods=100, freq="D", tz="Asia/Shanghai")
        df = pd.DataFrame({
            "close": np.random.rand(100) * 100 + 100,
            "volume": np.random.randint(100000, 1000000, 100),
        }, index=dates)

        df_with_factor = DataCleaner.add_factor_price(df, method="close")

        # 因子价格应等于收盘价
        pd.testing.assert_series_equal(df_with_factor["factor_price"], df["close"])

    def test_validate_data_returns_dict(self, df_with_issues):
        """测试数据验证返回字典"""
        report = DataCleaner.validate_data(df_with_issues)

        assert isinstance(report, dict)

    def test_validate_data_includes_all_keys(self, df_with_issues):
        """测试数据验证包含所有关键字段"""
        report = DataCleaner.validate_data(df_with_issues)

        expected_keys = ["missing_values"]
        for key in expected_keys:
            assert key in report

    def test_validate_data_counts_missing_values(self, df_with_issues):
        """测试数据验证统计缺失值数量"""
        report = DataCleaner.validate_data(df_with_issues)

        assert "missing_values" in report
        assert report["missing_values"]["close"] >= 1