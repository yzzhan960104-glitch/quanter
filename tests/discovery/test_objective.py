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


def test_evaluate_wf_per_fold_independent_universe(monkeypatch):
    """P5：evaluate_wf 每折独立 universe + 折内 train/oos 分段（mock run_full_scan）。"""
    from discovery.objective import evaluate_wf
    from discovery.split import walk_forward_split

    loaded = []

    def fake_load(start, sel_end, data_end=None, warmup_days=180):
        loaded.append((start, sel_end, data_end))
        return {"300001.SZ": None}

    def fake_scan(params, universe):
        # 每折返回 1 条 train 信号（date=折中点）+ 1 条 oos 信号（date=oos 中点）
        from datetime import date
        import pandas as pd
        return [{"signal_date": date(2020, 6, 1), "avg_pnl_pct": 1.0},
                {"signal_date": date(2022, 6, 1), "avg_pnl_pct": 2.0}]

    import discovery.objective as obj
    import discovery.snapshot as snap
    # evaluate_wf 内部 lazy import load_universe_window → patch 物理路径（snapshot 模块）
    monkeypatch.setattr(snap, "load_universe_window", fake_load)
    monkeypatch.setattr(obj, "run_full_scan", fake_scan)

    wf = walk_forward_split(embargo_days=5)
    out = evaluate_wf({"window": 60}, wf)
    assert len(out) == 4
    assert [r["fold"] for r in out] == ["wf1_2020_21", "wf2_2022_23", "wf3_2024", "wf4_2025"]
    # 每折 universe 独立重建（train.start 选股截止 / oos.end 数据截止注入）
    t0, o0 = wf.folds[0][1], wf.folds[0][2]
    assert loaded[0] == (t0.start, t0.end, o0.end)
    # 折内分段：train 段收 2020 信号（n=1）、oos 段收 2022 信号（n=1）
    assert out[0]["train"]["n"] == 1
    assert out[0]["oos"]["n"] == 1


# ============================================================================
# A2（DG-G4 · 2026-08-14）：扩展切分 + inner 各年 min calmar（2025 特化教训）
# ============================================================================
def test_extended_split_spans():
    """扩展切分：inner 2021-2024 四个自然年（含 2022 熊市考场）/ outer 2025-2026。"""
    from discovery.split import extended_split
    sp = extended_split()
    assert sp.inner.start == date(2021, 1, 1) and sp.inner.end == date(2024, 12, 31)
    assert sp.outer.start == date(2025, 1, 1) and sp.outer.end == date(2026, 12, 31)
    assert sp.embargo_days == 5


def test_holdout_split_untouched_by_extended():
    """既有二段切分（45 caller 对照锚）不被扩展切分污染——oos/wf4 口径不动。"""
    from discovery.split import holdout_split
    old = holdout_split()
    assert old.inner.start == date(2025, 1, 1) and old.outer.start == date(2026, 1, 1)


def _filled_year(year, n, pnl):
    """合成 n 笔 signal_date 全在 year 年、每笔 pnl 的 all_filled 条目（日期分散）。"""
    return [{"avg_pnl_pct": pnl,
             "signal_date": pd.Timestamp(date(year, 3, 1) if i % 2 else date(year, 9, 1))}
            for i in range(n)]


def _filled_year_mixed(year, pnls):
    """合成混合盈亏年（wins+losses 均非空——纯全赢/全亏会触发 risk_metrics 的
    空集退化分支返 ann=0，测不出年际差异；A2 测试用混合分布对齐真实形态）。"""
    return [{"avg_pnl_pct": p,
             "signal_date": pd.Timestamp(date(year, 3, 1) if i % 2 else date(year, 9, 1))}
            for i, p in enumerate(pnls)]


def test_yearly_metrics_min_takes_worst_year():
    """两年一好一差 → min 恰取差年（好年不被整段淹没——A2 的全部物理意图）。"""
    from discovery.split import Segment
    from discovery.objective import yearly_metrics
    seg = Segment("t", date(2021, 1, 1), date(2024, 12, 31))
    good = _filled_year_mixed(2021, [6.0] * 40 + [-1.0] * 10)   # 80% 胜率高均盈
    bad = _filled_year_mixed(2022, [1.0] * 10 + [-3.0] * 40)    # 20% 胜率深亏
    ym = yearly_metrics(good + bad, seg)
    assert 2021 in ym and 2022 in ym
    assert ym[2022] < ym[2021]
    assert min(ym.values()) == ym[2022]


def test_yearly_metrics_sparse_year_scores_zero():
    """n<min_trades 的年份记 0.0（保守：信号缺席=逃考失败，不剔除不奖励）。"""
    from discovery.split import Segment
    from discovery.objective import yearly_metrics
    seg = Segment("t", date(2021, 1, 1), date(2024, 12, 31))
    filled = _filled_year(2021, 50, 5.0) + _filled_year(2022, 10, 5.0)  # 2022 仅 10 笔
    ym = yearly_metrics(filled, seg, min_trades=30)
    assert ym[2022] == 0.0


