# -*- coding: utf-8 -*-
"""runner 测试:_eval_one/_eval_batch 与 discovery.objective.evaluate 逐字段等价(C6 红线)。

_eval_one 同进程(mock 可生效);_eval_batch 走 spawn Pool(子进程跑真实 evaluate,与父进程
直跑等价)。注入合成 universe,不依赖真实 parquet,快。
"""
from datetime import date

from compute_unit.protocol import Task, TrialSpec, SnapshotMetaSpec, SplitSpec, SegmentSpec


def _task_for(trial_id, params, seed=42):
    """构造含 1 trial 的 Task(供 _eval_batch / _eval_one)。"""
    return Task(
        protocol_version=1, task_id="t1", created_at="x", git_commit="a"*40,
        engine_hash="x", parquet_sha256="x",
        lake_start="2025-01-01", embargo_days=5,
        snapshot_meta=SnapshotMetaSpec("s", "u", 1, "dr", "2025-01-01"),
        split=SplitSpec(SegmentSpec("inner_2025", date(2025, 1, 1), date(2025, 12, 31)),
                        SegmentSpec("outer_2026", date(2026, 1, 1), date(2026, 12, 31)), 5),
        trials=[TrialSpec(trial_id=trial_id, params=params, source="discovery_search", seed=seed)],
    )


def test_eval_one_equivalent(fixed_params, synth_universe):
    """红线 C6:_eval_one(trial, universe, split) inner/outer/n_total == evaluate 直跑。"""
    from compute_unit.runner import _eval_one
    from discovery.objective import evaluate
    from discovery.split import holdout_split
    split = holdout_split()
    trial = TrialSpec("tid_eq", fixed_params, "discovery_search", 42)
    r = _eval_one(trial, synth_universe, split)
    direct = evaluate(fixed_params, synth_universe, split)
    assert r.status == "ok"
    assert r.inner == direct["inner"]       # 逐字段相等(红线)
    assert r.outer == direct["outer"]
    assert r.n_total == direct["n_total"]


def test_eval_one_failed(monkeypatch, fixed_params, synth_universe):
    """单组 evaluate 异常 → status=failed(同进程 mock 生效)。"""
    from compute_unit import runner
    from compute_unit.protocol import TrialSpec
    from discovery.split import holdout_split
    monkeypatch.setattr("discovery.objective.evaluate",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    trial = TrialSpec("tid_fail", fixed_params, "discovery_search", 42)
    r = runner._eval_one(trial, synth_universe, holdout_split())
    assert r.status == "failed"
    assert "boom" in r.error


def test_eval_one_degenerate(monkeypatch, fixed_params, synth_universe):
    """n_total==0(退化)→ status=degenerate。"""
    from compute_unit import runner
    from compute_unit.protocol import TrialSpec
    from discovery.split import holdout_split
    monkeypatch.setattr("discovery.objective.evaluate",
                        lambda p, u, s: {"inner": {}, "outer": {}, "n_total": 0})
    trial = TrialSpec("tid_deg", fixed_params, "discovery_search", 42)
    r = runner._eval_one(trial, synth_universe, holdout_split())
    assert r.status == "degenerate"
    assert r.n_total == 0


def test_eval_batch_equivalent(fixed_params, synth_universe):
    """_eval_batch(spawn Pool,真实 evaluate)inner == 父进程 evaluate 直跑(红线 C6)。

    子进程跑真实 evaluate(不 mock),与父进程同 universe 同 split,结果必然一致。
    """
    from compute_unit.runner import _eval_batch
    from discovery.objective import evaluate
    from discovery.split import holdout_split
    task = _task_for("tid_batch", fixed_params)
    results = _eval_batch(task, synth_universe, task.split, n_proc=1)
    direct = evaluate(fixed_params, synth_universe, holdout_split())
    assert len(results) == 1
    assert results[0].status == "ok"
    assert results[0].inner == direct["inner"]


def test_eval_batch_empty_trials():
    """空 trials → [](不起 Pool,省 spawn 开销)。"""
    from compute_unit.runner import _eval_batch
    task = _task_for("x", {})
    task.trials = []
    assert _eval_batch(task, {}, task.split, n_proc=1) == []
