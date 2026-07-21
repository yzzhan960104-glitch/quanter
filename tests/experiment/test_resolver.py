# -*- coding: utf-8 -*-
"""resolver 单元测试：只返 ACTIVE+weight>0；params 快照正确。"""
from experiment import store, resolver
from experiment.models import ExperimentStatus, ExperimentVersion


def _make(db, eid="e1", weight=0.2, params=None, status=ExperimentStatus.DRAFT, version=1):
    # 注：v1 plan 固定 version=1 与 schema UNIQUE(strategy_name, version) 冲突，
    # 此处改为可传 version 让多版本同 strategy 共存（保持 schema 与实现不变，仅修测试 helper）。
    v = ExperimentVersion(experiment_id=eid, strategy_name="neckline",
                          params=params or {"window": 60}, weight=weight, status=status,
                          version=version, source="manual", created_at="2026-07-22T10:00:00")
    store.create_version(db, v, operator="cli")


def test_resolve_returns_only_active_positive_weight(tmp_path):
    """resolve_active 只返 ACTIVE 且 weight>0（DRAFT/ARCHIVED/weight=0 过滤掉）。"""
    db = str(tmp_path / "t.db")
    store.init_db(db)
    _make(db, "e1", weight=0.5, version=1)           # 将 promote
    _make(db, "e2", weight=0.0, version=2)           # 留 DRAFT
    store.promote(db, "e1", weight=0.5, operator="cli", now="t")
    _make(db, "e3", weight=0.3, version=3)
    store.promote(db, "e3", weight=0.3, operator="cli", now="t")
    store.set_weight(db, "e3", new_weight=0.0, operator="cli", now="t2")  # weight=0 软下线
    active = resolver.resolve_active(db)
    ids = {a.experiment_id for a in active}
    assert ids == {"e1"}   # e2 DRAFT、e3 weight=0 都被过滤


def test_resolve_returns_params_snapshot(tmp_path):
    """resolve 返回的 params 是不可变快照（与 store 写入一致）。"""
    db = str(tmp_path / "t.db")
    store.init_db(db)
    _make(db, "e1", params={"window": 90, "min_touches": 4})
    store.promote(db, "e1", weight=1.0, operator="cli", now="t")
    [ae] = resolver.resolve_active(db)
    assert ae.params == {"window": 90, "min_touches": 4}
    assert ae.strategy_name == "neckline" and ae.weight == 1.0


def test_resolve_empty_when_no_active(tmp_path):
    """无 ACTIVE 实验 → 返回空列表（scan 据此 fail-fast）。"""
    db = str(tmp_path / "t.db")
    store.init_db(db)
    assert resolver.resolve_active(db) == []
