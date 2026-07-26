# -*- coding: utf-8 -*-
"""env_check 校验测试:三件哈希漂移分支 + snapshot 双校验分支 + verify_and_freeze 串联。

mock git/文件 sha(不依赖真实 parquet/git),各漂移分支断言抛 EnvDriftError。
"""
from datetime import date
from types import SimpleNamespace

import pytest

from compute_unit import env_check
from compute_unit.env_check import EnvDriftError
from compute_unit.protocol import Task, SnapshotMetaSpec, SplitSpec, SegmentSpec


def _task_with(git_commit="a"*40, engine_hash="abc123def456", parquet_sha256="b"*64,
               universe_count=1334, date_range="2025-01-01~2026-07-25"):
    """构造 Task,允许覆盖各哈希/snapshot 字段(测漂移分支)。"""
    return Task(
        protocol_version=1, task_id="t1", created_at="x", git_commit=git_commit,
        engine_hash=engine_hash, parquet_sha256=parquet_sha256,
        lake_start="2025-01-01", embargo_days=5,
        snapshot_meta=SnapshotMetaSpec("snap1234abcd5678", "创板科创", universe_count,
                                       date_range, "2025-01-01"),
        split=SplitSpec(SegmentSpec("i", date(2025, 1, 1), date(2025, 12, 31)),
                        SegmentSpec("o", date(2026, 1, 1), date(2026, 12, 31)), 5),
        trials=[],
    )


def test_check_hashes_git_drift(monkeypatch):
    """git_commit 不符 → EnvDriftError。"""
    monkeypatch.setattr(env_check, "_git_head_sha", lambda: "z"*40)
    monkeypatch.setattr(env_check, "_engine_hash", lambda: "abc123def456")
    monkeypatch.setattr(env_check, "_file_sha256", lambda p: "b"*64)
    with pytest.raises(EnvDriftError, match="git_commit"):
        env_check._check_hashes(_task_with(git_commit="a"*40))


def test_check_hashes_engine_drift(monkeypatch):
    """engine_hash 不符 → EnvDriftError。"""
    monkeypatch.setattr(env_check, "_git_head_sha", lambda: "a"*40)
    monkeypatch.setattr(env_check, "_engine_hash", lambda: "XXXXXXXXXXXX")
    monkeypatch.setattr(env_check, "_file_sha256", lambda p: "b"*64)
    with pytest.raises(EnvDriftError, match="engine_hash"):
        env_check._check_hashes(_task_with(engine_hash="abc123def456"))


def test_check_hashes_parquet_drift(monkeypatch):
    """parquet_sha256 不符 → EnvDriftError。"""
    monkeypatch.setattr(env_check, "_git_head_sha", lambda: "a"*40)
    monkeypatch.setattr(env_check, "_engine_hash", lambda: "abc123def456")
    monkeypatch.setattr(env_check, "_file_sha256", lambda p: "Z"*64)
    with pytest.raises(EnvDriftError, match="parquet"):
        env_check._check_hashes(_task_with(parquet_sha256="b"*64))


def test_check_hashes_all_match(monkeypatch):
    """三件全匹配 → 不抛(None)。"""
    monkeypatch.setattr(env_check, "_git_head_sha", lambda: "a"*40)
    monkeypatch.setattr(env_check, "_engine_hash", lambda: "abc123def456")
    monkeypatch.setattr(env_check, "_file_sha256", lambda p: "b"*64)
    env_check._check_hashes(_task_with())   # 不抛即通过


def test_check_snapshot_count_drift():
    """universe_count 不符 → EnvDriftError。"""
    meta = SimpleNamespace(universe_count=999, date_range="2025-01-01~2026-07-25")
    with pytest.raises(EnvDriftError, match="universe_count"):
        env_check._check_snapshot(_task_with(universe_count=1334), meta)


def test_check_snapshot_date_range_drift():
    """date_range 不符 → EnvDriftError。"""
    meta = SimpleNamespace(universe_count=1334, date_range="2025-01-01~2026-01-01")
    with pytest.raises(EnvDriftError, match="date_range"):
        env_check._check_snapshot(_task_with(date_range="2025-01-01~2026-07-25"), meta)


def test_verify_and_freeze_chains(monkeypatch):
    """verify_and_freeze = _check_hashes + freeze + _check_snapshot,返回 (universe, meta)。"""
    calls = []
    monkeypatch.setattr(env_check, "_check_hashes", lambda t: calls.append("hashes"))
    monkeypatch.setattr(env_check, "_check_snapshot", lambda t, m: calls.append("snapshot"))
    fake_meta = SimpleNamespace(universe_count=1334, date_range="2025-01-01~2026-07-25")
    monkeypatch.setattr("discovery.snapshot.freeze", lambda lake_start="x": ({"u": 1}, fake_meta))
    universe, meta = env_check.verify_and_freeze(_task_with())
    assert calls == ["hashes", "snapshot"]   # 串联顺序
    assert universe == {"u": 1}               # 返回 freeze 的 universe 供 runner 复用
