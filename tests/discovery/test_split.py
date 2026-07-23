# -*- coding: utf-8 -*-
"""L1 二段切分测试：2025/2026 不重叠 + embargo + Segment.covers。"""
from datetime import date


def test_inner_outer_no_overlap():
    """inner 2025 严格在 outer 2026 之前，无重叠（spec §3.3）。"""
    from discovery.split import holdout_split
    s = holdout_split()
    assert s.inner.end < s.outer.start
    assert s.inner.start == date(2025, 1, 1)
    assert s.outer.end == date(2026, 12, 31)


def test_segment_covers():
    """Segment.covers 边界判定（含端点）。"""
    from discovery.split import Segment
    seg = Segment("t", date(2025, 1, 1), date(2025, 12, 31))
    assert seg.covers(date(2025, 6, 15)) is True
    assert seg.covers(date(2025, 1, 1)) is True       # 含左端
    assert seg.covers(date(2026, 1, 1)) is False
    assert seg.covers(date(2024, 12, 31)) is False


def test_segment_covers_timestamp():
    """pandas Timestamp 也能判（scan_symbol 返回的 signal_date 类型）。"""
    import pandas as pd
    from discovery.split import Segment
    seg = Segment("t", date(2025, 1, 1), date(2025, 12, 31))
    assert seg.covers(pd.Timestamp("2025-06-15")) is True


def test_embargo_configurable():
    """embargo_days 可配（吸收 2025→2026 持仓跨越）。"""
    from discovery.split import holdout_split
    assert holdout_split(embargo_days=5).embargo_days == 5
    assert holdout_split(embargo_days=20).embargo_days == 20
