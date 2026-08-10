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


def test_freshness_fail_when_parquet_corrupt(tmp_path):
    """parquet 内容损坏（读异常）→ 被 except 兜住返 FAIL，不抛异常（review I1）。"""
    # 物理意图：模拟落湖文件被截断/半写坏，read_parquet 必然抛异常；
    # 防御点：除零/脏数据/IO 错误都不得击穿检查器，必须降级为 FAIL + None + 告警 message。
    (tmp_path / "a_shares_daily.parquet").write_bytes(b"not a parquet")
    r = check_freshness("daily", "2026-07-23", lake_dir=str(tmp_path))
    assert r.ok is False
    assert r.latest_date is None
    assert "异常" in r.message  # 命中「读最新日期异常」分支


def test_freshness_pass_when_single_level_datetimeindex(tmp_path):
    """单级 DatetimeIndex（非 MultiIndex）→ 走 fallback dates=idx 分支正确取最新日（review M1）。"""
    # 物理意图：某些上游落湖可能不 set_index 多列，仅以 DatetimeIndex 为索引；
    # fallback 分支（else: dates = idx）须保证该形态仍能取到 max date，不能只认 MultiIndex。
    import pandas as pd
    dates = pd.date_range("2026-07-01", "2026-07-23", freq="B")
    df = pd.DataFrame({"close": 10.0}, index=dates)  # 单级 DatetimeIndex
    df.to_parquet(tmp_path / "a_shares_daily.parquet")
    r = check_freshness("daily", "2026-07-23", lake_dir=str(tmp_path))
    assert r.ok is True
    assert r.latest_date == "2026-07-23"


# ============================================================================
# 行数骤降维度（T13-A · Task 6）
# ============================================================================
# 物理意图：freshness 只比 max-date 会被「刚重写但内容是残片」骗过（T12 单日湖 max-date
# 仍是今天 → 误判 PASS）。本组加行数骤降检测：与 sidecar 基线（上次健康行数）环比，
# 骤降则 FAIL + CRITICAL。基线仅健康时更新（防骤降被基线掩盖）。


def _write_daily_lake(lake_dir, n_rows):
    """造 n_rows 行的 daily 湖（date×symbol 笛卡尔积截断到 n_rows）。"""
    import pandas as pd
    side = max(1, n_rows // 10)
    idx = pd.MultiIndex.from_product(
        [pd.date_range("2026-08-01", periods=side),
         [f"{i:06d}.SZ" for i in range(side)]],
        names=["date", "symbol"])[:n_rows]
    pd.DataFrame({"close": range(len(idx))}, index=idx).to_parquet(
        lake_dir / "a_shares_daily.parquet")


def test_freshness_records_row_count(tmp_path):
    """健康检查结果记录当前行数（骤降检测的基础字段）。"""
    _write_daily_lake(tmp_path, 500)
    r = check_freshness("daily", "2026-08-01", lake_dir=str(tmp_path))
    assert r.row_count == 500


def test_freshness_fails_on_row_count_crater(tmp_path):
    """基线 10000 → 当前 100（骤降）：freshness 应 FAIL（即便 max-date 仍 >= 期望）。"""
    _write_daily_lake(tmp_path, 10000)
    check_freshness("daily", "2026-08-01", lake_dir=str(tmp_path))  # 建基线
    _write_daily_lake(tmp_path, 100)  # 模拟残片覆盖
    r = check_freshness("daily", "2026-08-01", lake_dir=str(tmp_path))
    assert r.ok is False
    assert "骤降" in r.message or "行数" in r.message


def test_freshness_baseline_only_updates_on_healthy(tmp_path):
    """基线只在健康（非骤降）时更新：骤降检查不应把基线拉低（防被掩盖）。"""
    import json
    _write_daily_lake(tmp_path, 10000)
    check_freshness("daily", "2026-08-01", lake_dir=str(tmp_path))
    _write_daily_lake(tmp_path, 100)
    check_freshness("daily", "2026-08-01", lake_dir=str(tmp_path))  # 骤降，不更新基线
    baseline = json.loads((tmp_path / ".freshness_baseline.json").read_text(encoding="utf-8"))
    assert baseline["daily"]["row_count"] == 10000
