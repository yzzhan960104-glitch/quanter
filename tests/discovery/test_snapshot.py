# -*- coding: utf-8 -*-
"""L0 快照冻结测试：纯函数优先（快），freeze 集成标 slow。"""
import pytest


def test_snapshot_hash_deterministic():
    """同输入 → 同 hash（可复现性基石，spec ADR3）。"""
    from discovery.snapshot import snapshot_hash
    h1 = snapshot_hash(1334, "2025-01-01~2026-07-23", "2025-01-01")
    h2 = snapshot_hash(1334, "2025-01-01~2026-07-23", "2025-01-01")
    assert h1 == h2


def test_snapshot_hash_differs_on_count():
    """universe 数量变 → hash 变（探查实证的 1334→1332 必须能检出）。"""
    from discovery.snapshot import snapshot_hash
    h1 = snapshot_hash(1334, "2025-01-01~2026-07-23", "2025-01-01")
    h2 = snapshot_hash(1332, "2025-01-01~2026-07-23", "2025-01-01")
    assert h1 != h2


def test_snapshot_hash_differs_on_date_range():
    """日期范围变（数据湖增量落湖）→ hash 变。"""
    from discovery.snapshot import snapshot_hash
    h1 = snapshot_hash(1334, "2025-01-01~2026-07-23", "2025-01-01")
    h2 = snapshot_hash(1334, "2025-01-01~2026-07-30", "2025-01-01")
    assert h1 != h2


@pytest.mark.slow
def test_freeze_loads_real_universe():
    """集成：freeze 真实加载（~5s），返回非空 universe + 元数据。需 data_lake。"""
    from discovery.snapshot import freeze
    universe, meta = freeze()
    assert len(universe) > 100                       # 创板科创应有数百只
    assert meta.universe_count == len(universe)
    assert len(meta.snapshot_hash) == 16
    assert "2025" in meta.date_range
