# -*- coding: utf-8 -*-
"""resolver：scan 的唯一入口，实时读 SQLite 返 [ActiveExperiment]。

Why 不缓存（design §5.3）：scan 是 schtasks/CLI 触发的短任务，每次实时读 SQLite
保证 CLI 改权重后下次 scan 立即生效。零常驻进程、零缓存一致性问题。
"""
from __future__ import annotations

from typing import Optional

from experiment.models import ActiveExperiment, ExperimentStatus, ExperimentVersion
from experiment.store import _DEFAULT_DB, list_versions


def resolve_active(db_path: Optional[str] = None) -> list:
    """返回当前所有在线实验（status=ACTIVE 且 weight>0）。

    返回：list[ActiveExperiment]，每项含 experiment_id/strategy_name/params/weight。
    空列表表示无在线实验（scan 调用方应 fail-fast，不下单）。
    """
    versions = list_versions(db_path or _DEFAULT_DB, status=ExperimentStatus.ACTIVE)
    # activated_at 透传：ExperimentVersion 已有该字段 + list_versions SELECT * 直接读；
    # 此处构造 ActiveExperiment 时带上，供 T6 ≥5天硬闸算影子期（Plan 4）。
    return [ActiveExperiment(experiment_id=v.experiment_id, strategy_name=v.strategy_name,
                             params=v.params, weight=v.weight, activated_at=v.activated_at)
            for v in versions if v.weight > 0]


def resolve_champion(db_path: Optional[str] = None) -> Optional[ExperimentVersion]:
    """返回「当前冠军」=weight 最高的 ACTIVE 实验版本（无 ACTIVE 返 None）。

    单一选择口径（2026-08-05 SSoT review-fix2 P2 落地）：多 ACTIVE 灰度并存时，
    broadcast / probe_champion_oos / weekly_replay / discovery cli 必须取同一口径的
    「当前冠军」——即 ``max(weight)``。此前各工具一个取 ``active[0]``（list_versions
    序，非最权重）、一个取 ``max(weight)``，多 ACTIVE 时各工具播报/回测的「当前冠军」
    不一致，是隐性 SSoT 漂移。本函数为唯一选择点，4 处调用方全部复用。

    物理意图（灰度语义）：ACTIVE 实验代表「在线生效版本」，weight 是其资金占比；
    多 ACTIVE 灰度（如 0.7+0.3）时，**最高权重者=当前主导版本**——这与 broadcast
    「展示当前主导实验」、probe/oos「探查当前生效冠军去偏」、weekly_replay「跑当前
    主导参数回测」、cli `oos`「固化当前冠军去偏」的诉求一致。无 ACTIVE → None
    （调用方各自 fail-fast：broadcast 降级 None、probe/cli sys.exit(2)、weekly_replay {}）。

    返回 ``ExperimentVersion``（完整字段）而非 ``ActiveExperiment``：调用方需读
    ``.note``（broadcast 解析 "outer ann="）/``.version``（brief 渲染版本号），这些
    字段只在 ExperimentVersion 上；ActiveExperiment 是 scan 用的精简视图。返回完整
    对象不破坏 ActiveExperiment 契约——resolver 仍只把 ActiveExperiment 暴露给 scan。

    Args:
        db_path: experiment.db 路径（测试注入）；None 走 _DEFAULT_DB 默认。
    """
    versions = [v for v in list_versions(db_path or _DEFAULT_DB, status=ExperimentStatus.ACTIVE)
                if v.weight > 0]
    if not versions:
        return None
    return max(versions, key=lambda v: v.weight)
