# -*- coding: utf-8 -*-
"""L1 评估函数测试：分段/embargo/calmar 用合成 filled（快）；run_full_scan 集成标 slow。"""
from datetime import date

import pandas as pd
import pytest


def test_metrics_of_calmar():
    """calmar = ann / max_dd（max_dd→0 时 ann>0 给 inf，否则 0）。"""
    from discovery.objective import metrics_of
    pairs = [(1.0, pd.Timestamp("2025-06-01")), (2.0, pd.Timestamp("2025-06-02")),
             (-0.5, pd.Timestamp("2025-06-03"))]
    m = metrics_of(pairs)
    assert m["n"] == 3
    assert "calmar" in m
    if m["max_dd"] > 1e-9:
        assert abs(m["calmar"] - m["ann"] / m["max_dd"]) < 1e-6


def test_metrics_of_empty():
    from discovery.objective import metrics_of
    m = metrics_of([])
    assert m["n"] == 0 and m["calmar"] == 0.0


def test_segment_metrics_splits_by_date():
    """合成 filled 跨 2025/2026，segment_metrics 按段过滤 signal_date。"""
    from discovery.objective import segment_metrics
    from discovery.split import holdout_split
    split = holdout_split()
    all_filled = [
        {"avg_pnl_pct": 1.0, "signal_date": pd.Timestamp("2025-06-01")},
        {"avg_pnl_pct": 2.0, "signal_date": pd.Timestamp("2025-12-30")},
        {"avg_pnl_pct": 3.0, "signal_date": pd.Timestamp("2026-02-01")},
        {"avg_pnl_pct": 4.0, "signal_date": pd.Timestamp("2026-06-01")},
    ]
    assert segment_metrics(all_filled, split.inner, embargo_days=0)["n"] == 2
    assert segment_metrics(all_filled, split.outer, embargo_days=0)["n"] == 2


def test_embargo_skips_outer_boundary():
    """embargo_days 跳过 outer 开头 N 天信号（吸收 inner→outer 持仓跨越，spec §3.3）。"""
    from discovery.objective import segment_metrics
    from discovery.split import holdout_split
    split = holdout_split(embargo_days=31)   # 跳过 2026-01 全月
    all_filled = [
        {"avg_pnl_pct": 1.0, "signal_date": pd.Timestamp("2026-01-15")},   # embargo 内，剔除
        {"avg_pnl_pct": 2.0, "signal_date": pd.Timestamp("2026-02-15")},   # embargo 后，保留
    ]
    assert segment_metrics(all_filled, split.outer, embargo_days=31)["n"] == 1


def test_evaluate_returns_inner_outer(champion_params, synth_sym_df):
    """evaluate 用合成 universe（1 只 synth 标的）跑通，返回 inner/outer 两段 dict。
    合成数据信号可能为 0——只验证结构，不验证 ann>0（真实验证在 slow 集成）。"""
    from discovery.objective import evaluate
    from discovery.split import holdout_split
    universe = {"300001.SZ": synth_sym_df}   # fixture 直接注入合成 df
    res = evaluate(champion_params, universe, holdout_split())
    assert set(res.keys()) >= {"inner", "outer", "n_total"}
    # embargo=0 时 inner+outer 笔数 = 全部（合成数据可能 0 笔，0==0+0 也满足）
    assert res["n_total"] == res["inner"]["n"] + res["outer"]["n"]


def test_evaluate_replay_uses_replay_report_metrics(champion_params, synth_sym_df):
    """v2：evaluate_replay 用 replay 引擎产出 ReplayReport 口径指标（非 kelly/calmar）。"""
    from discovery.objective import evaluate_replay
    from discovery.split import holdout_split
    universe = {"300001.SZ": synth_sym_df}
    res = evaluate_replay(champion_params, universe, holdout_split())
    assert set(res.keys()) >= {"inner", "outer", "n_total", "report"}
    for m in (res["inner"], res["outer"]):
        assert {"n_hits", "win_rate", "avg_rr", "max_drawdown",
                "annualized_return"} <= set(m.keys())
    assert res["n_total"] == res["inner"]["n_hits"] + res["outer"]["n_hits"]


def test_evaluate_replay_single_segment_override(champion_params, synth_sym_df):
    """v2：显式 start/end → 只评单段（inner），outer 空（供任意区间回测任务）。"""
    from discovery.objective import evaluate_replay
    from discovery.split import holdout_split
    universe = {"300001.SZ": synth_sym_df}
    res = evaluate_replay(champion_params, universe, holdout_split(),
                          start="2025-01-01", end="2025-06-30")
    assert res["outer"] == {}
    assert res["n_total"] == res["inner"]["n_hits"]


@pytest.mark.slow
def test_evaluate_champion_real(champion_params):
    """集成：当前冠军真实 evaluate（~3min），复现探查锚——2026 outer ann>0（探查实证 145-182%）。"""
    from discovery.objective import evaluate
    from discovery.snapshot import freeze
    from discovery.split import holdout_split
    universe, _ = freeze()
    res = evaluate(champion_params, universe, holdout_split())
    assert res["inner"]["n"] > 0
    assert res["outer"]["n"] > 0
    assert res["outer"]["ann"] > 0   # 复现探查：2026 未塌
