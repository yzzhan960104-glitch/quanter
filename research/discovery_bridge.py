# -*- coding: utf-8 -*-
"""discovery 进展读取 + 自动 publish 桥（2026-08-03 · 低功率探索与实验平台打通）。

物理定位：
    load_discovery_status：把 discovery_trials.db 的进展（trial 数/最新 run/k 进度/
    新冠军/ACTIVE 实验）读成 digest/API 可用的 dict——"进展可见"；
    auto_publish_champion：低功率每轮产生新冠军且 outer 优于当前 ACTIVE 的 outer
    时，自动 publish 到 experiment DRAFT（weight=0，promote 仍留人审）——
    "与实验平台打通"（替代手动 `python -m discovery publish`）。

护栏：
    - 只建 DRAFT（experiment promote 红线不变，过拟合参数不会直冲 ACTIVE）；
    - outer 不优于当前 ACTIVE → 不建 DRAFT（防每轮垃圾候选刷屏）；
    - daemon 侧注入式调用（run_daemon_cycle 的 auto_publish_fn 由 cli 装配），
      discovery 包零 research 依赖（分层干净）。
"""
from __future__ import annotations

import json
import logging
import sqlite3

from research import proposals

logger = logging.getLogger(__name__)

_DISCOVERY_DB = "logs/discovery_trials.db"


def _connect(db_path: str):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    return con


def load_discovery_status(db_path: str | None = None) -> dict:
    """读 discovery 库 → 进展 dict（trial 数/最新 run/k 进度/新冠军 metrics）。

    供 research digest 与 GET /research/discovery/status 使用；库缺失/表未建 →
    零值 dict（渲染「—」，不抛）。
    """
    out = {"n_trials": 0, "latest_run": None, "champion": None}
    db_path = db_path or _DISCOVERY_DB
    try:
        with _connect(db_path) as con:
            out["n_trials"] = int(con.execute(
                "SELECT COUNT(*) c FROM trial").fetchone()["c"] or 0)
            run = con.execute(
                "SELECT run_id, snapshot_hash, started_at, n_trials, status,"
                " frontier_size_prev, k_rounds_no_expansion, daemon_run_count"
                " FROM search_run ORDER BY started_at DESC LIMIT 1").fetchone()
            if run is not None:
                out["latest_run"] = dict(run)
            # 冠军：最新 snapshot 下 inner calmar 最高的 trial（feasibility 近似：
            # 直接取 inner_metrics 中 calmar 最大者，读库侧不重跑搜索）
            snap = con.execute(
                "SELECT snapshot_hash FROM snapshot ORDER BY created_at DESC LIMIT 1").fetchone()
            if snap is not None:
                rows = con.execute(
                    "SELECT trial_id, params, inner_metrics, outer_metrics FROM trial"
                    " WHERE snapshot_hash=? ORDER BY created_at DESC",
                    (snap["snapshot_hash"],)).fetchall()
                best = None
                for r in rows:
                    try:
                        inner = json.loads(r["inner_metrics"])
                    except (TypeError, ValueError):
                        continue
                    if best is None or inner.get("calmar", 0) > best[1].get("calmar", 0):
                        best = (r, inner)
                if best is not None:
                    r, inner = best
                    out["champion"] = {
                        "trial_id": r["trial_id"],
                        "params": json.loads(r["params"]),
                        "inner": inner,
                        "outer": _safe_json(r["outer_metrics"]),
                    }
    except Exception:
        logger.warning("读 discovery 状态失败（降级零值）：%s", db_path, exc_info=True)
    return out


def _safe_json(raw) -> dict | None:
    """outer_metrics JSON 容错（损坏/None → None）。"""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def _parse_outer_ann(note: str) -> float | None:
    """从 experiment note 解析 outer ann（如 "outer ann=18.4% ..." → 0.184）。"""
    if not note or "outer ann=" not in note:
        return None
    try:
        return float(note.split("outer ann=", 1)[1].split("%", 1)[0]) / 100.0
    except (ValueError, IndexError):
        return None


def _active_outer_ann() -> float | None:
    """当前 ACTIVE 实验的 outer 去偏年化（note 解析；无 ACTIVE/无 note → None）。"""
    try:
        from experiment.models import ExperimentStatus
        from experiment.store import list_versions
        versions = [
            v for v in list_versions() if v.status is ExperimentStatus.ACTIVE and v.weight > 0
        ]
        if not versions:
            return None
        top = max(versions, key=lambda v: v.weight)
        return _parse_outer_ann(top.note or "")
    except Exception:
        return None


def auto_publish_champion(trial_id: str, outer: dict | None,
                          db_path: str | None = None) -> str | None:
    """新冠军 outer 优于当前 ACTIVE → publish experiment DRAFT；否则 None。

    Args:
        trial_id: 本轮冠军 trial_id（daemon summary.top_trial_id）。
        outer: 冠军 outer 去偏 metrics（daemon 已算，{"ann":...}）；None → 跳过。
        db_path: discovery 库路径（测试注入）。
    Returns:
        experiment_id（已建 DRAFT）或 None（outer 缺失/不优于 ACTIVE/重复建桥失败）。
    """
    if not outer or outer.get("ann") is None:
        return None
    active_ann = _active_outer_ann()
    if active_ann is not None and float(outer["ann"]) <= active_ann:
        logger.info("新冠军 outer ann=%.1f%% 不优于 ACTIVE（%.1f%%），跳过自动 publish",
                    float(outer["ann"]) * 100, active_ann * 100)
        return None
    db_path = db_path or _DISCOVERY_DB
    try:
        with _connect(db_path) as con:
            row = con.execute(
                "SELECT params FROM trial WHERE trial_id=?", (trial_id,)).fetchone()
        if row is None:
            logger.warning("自动 publish 跳过：trial 不存在 %s", trial_id)
            return None
        params = json.loads(row["params"])
        exp_id = proposals._create_experiment_draft(params, trial_id)
        logger.info("自动 publish DRAFT：trial=%s outer ann=%.1f%% → %s",
                    trial_id, float(outer["ann"]) * 100, exp_id)
        return exp_id
    except Exception:
        logger.warning("自动 publish 异常（软降级，不影响 daemon 主流程）", exc_info=True)
        return None
