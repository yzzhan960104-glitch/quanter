# -*- coding: utf-8 -*-
"""protocol.py 序列化往返测试:date↔str、嵌套 split/snapshot/trials、三种 status。"""
from datetime import date

from compute_unit.protocol import (
    Task, Result, TrialSpec, TrialResult, SegmentSpec, SplitSpec, SnapshotMetaSpec,
)


def _sample_task():
    """构造一个完整 Task 样本(覆盖所有字段)。"""
    return Task(
        protocol_version=1, task_id="a1b2c3d4", created_at="2026-07-26T12:00:00",
        git_commit="a" * 40, engine_hash="abc123def456", parquet_sha256="b" * 64,
        lake_start="2025-01-01", embargo_days=5,
        snapshot_meta=SnapshotMetaSpec(
            snapshot_hash="snap1234abcd5678", universe_def="创板科创",
            universe_count=1334, date_range="2025-01-01~2026-07-25", lake_start="2025-01-01",
        ),
        split=SplitSpec(
            inner=SegmentSpec("inner_2025", date(2025, 1, 1), date(2025, 12, 31)),
            outer=SegmentSpec("outer_2026", date(2026, 1, 1), date(2026, 12, 31)),
            embargo_days=5,
        ),
        trials=[TrialSpec(trial_id="tid000000001", params={"window": 80},
                          source="discovery_search", seed=42)],
    )


def test_task_roundtrip(tmp_path):
    """Task → json → Task 字段全等(date 还原为 date 对象,非 str)。"""
    t = _sample_task()
    out = tmp_path / "task.json"
    t.to_json(out)
    t2 = Task.from_json(out)
    assert t2.task_id == t.task_id
    assert t2.git_commit == t.git_commit
    assert t2.snapshot_meta.universe_count == 1334
    assert t2.snapshot_meta.universe_def == "创板科创"          # 中文 ensure_ascii=False 保真
    assert t2.split.inner.start == date(2025, 1, 1)             # date 对象,非 str
    assert isinstance(t2.split.inner.start, date)
    assert t2.split.embargo_days == 5
    assert t2.trials[0].trial_id == "tid000000001"
    assert t2.trials[0].params == {"window": 80}
    assert t2.trials[0].seed == 42


def test_result_roundtrip(tmp_path):
    """Result → json → Result,含 failed/degenerate/ok 三种 status。"""
    r = Result(
        task_id="a1b2c3d4", git_commit="a" * 40, parquet_sha256="b" * 64,
        ran_at="2026-07-26T20:00:00",
        results=[
            TrialResult(trial_id="ok1", status="ok",
                        inner={"n": 10, "calmar": 5.0}, outer={"n": 8}, n_total=18),
            TrialResult(trial_id="fail1", status="failed", error="KeyError"),
            TrialResult(trial_id="deg1", status="degenerate", n_total=0),
        ],
    )
    out = tmp_path / "result.json"
    r.to_json(out)
    r2 = Result.from_json(out)
    assert len(r2.results) == 3
    assert r2.results[0].inner["calmar"] == 5.0
    assert r2.results[1].status == "failed" and r2.results[1].error == "KeyError"
    assert r2.results[2].status == "degenerate" and r2.results[2].n_total == 0
