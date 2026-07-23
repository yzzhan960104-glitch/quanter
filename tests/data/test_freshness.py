# -*- coding: utf-8 -*-
"""数据实时性检查核心：期望日 vs 数据湖最新日比对。"""
from data.freshness import check_freshness, FreshnessResult


def _make_daily_parquet(tmp_path, last_date):
    """造一个 a_shares_daily 风格 parquet（MultiIndex date,symbol），最新日 = last_date。"""
    import pandas as pd
    dates = pd.date_range("2026-07-01", last_date, freq="B")
    df = pd.DataFrame({
        "date": dates.tolist() * 2,
        "symbol": ["000001.SZ"] * len(dates) + ["000002.SZ"] * len(dates),
        "open": 10.0, "close": 10.0, "high": 10.0, "low": 10.0, "volume": 1000, "amount": 10000.0,
    })
    df = df.set_index(["date", "symbol"])
    p = tmp_path / "a_shares_daily.parquet"
    df.to_parquet(p)
    return p


def test_freshness_pass_when_latest_meets_expected(tmp_path):
    """数据湖最新日 >= 期望日 → PASS。"""
    _make_daily_parquet(tmp_path, "2026-07-23")
    r = check_freshness("daily", "2026-07-23", lake_dir=str(tmp_path))
    assert r.ok is True
    assert r.latest_date == "2026-07-23"


def test_freshness_fail_when_latest_stale(tmp_path):
    """数据湖最新日 < 期望日 → FAIL（T 日数据未落湖）。"""
    _make_daily_parquet(tmp_path, "2026-07-22")
    r = check_freshness("daily", "2026-07-23", lake_dir=str(tmp_path))
    assert r.ok is False
    assert "2026-07-22" in r.message


def test_freshness_fail_when_parquet_missing(tmp_path):
    """parquet 不存在 → FAIL（不猜、不崩）。"""
    r = check_freshness("daily", "2026-07-23", lake_dir=str(tmp_path))
    assert r.ok is False
    assert "缺失" in r.message or "不存在" in r.message
