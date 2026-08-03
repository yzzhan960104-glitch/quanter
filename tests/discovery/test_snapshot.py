# -*- coding: utf-8 -*-
"""L0 快照冻结测试：纯函数优先（快），freeze 集成标 slow。"""
import pandas as pd
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


def test_load_universe_reads_with_date_filter(monkeypatch):
    """回归（2026-08-03 资源优化）：read_parquet 必须带 filters（date>=start）。

    旧实现先全量读 1019 万行再筛 → 每 worker ~1.3GB；12 worker 并发加载是
    内存峰值根因。filter 推到读层后 2025 起只读 ~200 万行（~0.3GB）。
    """
    from discovery import snapshot
    calls = {}

    def fake_read(path, **kwargs):
        calls.update(kwargs)
        idx = pd.MultiIndex.from_tuples(
            [(pd.Timestamp("2025-01-02"), "300001.SZ"),
             (pd.Timestamp("2025-01-03"), "300001.SZ"),
             (pd.Timestamp("2025-01-02"), "600000.SH")],
            names=["date", "symbol"],
        )
        return pd.DataFrame({
            "open": [10.0, 11.0, 20.0],
            "high": [11.0, 12.0, 21.0],
            "low": [9.0, 10.0, 19.0],
            "close": [10.5, 11.5, 20.5],
            "volume": [1000.0, 1100.0, 2000.0],
            "amount": [1e5, 1.1e5, 2e5],
        }, index=idx)

    monkeypatch.setattr(snapshot.pd, "read_parquet", fake_read)
    universe = snapshot.load_universe(start="2025-01-01")
    assert "filters" in calls
    assert calls["filters"] == [("date", ">=", pd.Timestamp("2025-01-01"))]
    # 创板科创 + 流动性过滤后只剩 300001.SZ
    assert set(universe.keys()) == {"300001.SZ"}


@pytest.mark.slow
def test_freeze_loads_real_universe():
    """集成：freeze 真实加载（~5s），返回非空 universe + 元数据。需 data_lake。"""
    from discovery.snapshot import freeze
    universe, meta = freeze()
    assert len(universe) > 100                       # 创板科创应有数百只
    assert meta.universe_count == len(universe)
    assert len(meta.snapshot_hash) == 16
    assert "2025" in meta.date_range
