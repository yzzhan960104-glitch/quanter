# -*- coding: utf-8 -*-
"""task_export 测试:mock freeze/holdout_split/git/哈希,断言 task.json 字段 + trial_id 一致。"""
import json
from datetime import date
from types import SimpleNamespace

from compute_unit import task_export


def _fake_meta():
    return SimpleNamespace(
        snapshot_hash="snap1234abcd5678", universe_def="创板科创",
        universe_count=1334, date_range="2025-01-01~2026-07-25", lake_start="2025-01-01",
    )


def test_export_task_fields(monkeypatch, tmp_path):
    """export_task 写 task.json:字段完整 + trial_id == trial_id_of 算(C8 权威)。"""
    from discovery.store import trial_id_of
    from discovery.split import Segment, HoldoutSplit

    # mock freeze(返合成 meta,不读真实 parquet)
    monkeypatch.setattr("discovery.snapshot.freeze", lambda lake_start="x": ({}, _fake_meta()))
    # mock holdout_split(返固定 split)
    monkeypatch.setattr("discovery.split.holdout_split", lambda embargo_days=5: HoldoutSplit(
        inner=Segment("inner_2025", date(2025, 1, 1), date(2025, 12, 31)),
        outer=Segment("outer_2026", date(2026, 1, 1), date(2026, 12, 31)), embargo_days=5))
    # mock 三件哈希
    monkeypatch.setattr(task_export, "_git_head_sha", lambda: "a"*40)
    monkeypatch.setattr(task_export, "_engine_hash", lambda: "abc123def456")
    monkeypatch.setattr(task_export, "_file_sha256", lambda p: "b"*64)

    params = [{"window": 80, "min_touches": 2}]
    out = tmp_path / "task.json"
    task = task_export.export_task(params, out_path=out)

    # 三件哈希字段
    assert task.git_commit == "a"*40
    assert task.engine_hash == "abc123def456"
    assert task.parquet_sha256 == "b"*64
    # snapshot meta 透传
    assert task.snapshot_meta.universe_count == 1334
    assert task.snapshot_meta.snapshot_hash == "snap1234abcd5678"
    # trial_id == trial_id_of 算(C8 权威来源,Mac 不算)
    assert task.trials[0].trial_id == trial_id_of(params[0], "snap1234abcd5678", 42)
    # json 文件写出且可读回(ensure_ascii=False 中文保真)
    raw = json.loads(out.read_text(encoding="utf-8"))
    assert raw["task_id"] == task.task_id
    assert raw["trials"][0]["params"] == params[0]
    assert raw["snapshot_meta"]["universe_def"] == "创板科创"
