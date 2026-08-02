# -*- coding: utf-8 -*-
"""端到端等价测试(C6 红线):_eval_batch → summarize 全链路与 discovery 直跑等价。

注入合成 universe(不依赖真实 parquet,快)。task_export 的 freeze 集成在 test_task_export
已测,本测试聚焦「跑批 + 摘要」链路与 discovery 的等价——compute_unit 不能偷偷改口径。
"""
from datetime import date

from compute_unit.protocol import (
    Task, TrialSpec, SnapshotMetaSpec, SplitSpec, SegmentSpec, Result,
)
from compute_unit.runner import _eval_batch
from compute_unit.summary import summarize
from discovery.objective import evaluate
from discovery.split import holdout_split


def _task(trial_id, params):
    """构造含 1 trial 的 Task(手工填字段,跳过 task_export 的 freeze)。"""
    return Task(
        protocol_version=1, task_id="e2e", created_at="x", git_commit="a"*40,
        engine_hash="x", parquet_sha256="x",
        lake_start="2025-01-01", embargo_days=5,
        snapshot_meta=SnapshotMetaSpec("s", "u", 1, "dr", "2025-01-01"),
        split=SplitSpec(SegmentSpec("i", date(2025, 1, 1), date(2025, 12, 31)),
                        SegmentSpec("o", date(2026, 1, 1), date(2026, 12, 31)), 5),
        trials=[TrialSpec(trial_id, params, "discovery_search", 42)],
    )


def test_e2e_eval_equivalent(fixed_params, synth_universe):
    """_eval_batch(synth_universe) inner/outer/n_total == evaluate 直跑(逐字段,红线 C6)。"""
    task = _task("e2e_tid", fixed_params)
    results = _eval_batch(task, synth_universe, task.split, n_proc=1)
    direct = evaluate(fixed_params, synth_universe, holdout_split())
    assert results[0].status == "ok"
    assert results[0].inner == direct["inner"]
    assert results[0].outer == direct["outer"]
    assert results[0].n_total == direct["n_total"]


def test_e2e_summary_runs(fixed_params, synth_universe):
    """跑批 → 拼 Result → summarize 跑通(不抛,含标题 + trial_id)。"""
    task = _task("e2e2_tid", fixed_params)
    results = _eval_batch(task, synth_universe, task.split, n_proc=1)
    result = Result(task_id="e2e2", git_commit="a"*40, parquet_sha256="x",
                    ran_at="x", results=results)
    out = summarize(result, top_n=3)
    assert "Mac 计算单元" in out
    assert "e2e2_tid" in out


def test_e2e_replay_mode_equivalent(fixed_params, synth_universe):
    """v2 replay 模式：_eval_batch(replay task) 与 evaluate_replay 直跑逐字段相等（C6 同款）。"""
    from compute_unit.runner import _eval_batch
    from discovery.objective import evaluate_replay
    from discovery.split import holdout_split

    task = _task("e2e_rp", fixed_params)
    task.mode = "replay"
    results = _eval_batch(task, synth_universe, task.split, n_proc=1)
    direct = evaluate_replay(fixed_params, synth_universe, holdout_split())
    if direct["n_total"] > 0:
        assert results[0].status == "ok"
        assert results[0].inner == direct["inner"]
        assert results[0].outer == direct["outer"]
        assert results[0].n_total == direct["n_total"]
    else:
        assert results[0].status == "degenerate"
        assert results[0].n_total == 0
