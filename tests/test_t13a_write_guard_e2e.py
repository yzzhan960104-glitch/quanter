# -*- coding: utf-8 -*-
"""T13-A 全链路：重演 T12 抹除场景，验证守卫拒写 + freshness 检测双保险。

三道保险（纵深防御）：
  ① freshness 先建健康基线（sidecar row_count）
  ② 通用同步器试图残片覆盖 → 写入守卫拒写（原湖完好）
  ③ 即便守卫被旁路，freshness 下次检查也检出行数骤降（FAIL + CRITICAL）

物理意图：T12 实证 daily 双轨 + 通用同步器无写入守卫 → 1020万行被残片覆盖（1020万→3200）。
T13-A 三道保险任一独立即可阻断/发现抹除，组合后封死静默抹除路径。
"""
import pandas as pd
import pytest

from data.integrity import WriteGuardError, existing_row_count
from data.freshness import check_freshness


def _big_lake(path, n=10000):
    """造 n 行 daily 湖（100 日期 × (n/100) 标的，覆盖 typical 大湖规模）。"""
    idx = pd.MultiIndex.from_product(
        [pd.date_range("2026-01-01", periods=100),
         [f"{i:06d}.SZ" for i in range(max(1, n // 100))]],
        names=["date", "symbol"])
    pd.DataFrame({"close": range(len(idx))}, index=idx).to_parquet(path)


def test_t12_scenario_blocked_and_detected(tmp_path, monkeypatch):
    """重演 T12：大湖被残片试图覆盖 → 守卫拒写（原湖完好），freshness 检测骤降。"""
    lake = tmp_path / "a_shares_daily.parquet"
    _big_lake(str(lake))  # 现有大湖（10000 行）

    # ① 第一道保险：freshness 先建健康基线（sidecar row_count=10000）
    check_freshness("daily", "2026-04-09", lake_dir=str(tmp_path))

    # ② 第二道保险：通用同步器试图残片覆盖 → 守卫拒写
    from data import tushare_sync
    crater = pd.DataFrame({"trade_date": ["20260409"], "ts_code": ["000001.SZ"],
                           "close": [1.0]})
    monkeypatch.setattr(tushare_sync, "_fetch_with_guard",
                        lambda api, **kw: crater)
    with pytest.raises(WriteGuardError):
        tushare_sync._sync_single("daily", "daily", None, "trade_date", str(lake),
                                  cfg={"api": "daily", "by": "single",
                                       "date_col": "trade_date", "symbol_col": "ts_code"})
    # 拒写后原湖完好（行数不变）
    assert existing_row_count(str(lake)) == 10000

    # ③ 第三道保险：即便守卫被旁路（人为直接覆盖成小湖），freshness 下次检查也检出骤降
    _big_lake(str(lake), n=100)  # 强行模拟覆盖后的残片小湖（100 行）
    r = check_freshness("daily", "2026-04-09", lake_dir=str(tmp_path))
    assert r.ok is False  # 行数骤降被检出（100 << 基线 10000 × 0.9）
