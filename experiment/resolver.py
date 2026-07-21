# -*- coding: utf-8 -*-
"""resolver：scan 的唯一入口，实时读 SQLite 返 [ActiveExperiment]。

Why 不缓存（design §5.3）：scan 是 schtasks/CLI 触发的短任务，每次实时读 SQLite
保证 CLI 改权重后下次 scan 立即生效。零常驻进程、零缓存一致性问题。
"""
from __future__ import annotations

from typing import Optional

from experiment.models import ActiveExperiment, ExperimentStatus
from experiment.store import _DEFAULT_DB, list_versions


def resolve_active(db_path: Optional[str] = None) -> list:
    """返回当前所有在线实验（status=ACTIVE 且 weight>0）。

    返回：list[ActiveExperiment]，每项含 experiment_id/strategy_name/params/weight。
    空列表表示无在线实验（scan 调用方应 fail-fast，不下单）。
    """
    versions = list_versions(db_path or _DEFAULT_DB, status=ExperimentStatus.ACTIVE)
    return [ActiveExperiment(experiment_id=v.experiment_id, strategy_name=v.strategy_name,
                             params=v.params, weight=v.weight)
            for v in versions if v.weight > 0]
