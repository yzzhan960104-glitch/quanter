# -*- coding: utf-8 -*-
"""experiment.store 单元测试：SQLite CRUD + 审计 + 事务回滚 + 状态机/权重校验。"""
import os
import tempfile

import pytest

from experiment.models import ExperimentStatus, ExperimentVersion
from experiment import store


@pytest.fixture
def db(tmp_path):
    """每个测试用独立临时 db 文件。"""
    p = str(tmp_path / "t.db")
    store.init_db(p)
    return p


def _make(version_id="e1", strategy="neckline", weight=0.2, status=ExperimentStatus.DRAFT,
          version=1, params=None):
    return ExperimentVersion(
        experiment_id=version_id, strategy_name=strategy, params=params or {"window": 60},
        weight=weight, status=status, version=version, source="manual",
        created_at="2026-07-22T10:00:00")


def test_init_db_creates_tables(db):
    """init_db 建两张表 + 索引。"""
    import sqlite3
    con = sqlite3.connect(db)
    tabs = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"experiment_version", "audit_log"} <= tabs
    con.close()


def test_create_and_list(db):
    """create 写 DRAFT，list 能读到。"""
    store.create_version(db, _make(), operator="cli")
    rows = store.list_versions(db)
    assert len(rows) == 1 and rows[0].experiment_id == "e1"
    assert rows[0].status == ExperimentStatus.DRAFT


def test_promote_writes_audit_and_status(db):
    """promote: DRAFT→ACTIVE + 写审计 + 校验权重和。"""
    store.create_version(db, _make(weight=0.0), operator="cli")
    store.promote(db, "e1", weight=0.2, operator="cli", now="2026-07-22T11:00:00")
    rows = store.list_versions(db, status=ExperimentStatus.ACTIVE)
    assert rows[0].weight == 0.2 and rows[0].activated_at == "2026-07-22T11:00:00"
    audit = store.list_audit(db, "e1")
    assert any(a.action == "promote" for a in audit)


def test_promote_rejects_weight_overflow(db):
    """权重和 > 1.0 promote 被拒（资金守恒红线）。"""
    store.create_version(db, _make("e1", weight=0.0), operator="cli")
    store.promote(db, "e1", weight=0.8, operator="cli", now="t")
    store.create_version(db, _make("e2", version=2, weight=0.0), operator="cli")
    with pytest.raises(ValueError, match="权重"):
        store.promote(db, "e2", weight=0.3, operator="cli", now="t")  # 0.8+0.3=1.1 > 1.0


@pytest.mark.xfail(reason="v1 plan 自身矛盾：ARCHIVED→ACTIVE 在 _LEGAL_TRANSITIONS 中是 rollback "
                          "合法路径，故 promote(ARCHIVED 版本) 通过状态机校验。需设计决策：promote "
                          "是否应限定起点为 DRAFT（与 rollback 区分）。见 task-1-report.md NEEDS_CONTEXT。")
def test_illegal_transition_rejected(db):
    """ARCHIVED→DRAFT 非法迁移拒绝。"""
    store.create_version(db, _make("e1", weight=0.0), operator="cli")
    store.promote(db, "e1", weight=0.5, operator="cli", now="t")
    store.archive(db, "e1", operator="cli", now="t")
    # 直接对已 ARCHIVED 再 promote（ACTIVE→ACTIVE 等效非法）应拒
    with pytest.raises(ValueError):
        store.promote(db, "e1", weight=0.5, operator="cli", now="t")


def test_rollback_restores_active(db):
    """rollback: ARCHIVED→ACTIVE，恢复上线。"""
    store.create_version(db, _make("e1", weight=0.0), operator="cli")
    store.promote(db, "e1", weight=0.5, operator="cli", now="t")
    store.archive(db, "e1", operator="cli", now="t")
    store.rollback(db, "e1", operator="cli", now="t2")
    rows = store.list_versions(db, status=ExperimentStatus.ACTIVE)
    assert len(rows) == 1 and rows[0].experiment_id == "e1"


def test_set_weight_records_old_new(db):
    """set_weight 记审计 changed_fields weight [旧,新]。"""
    store.create_version(db, _make("e1", weight=0.0), operator="cli")
    store.promote(db, "e1", weight=0.2, operator="cli", now="t")
    store.set_weight(db, "e1", new_weight=0.5, operator="cli", now="t2")
    audit = store.list_audit(db, "e1")
    sw = [a for a in audit if a.action == "set-weight"][0]
    assert sw.changed_fields["weight"] == [0.2, 0.5]


def test_params_immutable_after_promote(db):
    """promote 后 params 不可变：用同名同 version 改 params 应被拒（UNIQUE 约束 + 显式拒绝）。"""
    store.create_version(db, _make("e1", params={"window": 60}), operator="cli")
    store.promote(db, "e1", weight=0.5, operator="cli", now="t")
    # 再次 create 同 experiment_id 应拒（主键冲突）
    with pytest.raises(ValueError):
        store.create_version(db, _make("e1", params={"window": 99}), operator="cli")
