# -*- coding: utf-8 -*-
"""跑批调度测试：断点续跑去重 + 采样落库计数 + 汇总字段。

合成 universe 快测（monkeypatch freeze/eval_batch 避免真实 Pool+parquet）；
真实跑批集成在 Task 5 cli slow 测试。
"""
from datetime import date


def _fake_meta():
    from discovery.snapshot import SnapshotMeta
    return SnapshotMeta("snaptest", "创板科创", 1, "2025~2026", "2025-01-01")


def _fake_split():
    from discovery.split import HoldoutSplit, Segment
    return HoldoutSplit(Segment("i", date(2025, 1, 1), date(2025, 12, 31)),
                        Segment("o", date(2026, 1, 1), date(2026, 12, 31)), 5)


def test_persist_trial_writes_and_returns_id(tmp_path, monkeypatch):
    """_persist_trial 落库返回 trial_id（新 trial）。"""
    from discovery import runner
    from discovery.store import init_db, connect, trial_exists
    db = str(tmp_path / "t.db")
    init_db(db)
    params = {"window": 80, "min_rr": 2.0}
    result = {"inner": {"ann": 0.5, "calmar": 2.0, "max_dd": 0.2, "n": 100},
              "outer": {"ann": 1.5, "calmar": 5.0, "max_dd": 0.3, "n": 80}, "n_total": 180}
    with connect(db) as conn:
        tid = runner._persist_trial(conn, params, _fake_meta(), "holdout_2025_2026",
                                    "eng1", result, "sobol", seed=0)
    assert tid is not None
    with connect(db) as conn:
        assert trial_exists(conn, tid) is True


def test_persist_trial_returns_none_on_dup(tmp_path):
    """同 params+snapshot+seed 已存在 → 返回 None（断点续跑去重）。"""
    from discovery import runner
    from discovery.store import init_db, connect
    db = str(tmp_path / "t.db")
    init_db(db)
    params = {"window": 80}
    result = {"inner": {"ann": 0.5}, "outer": {"ann": 1.5}, "n_total": 10}
    with connect(db) as conn:
        tid1 = runner._persist_trial(conn, params, _fake_meta(), "s", "e", result, "sobol", 0)
        tid2 = runner._persist_trial(conn, params, _fake_meta(), "s", "e", result, "sobol", 0)
    assert tid1 is not None
    assert tid2 is None   # 重复落库被 INSERT OR IGNORE 吞


def test_run_search_dedup_on_restart(tmp_path, monkeypatch):
    """kill 重启场景：已落 trial 不重跑（eval_batch 不再被调用于已存组）。"""
    from discovery import runner
    from discovery.store import init_db, connect
    db = str(tmp_path / "t.db")
    init_db(db)

    # monkeypatch sample_search 产固定 3 组（可复现 seed）
    fixed_params = [
        {"window": 40, "min_rr": 2.0, "tp1_h_mult": 1.0, "tp_h_mult": 2.5,
         "cancel_thresh_mult": 3.0, "trailing_grace": 5, "trailing_step": 0.1, "trailing_floor": 0.0},
        {"window": 60, "min_rr": 2.0, "tp1_h_mult": 1.0, "tp_h_mult": 2.5,
         "cancel_thresh_mult": 3.0, "trailing_grace": 5, "trailing_step": 0.1, "trailing_floor": 0.0},
        {"window": 80, "min_rr": 2.0, "tp1_h_mult": 1.0, "tp_h_mult": 2.5,
         "cancel_thresh_mult": 3.0, "trailing_grace": 5, "trailing_step": 0.1, "trailing_floor": 0.0},
    ]
    monkeypatch.setattr(runner, "sample_search", lambda **kw: fixed_params)
    # monkeypatch eval_batch 直接造结果（不真实起 Pool）
    def fake_eval(plist, **kw):
        return [(p, {"inner": {"ann": 0.5, "calmar": 2.0, "max_dd": 0.2, "n": 100},
                     "outer": {"ann": 1.5, "calmar": 5.0, "max_dd": 0.3, "n": 80},
                     "n_total": 180}) for p in plist]
    monkeypatch.setattr(runner, "eval_batch", fake_eval)
    monkeypatch.setattr(runner, "_engine_hash", lambda: "eng1")

    # 第一次跑：3 组全落库
    s1 = runner.run_search(_fake_meta(), _fake_split(), budget=3, n_sobol=2, n_random=1,
                           seed=42, db_path=db)
    assert s1.n_new_trials == 3
    assert s1.n_skipped_dup == 0
    # 第二次跑（模拟重启，同 seed 同采样）：3 组已存，全跳过
    s2 = runner.run_search(_fake_meta(), _fake_split(), budget=3, n_sobol=2, n_random=1,
                           seed=42, db_path=db)
    assert s2.n_new_trials == 0
    assert s2.n_skipped_dup == 3


def test_run_search_summary_fields(tmp_path, monkeypatch):
    """RunSummary 字段齐全（n_sampled/n_evaluated/n_new/n_skipped/n_failed/top_inner_calmar）。"""
    from discovery import runner
    from discovery.store import init_db
    db = str(tmp_path / "t.db")
    init_db(db)
    monkeypatch.setattr(runner, "sample_search", lambda **kw: [
        {"window": 80, "min_rr": 2.0, "tp1_h_mult": 1.0, "tp_h_mult": 2.5,
         "cancel_thresh_mult": 3.0, "trailing_grace": 5, "trailing_step": 0.1, "trailing_floor": 0.0}])
    monkeypatch.setattr(runner, "eval_batch", lambda plist, **kw: [
        (plist[0], {"inner": {"ann": 0.5, "calmar": 3.5, "max_dd": 0.2, "n": 100},
                    "outer": {"ann": 1.5, "calmar": 5.0, "max_dd": 0.3, "n": 80}, "n_total": 180})])
    monkeypatch.setattr(runner, "_engine_hash", lambda: "eng1")
    s = runner.run_search(_fake_meta(), _fake_split(), budget=1, n_sobol=1, n_random=0,
                          seed=1, db_path=db)
    assert s.n_sampled == 1
    assert s.n_evaluated == 1
    assert s.n_new_trials == 1
    assert s.n_failed == 0
    assert s.top_inner_calmar == 3.5
    assert s.db_path == db
    assert s.status == "budget_exhausted"


def test_run_search_failed_trial_filtered(tmp_path, monkeypatch):
    """eval_batch 返回 None（异常组）→ n_failed 计数，不落库。"""
    from discovery import runner
    from discovery.store import init_db
    db = str(tmp_path / "t.db")
    init_db(db)
    monkeypatch.setattr(runner, "sample_search", lambda **kw: [
        {"window": 80, "min_rr": 2.0, "tp1_h_mult": 1.0, "tp_h_mult": 2.5,
         "cancel_thresh_mult": 3.0, "trailing_grace": 5, "trailing_step": 0.1, "trailing_floor": 0.0}])
    monkeypatch.setattr(runner, "eval_batch", lambda plist, **kw: [None])  # 全失败
    monkeypatch.setattr(runner, "_engine_hash", lambda: "eng1")
    s = runner.run_search(_fake_meta(), _fake_split(), budget=1, n_sobol=1, n_random=0,
                          seed=1, db_path=db)
    assert s.n_failed == 1
    assert s.n_new_trials == 0
