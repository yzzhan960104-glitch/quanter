# -*- coding: utf-8 -*-
"""experiment.models 单元测试：状态机校验 + 权重守恒 + dataclass 契约。"""
import pytest
from experiment.models import (
    ExperimentStatus, ExperimentVersion, AuditLog, ActiveExperiment,
    validate_transition, validate_weight_sum,
)


def _ver(**kw):
    """造一个合法 ExperimentVersion（默认值覆盖测试用）。"""
    base = dict(experiment_id="neckline_v1_20260722", strategy_name="neckline",
                params={"window": 60}, weight=0.2, status=ExperimentStatus.DRAFT,
                version=1, source="manual", note="", created_at="2026-07-22T10:00:00",
                activated_at=None, archived_at=None)
    base.update(kw)
    return ExperimentVersion(**base)


def test_legal_transitions():
    """合法迁移：DRAFT→ACTIVE、ACTIVE→ARCHIVED、ARCHIVED→ACTIVE。"""
    assert validate_transition(ExperimentStatus.DRAFT, ExperimentStatus.ACTIVE) is True
    assert validate_transition(ExperimentStatus.ACTIVE, ExperimentStatus.ARCHIVED) is True
    assert validate_transition(ExperimentStatus.ARCHIVED, ExperimentStatus.ACTIVE) is True


def test_illegal_transitions():
    """非法迁移一律拒绝（ARCHIVED→DRAFT、已 ACTIVE 再 promote 等）。"""
    assert validate_transition(ExperimentStatus.ARCHIVED, ExperimentStatus.DRAFT) is False
    assert validate_transition(ExperimentStatus.ACTIVE, ExperimentStatus.ACTIVE) is False
    assert validate_transition(ExperimentStatus.DRAFT, ExperimentStatus.ARCHIVED) is False


def test_weight_sum_within_limit():
    """权重和 ≤ 1.0 放行：现有 ACTIVE 合计 0.8，新版本 0.2 → 合计 1.0，通过。"""
    active = [_ver(status=ExperimentStatus.ACTIVE, weight=0.8)]
    assert validate_weight_sum(active, new_weight=0.2) is True


def test_weight_sum_exceeds_limit():
    """权重和 > 1.0 拒绝（资金守恒红线）：现有 0.8，新版本 0.3 → 1.1，拒。"""
    active = [_ver(status=ExperimentStatus.ACTIVE, weight=0.8)]
    assert validate_weight_sum(active, new_weight=0.3) is False


def test_active_experiment_dataclass():
    """ActiveExperiment 是 resolver 返回的精简视图（scan 消费）。"""
    ae = ActiveExperiment(experiment_id="e1", strategy_name="neckline",
                          params={"window": 60}, weight=0.2)
    assert ae.experiment_id == "e1" and ae.weight == 0.2


def test_audit_log_records_change():
    """AuditLog 记录 changed_fields（旧→新）。"""
    log = AuditLog(timestamp="2026-07-22T10:00:00", action="set-weight",
                   experiment_id="e1", changed_fields={"weight": [0.2, 0.5]},
                   operator="cli", note="")
    assert log.changed_fields["weight"] == [0.2, 0.5]
