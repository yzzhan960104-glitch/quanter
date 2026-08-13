# -*- coding: utf-8 -*-
"""参数敏感性分析纯函数（P3 · spec §4 · 2026-08-13）。

物理定位（Why）：discovery 的 trial 语料（Sobol 均匀覆盖 = 天然 DOE 设计）是免费的
敏感性数据源——对 21 维参数算「边际效应」（每档 inner 指标均值）与「主效应排序」
（档间极差），可量化识别死参数（如 min_rr 已被实证为死参）与主导参数，为 P4 策略
本体改进提供数据驱动优先级，并进后台 DiscoveryLab 视图「直接看」。

分层红线（spec §4.1）：本模块**零 DB 连接、零 presentation 依赖**——纯函数接收
trial rows（调用方从 SQLite 读后注入），analysis 与读库分离（discovery 包铁律，
与 discovery_bridge 的 research 层读库模式对称）。

口径：
  - metric 取 inner_metrics 的键（calmar/ann/sharpe/max_dd/kelly 等）；默认 calmar
    （搜索主排序目标）。
  - 只分析「已落库的合法 trial」（调用方按 snapshot/engine_hash 过滤后注入）。
"""
from __future__ import annotations

import json


def _param_of(trial):
    """trial row → params dict（JSON 字符串容错：坏行返 {} 由调用方过滤）。"""
    try:
        return json.loads(trial["params"]) if isinstance(trial.get("params"), str) \
            else (trial.get("params") or {})
    except (TypeError, ValueError):
        return {}


def _metric_of(trial, metric):
    """trial row → inner metric 值（JSON 字符串容错；缺失/坏行返 None 由统计跳过）。"""
    try:
        inner = json.loads(trial["inner_metrics"]) if isinstance(trial.get("inner_metrics"), str) \
            else (trial.get("inner_metrics") or {})
        return inner.get(metric)
    except (TypeError, ValueError):
        return None


def marginal_effects(trials, param_keys, metric="calmar"):
    """边际效应：每参数 × 每档 → {mean, n}（逐档 inner metric 均值与样本量）。

    trials: list[dict]（含 params/inner_metrics 键，调用方读库注入）。
    param_keys: 分析维度列表（21 维 PARAM_KEYS 或子集）。
    返回 {key: {level_str: {"mean": float|None, "n": int}}}——level 用 str 归一
    （None 档显示 "None"），n<1 的档 mean=None（前端渲染「—」）。
    """
    out = {}
    for key in param_keys:
        levels = {}
        for t in trials:
            p = _param_of(t)
            v = _metric_of(t, metric)
            if v is None or key not in p:
                continue
            lv = str(p[key])
            slot = levels.setdefault(lv, {"sum": 0.0, "n": 0})
            slot["sum"] += float(v)
            slot["n"] += 1
        out[key] = {
            lv: {"mean": round(s["sum"] / s["n"], 4), "n": s["n"]}
            for lv, s in levels.items()
        }
    return out


def main_effect_ranking(marginals):
    """主效应排序：档间均值极差（max-mean − min-mean）降序——一阶主效应近似。

    物理意图：极差大的参数 = 调它显著改变目标 → 主效应强；极差≈0 = 死参数候选。
    只对「≥2 档有样本」的参数算极差（单档无方差信息）。返回 [(key, spread, n_levels)]。
    """
    ranked = []
    for key, levels in marginals.items():
        means = [l["mean"] for l in levels.values() if l["mean"] is not None and l["n"] >= 1]
        if len(means) >= 2:
            ranked.append((key, round(max(means) - min(means), 4), len(levels)))
    ranked.sort(key=lambda x: x[1], reverse=True)
    return ranked


def dead_param_flags(marginals, ranking, spread_quantile=0.1):
    """死参数候选：主效应极差 ≤ 全局极差的 spread_quantile 分位 → 低方差标记。

    物理意图（spec §4.4 验收锚）：min_rr 已被实证为死参（结构恒 2.0，normalize 强制），
    其档间极差应为 0 或接近 0 → 落入低分位 → 标记。阈值取「最大极差 × spread_quantile」
    （相对阈值，自动适配目标指标尺度）。返回 [key, ...]。
    """
    if not ranking:
        return []
    max_spread = ranking[0][1]
    threshold = max_spread * spread_quantile
    return [key for key, spread, _ in ranking if spread <= threshold]


def coverage_blind_spots(marginals, param_space):
    """覆盖盲区：PARAM_SPACE 候选档中从未被采样到的档（trial 语料覆盖缺口）。"""
    spots = {}
    space = {k: c for k, _layer, c in param_space}   # (key, layer, candidates) 三件套 → 档表
    for key, levels in marginals.items():
        if key not in space:
            continue
        sampled = {lv for lv, s in levels.items() if s["n"] > 0}
        missing = [str(c) for c in space[key] if str(c) not in sampled]
        if missing:
            spots[key] = missing
    return spots


def heatmap_data(trials, x_key, y_key, metric="calmar"):
    """两维热力图数据：网格均值 + 样本量同行返回（防「单点热区」误导）。

    返回 {x_axis, y_axis, grid, n_obs}：x/y 档按候选值排序（数值优先，None 垫底）；
    grid[i][j] 为 (x_axis[j], y_axis[i]) 交点的 mean（无样本 None）；n_obs 同形样本量。
    """
    xs, ys = [], []
    grid_map = {}   # (x, y) -> [values...]
    for t in trials:
        p = _param_of(t)
        v = _metric_of(t, metric)
        if v is None or x_key not in p or y_key not in p:
            continue
        xv, yv = str(p[x_key]), str(p[y_key])
        if xv not in xs:
            xs.append(xv)
        if yv not in ys:
            ys.append(yv)
        grid_map.setdefault((xv, yv), []).append(float(v))

    def _sort_levels(levels):
        """档排序：数值可解析的按数值升序，其余（None/分类）按 str 垫底。"""
        def _key(lv):
            try:
                return (0, float(lv))
            except (TypeError, ValueError):
                return (1, lv)
        return sorted(levels, key=_key)

    x_axis = _sort_levels(xs)
    y_axis = _sort_levels(ys)
    grid, n_obs = [], []
    for y in y_axis:
        row, nrow = [], []
        for x in x_axis:
            vals = grid_map.get((x, y))
            if vals:
                row.append(round(sum(vals) / len(vals), 4))
                nrow.append(len(vals))
            else:
                row.append(None)   # 无样本格：均值 None + n=0（前端渲染「—」）
                nrow.append(0)
        grid.append(row)
        n_obs.append(nrow)
    return {"x_axis": x_axis, "y_axis": y_axis, "grid": grid, "n_obs": n_obs}
