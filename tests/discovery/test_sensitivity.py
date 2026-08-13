# -*- coding: utf-8 -*-
"""敏感性分析纯函数单测（P3 · spec §4 · 合成 trial 语料，零 DB）。

物理意图：marginal_effects / main_effect_ranking / dead_param_flags / heatmap_data
是 P4「数据驱动策略改进」与后台 DiscoveryLab 视图的分析核心——本文件钉死：
  ① 边际效应分档均值/样本量口径（含 None 档 str 归一）；
  ② 主效应排序 = 档间均值极差降序；
  ③ 死参数标记：合成语料中「结构恒值」参数（min_rr 三档同 calmar）必须落入低方差
     标记（spec §4.4 验收锚——用 min_rr 作锚是借其历史死参设定构造零方差场景，
     不代表 P4 后仍为死参；P4 已复活 min_rr）；
  ④ 热力图网格均值与 n_obs 同行（防单点热区误导）+ 无样本格 None。
"""
from __future__ import annotations

import json

# 项目根挂 sys.path 由 tests/conftest.py 统一注入（勿在本文件重复 insert——
# insert tests/ 会把 tests/discovery 命名空间包顶到 discovery 真包之前，遮蔽 import）。
from discovery.sensitivity import (  # noqa: E402
    marginal_effects,
    main_effect_ranking,
    dead_param_flags,
    coverage_blind_spots,
    heatmap_data,
)

_KEYS = ["window", "min_rr", "tp_h_mult"]


def _trial(params, calmar):
    """合成 trial row（对齐 store.write_trial 的 JSON 序列化形态）。"""
    return {"params": json.dumps(params),
            "inner_metrics": json.dumps({"calmar": calmar, "n": 10})}


def _trials_calmar_effect():
    """window 强效应 + min_rr 零效应（死参锚）+ tp_h_mult 中效应 的合成语料。

    window：40→calmar 1.0 / 60→2.0 / 80→3.0（极差 2.0 = 主效应第一）；
    tp_h_mult：1.5→1.5 / 2.0→2.0 / 2.5→2.5（极差 1.0 = 第二）；
    min_rr：任意档值都恒 1.0/1.5/2.0 → 同一 calmar（结构死参，极差 0）。
    """
    trials = []
    for w, c in [(40, 1.0), (60, 2.0), (80, 3.0)]:
        for tp, c2 in [(1.5, 1.5), (2.0, 2.0), (2.5, 2.5)]:
            for rr in [1.0, 1.5, 2.0]:
                trials.append(_trial({"window": w, "min_rr": rr, "tp_h_mult": tp},
                                     calmar=c + c2))   # 主效应叠加，min_rr 无贡献
    return trials


def test_marginal_effects_means_and_counts():
    """边际效应：window 档均值随档递增（强效应），min_rr 三档均值相同（死参锚）。"""
    trials = _trials_calmar_effect()
    marg = marginal_effects(trials, _KEYS, metric="calmar")
    # window：3 档 × (3 tp × 3 rr) = 9 样本/档，均值 1+1.5/2+2/3+2.5 各自叠加平均
    assert marg["window"]["40"]["n"] == 9
    assert marg["window"]["80"]["mean"] > marg["window"]["60"]["mean"] > marg["window"]["40"]["mean"]
    # min_rr：三档样本数相同且均值一致（无主效应）
    means_rr = {marg["min_rr"][lv]["mean"] for lv in ("1.0", "1.5", "2.0")}
    assert len(means_rr) == 1, f"死参 min_rr 档间均值应相同，实际 {means_rr}"


def test_main_effect_ranking_order():
    """主效应排序：window（极差 2.0）> tp_h_mult（1.0）> min_rr（0）。"""
    trials = _trials_calmar_effect()
    marg = marginal_effects(trials, _KEYS, metric="calmar")
    ranked = main_effect_ranking(marg)
    assert [k for k, _, _ in ranked] == ["window", "tp_h_mult", "min_rr"]
    assert ranked[0][1] == 2.0 and ranked[-1][1] == 0.0


def test_dead_param_flags_min_rr():
    """死参数标记：min_rr 极差 0 ≤ 最大极差×0.1 → 标记（spec §4.4 验收锚）。"""
    trials = _trials_calmar_effect()
    marg = marginal_effects(trials, _KEYS, metric="calmar")
    ranked = main_effect_ranking(marg)
    assert "min_rr" in dead_param_flags(marg, ranked)


def test_coverage_blind_spots():
    """覆盖盲区：候选档中从未采样到的档被列出（含 None 档 str 归一）。"""
    trials = [_trial({"window": 40, "min_rr": 1.0, "tp_h_mult": 1.5}, 1.0)]
    marg = marginal_effects(trials, _KEYS, metric="calmar")
    # PARAM_SPACE 三件套口径 (key, layer, candidates)——与 sampler 同源
    spots = coverage_blind_spots(marg, [("window", "id", [40, 60, 80]),
                                        ("min_rr", "id", [1.0, 1.5, 2.0]),
                                        ("tp_h_mult", "id", [1.5, 2.0])])
    assert spots["window"] == ["60", "80"]
    assert spots["min_rr"] == ["1.5", "2.0"]


def test_heatmap_data_grid_and_nobs():
    """热力图：网格均值 + n_obs 同行；无样本格 (mean=None, n=0)。"""
    trials = [
        _trial({"window": 40, "tp_h_mult": 1.5}, 1.0),
        _trial({"window": 40, "tp_h_mult": 1.5}, 2.0),   # 同格 2 样本 → 均值 1.5
        _trial({"window": 60, "tp_h_mult": 2.0}, 3.0),
    ]
    h = heatmap_data(trials, "window", "tp_h_mult", metric="calmar")
    assert h["x_axis"] == ["40", "60"]          # 数值升序
    assert h["y_axis"] == ["1.5", "2.0"]
    # grid[0] = y=1.5 行：[x=40 格=1.5, x=60 格=None]
    assert h["grid"][0] == [1.5, None]
    assert h["n_obs"][0] == [2, 0]
    assert h["grid"][1] == [None, 3.0]
    assert h["n_obs"][1] == [0, 1]


def test_bad_rows_skipped():
    """坏行（params/inner_metrics 非 JSON）→ 跳过不抛（读库容错）。"""
    trials = [
        _trial({"window": 40, "min_rr": 1.0, "tp_h_mult": 1.5}, 1.0),
        {"params": "{broken", "inner_metrics": "{also broken"},
        {"params": "{}", "inner_metrics": "{}"},
    ]
    marg = marginal_effects(trials, _KEYS, metric="calmar")
    assert marg["window"]["40"]["n"] == 1   # 坏行全跳过，好行计数 1
