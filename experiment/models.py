# -*- coding: utf-8 -*-
"""实验系统数据模型 + 状态机/权重校验（纯标准库，零外部依赖）。

核心抽象（design §3.1）：不区分 prod/candidate 语义，平台只懂「版本 + 权重」。
- ExperimentVersion: 策略名 + 参数快照(promote 后不可变) + 资金权重 + 状态 + 版本号
- AuditLog: append-only 变更审计（谁/何时/改了哪个版本/权重从x→y）
- ActiveExperiment: resolver 返回给 scan 的精简视图（experiment_id/strategy_name/params/weight）

状态机（design §3.3）：
    DRAFT ──promote──→ ACTIVE ──archive──→ ARCHIVED
                                         └──rollback──→ ACTIVE
    DRAFT ──discard──→ 删除
资金守恒红线：所有 ACTIVE 版本 weight 之和 ≤ 1.0。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class ExperimentStatus(str, Enum):
    """实验版本三态。str 枚举便于 SQLite 存取（直接存 name 字符串）。"""
    DRAFT = "DRAFT"        # 草稿：未上线，可编辑/删除
    ACTIVE = "ACTIVE"      # 在线：scan resolve 会读到，参与下单
    ARCHIVED = "ARCHIVED"  # 归档：下线，保留历史，可 rollback 回 ACTIVE


@dataclass
class ExperimentVersion:
    """实验版本（参数快照，promote 到 ACTIVE 后 params 不可变）。"""
    experiment_id: str                 # 唯一标识，如 "neckline_v6_20260722"
    strategy_name: str                 # build_strategy 的 name（"neckline"/"caisen"）
    params: dict                       # 参数快照（JSON 序列化存 SQLite，promote 后锁）
    weight: float                      # 资金占比 0.0~1.0
    status: ExperimentStatus
    version: int                       # 同 strategy_name 下递增
    source: str = ""                   # param_lab:run_xxx / manual / rollback
    note: str = ""
    created_at: str = ""               # ISO 时间戳
    activated_at: Optional[str] = None
    archived_at: Optional[str] = None


@dataclass
class ActiveExperiment:
    """resolver 返回给 scan 的精简视图（design §4.1 resolve_active 契约）。

    Why 精简：scan 只需知道「用哪个策略 + 什么参数 + 多大权重」，不需版本/审计元信息。
    """
    experiment_id: str
    strategy_name: str
    params: dict
    weight: float


@dataclass
class AuditLog:
    """append-only 审计记录（design §3.4）。changed_fields 记旧→新值。"""
    timestamp: str
    action: str                        # create/promote/set-weight/archive/rollback/discard
    experiment_id: str
    changed_fields: dict = field(default_factory=dict)
    operator: str = "cli"
    note: str = ""


# ============================================================================
# 状态机与权重校验（纯函数，store.py 写入前调）
# ============================================================================
# 合法迁移表（design §3.3 状态机）。set-weight 不改 status，不在此表。
_LEGAL_TRANSITIONS = {
    (ExperimentStatus.DRAFT, ExperimentStatus.ACTIVE): "promote",
    (ExperimentStatus.ACTIVE, ExperimentStatus.ARCHIVED): "archive",
    (ExperimentStatus.ARCHIVED, ExperimentStatus.ACTIVE): "rollback",
}


def validate_transition(old: ExperimentStatus, new: ExperimentStatus) -> bool:
    """状态迁移合法性校验。非法迁移（如 ARCHIVED→DRAFT）返回 False，store 拒绝写入。"""
    return (old, new) in _LEGAL_TRANSITIONS


def validate_weight_sum(active_versions: list, new_weight: float) -> bool:
    """资金守恒红线：所有 ACTIVE 版本 weight + new_weight ≤ 1.0。

    参数：
        active_versions: 当前所有 ACTIVE 的 ExperimentVersion 列表（不含待加入的版本）。
        new_weight: 待 promote/set-weight 的目标权重。

    Why ≤ 1.0 而非 == 1.0：允许部分资金空闲（如两实验合计 0.5，剩 0.5 空仓）。
    """
    current = sum(v.weight for v in active_versions)
    return (current + new_weight) <= 1.0 + 1e-9   # 浮点容差
