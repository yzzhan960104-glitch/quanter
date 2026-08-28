

# ============================================================================
# W5-2（2026-08-28 全库评审 P1-1）：engine_hash 进 trial 去重键
# ============================================================================
def test_trial_idof_engine_hash_changes_id():
    """同 params/snapshot/seed，不同 engine_hash → 不同 trial_id（内核变更=新试验，
    不再被 trial_exists 当重复静默跳过）。"""
    from discovery.store import trial_id_of
    p = {"window": 60}
    a = trial_id_of(p, "snap1", seed=0, engine_hash="hash_a")
    b = trial_id_of(p, "snap1", seed=0, engine_hash="hash_b")
    c = trial_id_of(p, "snap1", seed=0, engine_hash="")
    assert a != b and b != c and a != c


def test_trial_idof_legacy_backward_compat():
    """engine_hash 缺省 ""：签名含 "e":"" 键——与旧三键签名不同但语义向后兼容
    （老 trial_id 不迁移，新旧 id 空间自然分流）。钉死签名形态防回归。"""
    from discovery.store import trial_id_of
    import json, hashlib
    p = {"w": 1}
    expect = hashlib.sha256(json.dumps(
        {"p": p, "s": "s", "seed": 7, "e": ""},
        sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()[:12]
    assert trial_id_of(p, "s", seed=7) == expect


def test_read_trials_filters_by_engine_hash(tmp_path):
    """engine_hash 过滤：跨内核 trial 不混入 DSR/Pareto 读数。"""
    from discovery.store import connect, init_db, write_trial, read_trials_by_snapshot
    db = tmp_path / "t.db"
    init_db(str(db))
    with connect(str(db)) as conn:
        write_trial(conn, "t1", {"w": 1}, "snap", "eng_old", "s_tag", "{}", "{}", "grid")
        write_trial(conn, "t2", {"w": 2}, "snap", "eng_new", "s_tag", "{}", "{}", "grid")
        all_rows = read_trials_by_snapshot(conn, "snap")
        assert {r["trial_id"] for r in all_rows} == {"t1", "t2"}
        new_only = read_trials_by_snapshot(conn, "snap", engine_hash="eng_new")
        assert [r["trial_id"] for r in new_only] == ["t2"]