def test_yearly_metrics_excludes_out_of_segment():
    """segment 外年份不计入（与 segment_metrics 同界语义，inner/outer 隔离）。"""
    from discovery.split import Segment
    from discovery.objective import yearly_metrics
    seg = Segment("t", date(2021, 1, 1), date(2024, 12, 31))
    filled = _filled_year(2021, 50, 5.0) + _filled_year(2025, 50, 5.0)  # 2025 属 outer
    assert 2025 not in yearly_metrics(filled, seg)


def test_evaluate_injects_yearly_fields(monkeypatch):
    """evaluate 的 inner dict 注入 yearly_calmar + min_yearly_calmar（A2 排序新目标）。"""
    from unittest.mock import patch
    from discovery.split import extended_split
    from discovery.objective import evaluate
    # inner 两年信号 + outer 一年信号（2025 不进 inner yearly）+ 2023 无信号年不入 dict
    filled = (_filled_year(2021, 40, 5.0) + _filled_year(2022, 40, -1.0)
              + _filled_year(2025, 40, 8.0))
    with patch("discovery.objective.run_full_scan", return_value=filled):
        res = evaluate({}, {}, extended_split())
    assert set(res["inner"]["yearly_calmar"]) == {2021, 2022}
    assert res["inner"]["min_yearly_calmar"] == min(res["inner"]["yearly_calmar"].values())
    # 整段字段保留（feasibility_gate 等既有消费者兼容——纯增量不破坏）
    assert "calmar" in res["inner"] and "n" in res["inner"]


def test_evaluate_empty_inner_min_is_zero(monkeypatch):
    """inner 完全无信号 → yearly 空 dict，min_yearly_calmar 兜底 0.0（不抛）。"""
    from unittest.mock import patch
    from discovery.split import extended_split
    from discovery.objective import evaluate
    with patch("discovery.objective.run_full_scan", return_value=[]):
        res = evaluate({}, {}, extended_split())
    assert res["inner"]["yearly_calmar"] == {}
    assert res["inner"]["min_yearly_calmar"] == 0.0


# ============================================================================
# R1-1（2026-08-16）：组合约束口径评估——portfolio_metrics / evaluate_portfolio
# ============================================================================
from unittest.mock import patch

from discovery.objective import portfolio_metrics, evaluate_portfolio


def _mk_trade(sym, signal, entry, exit_, pnl):
    """合成 filled 记录（scan_symbol 产物键名：signal_date/buy_date/exit_date）。"""
    return {"symbol": sym, "signal_date": signal, "buy_date": entry,
            "exit_date": exit_, "avg_pnl_pct": pnl, "rr": 0.0}


@pytest.fixture
def seg_2025():
    from discovery.split import Segment
    return Segment("inner_2025", date(2025, 1, 1), date(2025, 12, 31))


@pytest.fixture
def dates_2025():
    return pd.date_range("2025-01-01", "2025-12-31", freq="B")


def test_portfolio_metrics_basic_math(seg_2025, dates_2025):
    """顺序三笔（+10%/-5%/+8%）→ equity=1+0.05×(逐笔扣双边5bps滑点)；win/dd/n 齐。"""
    trades = [
        _mk_trade("A", "2025-02-03", "2025-02-04", "2025-02-14", 10.0),
        _mk_trade("B", "2025-03-03", "2025-03-04", "2025-03-14", -5.0),
        _mk_trade("C", "2025-04-03", "2025-04-04", "2025-04-14", 8.0),
    ]
    m = portfolio_metrics(trades, seg_2025, dates_2025)
    slip = 5.0 * 2 / 10_000 * 100           # 双边 5bps×2=10bps=0.1%/笔（pnl_pct 折减）
    expect_equity = 1 + 0.05 * ((10.0 - slip) + (-5.0 - slip) + (8.0 - slip)) / 100
    assert m["n"] == 3 and m["n_taken"] == 3
    assert m["equity_end"] == pytest.approx(expect_equity, abs=1e-9)
    assert m["win_rate"] == pytest.approx(2 / 3)
    assert m["max_dd"] < 0                    # 第二笔亏损造成谷值（负值口径）
    assert m["ann"] > 0                       # 净值>1 → 正年化
    assert m["n_days"] == len(dates_2025)     # 交易日计数镜像 replay 口径


