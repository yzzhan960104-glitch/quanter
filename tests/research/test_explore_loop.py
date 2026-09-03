# -*- coding: utf-8 -*-
"""explore_loop（归因驱动定向参数探索环）单测：意见→网格映射/升格门槛/
归因段渲染/白名单守卫。不跑回测、不连库——扫描与提案升格为集成路径
（18:45 cron 实跑验证）。"""
from __future__ import annotations

import json
from pathlib import Path

from research import explore_loop as el

CHAMP = {"stop_atr_mult": 1.5, "min_rr": 2.0, "max_holding": 30,
         "buy_limit_atr_mult": 3.0, "breakout_vol_mult": 2.0}


def _review(directions: dict | None = None, primary: str = "止损设置偏松"):
    return {"day": "2026-09-03", "legs": [
        {"leg": "main", "rows": [
            {"symbol": "300433.SZ", "fpnl_pct": -7.8, "days_held": 5,
             "analysis": {"primary": primary,
                          "param_directions": directions}},
            {"symbol": "300017.SZ", "fpnl_pct": -1.0, "days_held": 1,
             "analysis": {"primary": primary,
                          "param_directions": directions}},
        ]}]}


def test_plan_grids_llm_directions_win():
    """LLM param_directions 键频次优先于主因关键词回落。"""
    grids = el.plan_grids(_review(directions={"stop_atr_mult": "收紧至1.0-1.5"}),
                          CHAMP)
    assert len(grids) == 1 and grids[0]["key"] == "stop_atr_mult"
    assert grids[0]["base"] == 1.5
    assert 1.5 not in grids[0]["values"]            # 基线值不重跑
    assert all(v != 1.5 for v in grids[0]["values"])


def test_plan_grids_fallback_primary_keywords():
    """无 LLM 方向 → 主因关键词映射（「止损」→ stop_atr_mult 网格）。"""
    grids = el.plan_grids(_review(primary="止损设置偏松"), CHAMP)
    assert grids and grids[0]["key"] == "stop_atr_mult"


def test_plan_grids_llm_key_whitelist_guard():
    """白名单外键（如 TRADE_CFG 域的 pos_cap）不进网格——拦下后回落主因映射。"""
    grids = el.plan_grids(_review(directions={"pos_cap": "降到 5%"}), CHAMP)
    assert all(g["key"] != "pos_cap" for g in grids)  # 白名单外键绝不入网格
    assert grids and grids[0]["key"] == "stop_atr_mult"  # 回落主因（止损）


def test_plan_grids_top2_dimensions_only():
    """成本控制：最多 2 维。"""
    directions = {"stop_atr_mult": "收紧", "min_rr": "提高", "max_holding": "缩短"}
    grids = el.plan_grids(_review(directions=directions), CHAMP)
    assert len(grids) == 2


def test_plan_grids_champion_missing_key_skips():
    grids = el.plan_grids(_review(directions={"trailing_grace": "放宽"}), CHAMP)
    assert not grids                                 # 冠军无该键 → 维度跳过


def test_render_loser_section(tmp_path, monkeypatch):
    monkeypatch.setattr(el, "OUT_DIR", tmp_path)
    (tmp_path / "loser_review_2026-09-03.json").write_text(
        json.dumps(_review(directions={"stop_atr_mult": "收紧至 1.0-1.5"}),
                   ensure_ascii=False), encoding="utf-8")
    md = el.render_loser_section("2026-09-03")
    assert "实盘亏损归因" in md and "300433.SZ" in md
    assert "stop_atr_mult=收紧至 1.0-1.5" in md     # 探索方向随段输出
    # 缺产物日 → 空串（digest 主链零依赖）
    assert el.render_loser_section("2026-01-01") == ""
