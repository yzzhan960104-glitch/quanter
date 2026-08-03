# -*- coding: utf-8 -*-
"""回测 worker 内存优化单测（2026-08-03）。

覆盖：_replay_lake_min_date 的 env 覆盖与默认值（3 年滑动窗口）——
回测 worker 用 date_min 加载 daily 湖，避免把 10 年全量湖搬进内存。
"""


def test_replay_lake_min_date_default_is_three_years_back(monkeypatch):
    """默认起点 = 今天往前 3 年（策略 window+ATR 预热足够，全量湖内存降 ~70%）。"""
    from datetime import date, timedelta

    from backtest import worker
    monkeypatch.delenv("REPLAY_LAKE_MIN_DATE", raising=False)
    expected = (date.today() - timedelta(days=3 * 365)).isoformat()
    assert worker._replay_lake_min_date() == expected


def test_replay_lake_min_date_env_override(monkeypatch):
    """老窗口回测被截断时可用 env 显式放宽起点。"""
    from backtest import worker
    monkeypatch.setenv("REPLAY_LAKE_MIN_DATE", "2019-01-01")
    assert worker._replay_lake_min_date() == "2019-01-01"
