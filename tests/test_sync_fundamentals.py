"""sync_fundamentals 数据同步层单测（mock Tushare，不依赖网络/积分）。

注：原文件含 factors/fundamental 截面因子测试，蔡森专精化 Phase 1 Task 3 删除 factors 体系后
整段移除——保留纯数据同步（面板合并/数值 coerce/熔断不计分）部分，因子计算测试随 factors 一同下线。
"""
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
