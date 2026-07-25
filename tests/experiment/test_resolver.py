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


def test_resolve_active_includes_activated_at(tmp_path, monkeypatch):
    """resolve_active 返回值须含 activated_at（≥5天硬闸依赖此字段算影子期，Plan 4 T6）。

    物理意图：ActiveExperiment 是 scan/engine _eod 的输入；T6 影子期 ≥5天硬闸需用此字段
    判「上线满 5 天才进实盘 budget」。本测把 activated_at 从 SQLite 读出后是否被 resolver
    透传到 ActiveExperiment 钉死（additive 字段，不破坏既有 dataclass 字段顺序）。
    """
    import sqlite3
    db = str(tmp_path / "e.db")
    store.init_db(db)
    v = ExperimentVersion("exp1", "neckline", {"window": 5}, 0.1,
                          ExperimentStatus.ACTIVE, 1, source="test",
                          created_at="2026-01-01T00:00:00", activated_at="2026-01-01T00:00:00")
    store.create_version(db, v)   # create_version 落的是 DRAFT；下面手动 promote
    # 直接落 ACTIVE（绕过 promote 的权重校验，单独测 resolver 读 activated_at 列）
    con = sqlite3.connect(db)
    con.execute(
        "UPDATE experiment_version SET status='ACTIVE', activated_at='2026-01-01T00:00:00' "
        "WHERE experiment_id='exp1'")
    con.commit()
    con.close()
    active = resolver.resolve_active(db)
    assert len(active) == 1
    assert active[0].activated_at == "2026-01-01T00:00:00"
