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


def test_walk_forward_split_structure():
    """P5：4 折锚定结构（train→次年 oos）+ 终局 2026；wf4 = 二段 holdout 口径（交叉验证锚）。"""
    from discovery.split import walk_forward_split
    wf = walk_forward_split(embargo_days=5)
    assert [name for name, _, _ in wf.folds] == \
        ["wf1_2020_21", "wf2_2022_23", "wf3_2024", "wf4_2025"]
    # 每折 oos 紧随 train 次年（经典锚定：train 段末 + 1 天 == oos 段始）
    for _name, train, oos in wf.folds:
        assert oos.start.year == train.end.year + 1
        assert oos.end.year == oos.start.year
    assert wf.final_oos.name == "oos_2026"
    # wf4 与二段 holdout 同口径（train 2025 / oos 2026）——交叉验证一致性锚
    t4, o4 = wf.folds[3][1], wf.folds[3][2]
    assert (t4.start.year, t4.end.year) == (2025, 2025)
    assert (o4.start.year, o4.end.year) == (2026, 2026)
    assert wf.embargo_days == 5
