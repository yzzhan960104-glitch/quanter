"""sync_fundamentals + factors/fundamental 单测（mock Tushare + 临时湖，不依赖网络/积分）。"""
import pandas as pd
import pytest

from scripts.sync_fundamentals import build_fundamentals_panel, fetch_valuation_panel


# ---------------- sync_fundamentals ----------------

def test_build_fundamentals_panel_merges_to_multiindex():
    """合并逐日面板 → MultiIndex(date, symbol)，date 为 datetime。"""
    panels = [
        pd.DataFrame({"ts_code": ["600000.SH", "600010.SH"], "trade_date": ["20240102", "20240102"],
                      "pe": [10.0, 20.0], "pe_ttm": [9.5, 19.5], "total_mv": [1000, 2000]}),
        pd.DataFrame({"ts_code": ["600000.SH", "600010.SH"], "trade_date": ["20240103", "20240103"],
                      "pe": [11.0, 21.0], "pe_ttm": [10.5, 20.5], "total_mv": [1010, 2020]}),
    ]
    big = build_fundamentals_panel(panels)
    assert isinstance(big.index, pd.MultiIndex)
    assert big.index.names == ["date", "symbol"]
    assert pd.api.types.is_datetime64_any_dtype(big.index.get_level_values("date"))
    assert big.loc[("2024-01-02", "600000.SH"), "pe"] == 10.0
    assert big.loc[("2024-01-03", "600010.SH"), "pe_ttm"] == 20.5


def test_build_fundamentals_panel_raises_on_empty():
    with pytest.raises(RuntimeError, match="基本面面板为空"):
        build_fundamentals_panel([])


def test_build_fundamentals_panel_coerces_strings_and_none():
    """Tushare 偶返回字符串/None → coerce 成数值/NaN（不崩落盘）。"""
    panels = [pd.DataFrame({"ts_code": ["600000.SH"], "trade_date": ["20240102"],
                            "pe": ["10.5"], "pe_ttm": [None]})]
    big = build_fundamentals_panel(panels)
    assert big["pe"].iloc[0] == 10.5
    assert pd.isna(big["pe_ttm"].iloc[0])


def test_fetch_valuation_panel_calls_daily_basic_with_trade_date():
    """fetch_valuation_panel 调 pro.daily_basic(trade_date=...)，返回非空透传。"""
    from data.resilience import CircuitState, tushare_breaker
    tushare_breaker._state = CircuitState.CLOSED
    tushare_breaker._failure_count = 0

    fake_df = pd.DataFrame({"ts_code": ["600000.SH"], "trade_date": ["20240102"], "pe": [10.0]})

    class _FakePro:
        def __init__(self):
            self.calls = []

        def daily_basic(self, **kwargs):
            self.calls.append(kwargs)
            return fake_df

    try:
        pro = _FakePro()
        df = fetch_valuation_panel(pro, "20240102")
        assert len(df) == 1
        assert pro.calls[0]["trade_date"] == "20240102"
    finally:
        tushare_breaker._state = CircuitState.CLOSED
        tushare_breaker._failure_count = 0


def test_fetch_valuation_panel_quota_error_returns_empty_no_breaker():
    """积分不足（持久态）→ 返空 DF，不计熔断（与 fetch_qfq 范式一致）。"""
    from data.resilience import CircuitState, tushare_breaker
    tushare_breaker._state = CircuitState.CLOSED
    tushare_breaker._failure_count = 0

    class _QuotaPro:
        def daily_basic(self, **kwargs):
            raise Exception("对不起, 您的积分不足访问 daily_basic 接口")

    try:
        df = fetch_valuation_panel(_QuotaPro(), "20240102")
        assert df.empty
        assert tushare_breaker._failure_count == 0  # 持久态不计熔断
    finally:
        tushare_breaker._state = CircuitState.CLOSED
        tushare_breaker._failure_count = 0


# ---------------- factors/fundamental ----------------

@pytest.fixture
def fundamentals_lake(tmp_path):
    """注入临时 fundamentals 湖（用 reader.load 正确初始化 _lakes + _ffills + _dtypes）。"""
    from data.lake_reader import DataLakeReader
    reader = DataLakeReader.get_instance()
    reader._lakes.clear()
    reader._ffills.clear()
    reader._dtypes.clear()
    reader._default_key = None

    # 4 只标的的 pe_ttm 截面（A=10 最低，D=40 最高）
    df = pd.DataFrame(
        {"pe_ttm": [10, 20, 30, 40]},
        index=pd.MultiIndex.from_tuples(
            [("2024-01-02", "A"), ("2024-01-02", "B"),
             ("2024-01-02", "C"), ("2024-01-02", "D")],
            names=["date", "symbol"],
        ),
    )
    path = tmp_path / "fund.parquet"
    df.to_parquet(path)
    reader.load(str(path), key="fundamentals")  # 正确建 _lakes + _ffills + _dtypes

    yield reader
    reader._lakes.clear()
    reader._ffills.clear()
    reader._dtypes.clear()
    reader._default_key = None


def test_valuation_cross_section_value_direction(fundamentals_lake):
    """价值方向：低 pe → 高分（A=10 最低 → 最高分）。"""
    from factors.fundamental import valuation_cross_section
    rank = valuation_cross_section("2024-01-02", "pe_ttm", direction="value")
    assert rank["A"] > rank["B"] > rank["C"] > rank["D"]
    assert 0.0 <= rank.min() <= rank.max() <= 1.0


def test_valuation_cross_section_growth_direction(fundamentals_lake):
    """成长方向：高 pe → 高分（D=40 最高 → 最高分）。"""
    from factors.fundamental import valuation_cross_section
    rank = valuation_cross_section("2024-01-02", "pe_ttm", direction="growth")
    assert rank["D"] > rank["A"]


def test_valuation_cross_section_missing_lake_returns_empty():
    """fundamentals 湖未加载 → 返空 Series（离线降级，不抛）。"""
    from data.lake_reader import DataLakeReader
    reader = DataLakeReader.get_instance()
    reader._lakes.clear()
    reader._ffills.clear()
    reader._dtypes.clear()
    reader._default_key = None

    from factors.fundamental import valuation_cross_section
    rank = valuation_cross_section("2024-01-02", "pe_ttm")
    assert rank.empty