def test_portfolio_metrics_max_positions_cap(seg_2025, dates_2025):
    """7 笔全并发 → 默认 max_positions=6 只接 6 笔（n_taken=6，n=7）——口径裂缝的机理钉死。"""
    trades = [_mk_trade(f"S{i}", "2025-02-03", "2025-02-04", "2025-03-14", 5.0)
              for i in range(7)]
    m = portfolio_metrics(trades, seg_2025, dates_2025)
    assert m["n"] == 7 and m["n_taken"] == 6


def test_portfolio_metrics_embargo_excludes_boundary(seg_2025, dates_2025):
    """embargo=5：段起点+2 天的信号被跳过（吸收跨段持仓，segment_metrics 同源语义）。"""
    trades = [_mk_trade("A", "2025-01-02", "2025-01-03", "2025-01-13", 5.0),
              _mk_trade("B", "2025-02-03", "2025-02-04", "2025-02-14", 5.0)]
    m = portfolio_metrics(trades, seg_2025, dates_2025, embargo_days=5)
    assert m["n"] == 1                        # 只有 2 月那笔入段


def test_portfolio_metrics_slippage_zero_option(seg_2025, dates_2025):
    """零滑点模型注入：equity 不折减（研究对照口径）。"""
    from backtest.models import PositionModel
    trades = [_mk_trade("A", "2025-02-03", "2025-02-04", "2025-02-14", 10.0)]
    m = portfolio_metrics(trades, seg_2025, dates_2025,
                          position_model=PositionModel(slippage_bps=0.0))
    assert m["equity_end"] == pytest.approx(1.005)


def test_evaluate_portfolio_segments_and_yearly(monkeypatch, dates_2025):
    """接线：run_full_scan 桩 → inner/outer 分段隔离 + yearly_calmar/min 注入。"""
    filled = [
        _mk_trade("A", "2025-03-03", "2025-03-04", "2025-03-14", 10.0),
        _mk_trade("B", "2025-06-03", "2025-06-04", "2025-06-14", -4.0),
        _mk_trade("C", "2025-09-03", "2025-09-04", "2025-09-14", 12.0),
        _mk_trade("D", "2026-02-03", "2026-02-04", "2026-02-14", 6.0),
    ]
    fake_universe = {"S": pd.DataFrame(index=dates_2025)}
    monkeypatch.setattr("discovery.objective.run_full_scan", lambda *a, **kw: filled)
    from discovery.split import holdout_split
    res = evaluate_portfolio({}, fake_universe, holdout_split())
    assert res["n_total"] == 4
    assert res["inner"]["n"] == 3 and res["outer"]["n"] == 1   # 信息隔离分段
    assert 2025 in res["inner"]["yearly_calmar"]
    assert "min_yearly_calmar" in res["inner"]
    # 单年 3 笔 < 30 → 逃考惩罚记 0（yearly_metrics 同款保守口径）
    assert res["inner"]["yearly_calmar"][2025] == 0.0


def test_worker_objective_env_switch(monkeypatch):
    """R1-1：DISCOVERY_OBJECTIVE env 切搜索口径——默认 portfolio，scan 回滚口。"""
    from discovery import worker
    from discovery.objective import evaluate, evaluate_portfolio
    assert worker._objective_fn() is evaluate_portfolio          # 默认（R1 起新口径）
    monkeypatch.setenv("DISCOVERY_OBJECTIVE", "scan")
    assert worker._objective_fn() is evaluate                     # legacy 对照口
    monkeypatch.setenv("DISCOVERY_OBJECTIVE", "PORTFOLIO")
    assert worker._objective_fn() is evaluate_portfolio           # 大小写不敏感


def test_portfolio_metrics_feasibility_compatible(seg_2025, dates_2025):
    """下游契约：portfolio 指标过 feasibility_gate（max_dd≤0.4 ∧ n≥30）与 DSR 键。"""
    from discovery.judging import feasibility_gate
    trades = [_mk_trade(f"S{i}", f"2025-{m:02d}-03", f"2025-{m:02d}-04",
                        f"2025-{m:02d}-18", 4.0)
              for i, m in enumerate(range(1, 13), start=1)
              for _ in range(2)] +              [_mk_trade(f"U{i}", f"2025-{m:02d}-20", f"2025-{m:02d}-21",
                        f"2025-{m:02d}-27", 4.0)
              for i, m in enumerate(range(1, 7), start=1)]
    trades += [_mk_trade(f"T{i}", f"2025-{i + 6:02d}-03", f"2025-{i + 6:02d}-04",
                        f"2025-{i + 6:02d}-18", -3.0)
               for i in range(0, 6)]
    m = portfolio_metrics(trades, seg_2025, dates_2025)
    assert m["n"] == len(trades) >= 30
    assert m["max_dd"] <= 0.4                       # 负值口径过闸（与正值同义）
    assert feasibility_gate(m) is True
    assert "sharpe" in m and "kelly" in m           # DSR 门控/展示消费的补充键
