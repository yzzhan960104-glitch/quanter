# -*- coding: utf-8 -*-
"""P3 参数发现敏感性分析只读端点（2026-08-13 · spec §4.2）。

物理定位（Why）：discovery trial 语料的敏感性分析 / 热力图 / 参数空间元数据，供前端
DiscoveryLabView「直接看」——分析是纯读操作，不应被写权限误伤：research_router 保持
require_write（proposal 写端点不动），本 router 单独挂载**不挂写鉴权**（spec §4.2）。

分层红线（spec §4.1）：本模块是 presentation 层的「research 桥」——只读库 + 调
discovery/sensitivity.py 纯函数（与 research/discovery_bridge.py 的读库模式对称，
discovery 包零 presentation 依赖不变）。

离线降级：discovery DB 缺失/空 → 空结构（沿 macro 路由惯例，不阻断 lifespan）。
"""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter

router = APIRouter(prefix="/research/discovery", tags=["参数发现分析"])

_DISCOVERY_DB = "logs/discovery_trials.db"


def _read_trials(db_path=None):
    """读最新 snapshot 的 trial rows（params/inner_metrics 原样 JSON 串，调用方解析）。

    legacy 行（snapshot_hash 带 '-pre-p1-legacy' 后缀）天然不匹配最新 snapshot 查询
    → 只分析当前引擎口径的 trial。库缺失/表未建 → []（降级，不抛）。
    """
    db = db_path or _DISCOVERY_DB
    try:
        con = sqlite3.connect(db)
        con.row_factory = sqlite3.Row
        snap = con.execute(
            "SELECT snapshot_hash FROM snapshot ORDER BY created_at DESC LIMIT 1").fetchone()
        if snap is None:
            return []
        rows = con.execute(
            "SELECT params, inner_metrics FROM trial WHERE snapshot_hash=?",
            (snap["snapshot_hash"],)).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


@router.get("/sensitivity")
def sensitivity():
    """21 维敏感性表：边际效应（每档均值+样本量）+ 主效应排名 + 死参数 + 覆盖盲区。

    前端「敏感性仪表板」数据源；metric 固定 calmar（搜索主排序目标，spec §4.2）。
    """
    from discovery.sensitivity import (marginal_effects, main_effect_ranking,
                                       dead_param_flags, coverage_blind_spots)
    from discovery.tools.param_iter import PARAM_SPACE
    from discovery.constraints import PARAM_KEYS
    trials = _read_trials()
    marg = marginal_effects(trials, PARAM_KEYS, metric="calmar")
    ranking = main_effect_ranking(marg)
    return {
        "n_trials": len(trials),
        "metric": "calmar",
        "marginals": marg,
        "ranking": [{"key": k, "spread": s, "n_levels": n} for k, s, n in ranking],
        "dead_params": dead_param_flags(marg, ranking),
        "blind_spots": coverage_blind_spots(marg, PARAM_SPACE),
    }


@router.get("/heatmap")
def heatmap(x: str, y: str, metric: str = "calmar", fill: bool = False):
    """两维热力图网格数据（{x_axis, y_axis, grid, n_obs}——n_obs 同行防单点热区误导）。

    fill 恒 False（补格评估留 P4 后，spec §4.2）：返回时透传该旗标供前端提示。
    """
    from discovery.sensitivity import heatmap_data
    trials = _read_trials()
    data = heatmap_data(trials, x, y, metric=metric)
    data["fill"] = bool(fill)
    return data


@router.get("/params")
def params():
    """PARAM_SPACE 候选档 + 耦合约束元数据（前端维度选择器联动）。"""
    from discovery.tools.param_iter import PARAM_SPACE
    return {
        "param_space": [{"key": k, "layer": layer, "candidates": cands}
                        for k, layer, cands in PARAM_SPACE],
        # 耦合约束提示（前端选择器禁用/警示；与 discovery/constraints.py 同源语义）
        "constraints": [
            "tp1_h_mult <= tp_h_mult（止盈1 不得比止盈2 远）",
            "cancel_thresh_mult >= tp1_h_mult 或 None=放飞（撤单不得比止盈1 早）",
            "trailing_grace=0 时 trailing_step/floor 互锁归零（trailing 不激活）",
            "min_rr 为死参数（结构恒 2.0，normalize 强制，调它无意义）",
        ],
    }
