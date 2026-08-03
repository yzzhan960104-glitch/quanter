# -*- coding: utf-8 -*-
"""策略级 skip/事件统计累加纯函数单测（A3 · 2026-08-03 Phase A）。

物理意图：simulate_exit 返回的 same_day_both / stop_gap 事件需要从"逐信号"
聚合到"策略级计数"，供 replay metadata 与自主优化管道归因（量化方向性假设
与跳空止损的规模）。本测试钉死聚合纯函数 _accumulate_sim_stats。
"""
from strategies.neckline.backtest import _accumulate_sim_stats


def test_accumulate_counts_skip_and_same_day():
    """skip_target_met + same_day_both → n_skipped 与 same_day_both 各 +1。"""
    stats = {"n_skipped": 0, "same_day_both": 0, "stop_gap": 0}
    _accumulate_sim_stats(stats, {
        "exit_reason": "skip_target_met", "same_day_both": True})
    assert stats == {"n_skipped": 1, "same_day_both": 1, "stop_gap": 0}


def test_accumulate_plain_skip_not_same_day():
    """skip_no_pullback（无同日竞争）→ 只加 n_skipped。"""
    stats = {"n_skipped": 0, "same_day_both": 0, "stop_gap": 0}
    _accumulate_sim_stats(stats, {
        "exit_reason": "skip_no_pullback", "same_day_both": False})
    assert stats == {"n_skipped": 1, "same_day_both": 0, "stop_gap": 0}


def test_accumulate_stop_gap_only_for_gap_stop():
    """止损跳空（stop_gap=True）→ stop_gap +1；非跳空止损不计。"""
    stats = {"n_skipped": 0, "same_day_both": 0, "stop_gap": 0}
    _accumulate_sim_stats(stats, {"exit_reason": "stop_loss", "stop_gap": True})
    assert stats == {"n_skipped": 0, "same_day_both": 0, "stop_gap": 1}
    _accumulate_sim_stats(stats, {"exit_reason": "stop_loss", "stop_gap": False})
    assert stats["stop_gap"] == 1


def test_accumulate_ignores_none_and_filled_non_stop():
    """sim=None 或 tp2/timeout 成交 → 不计数（只统计跳过与跳空止损事件）。"""
    stats = {"n_skipped": 0, "same_day_both": 0, "stop_gap": 0}
    _accumulate_sim_stats(stats, None)
    _accumulate_sim_stats(stats, {"exit_reason": "tp2", "stop_gap": False})
    assert stats == {"n_skipped": 0, "same_day_both": 0, "stop_gap": 0}
