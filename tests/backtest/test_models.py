# -*- coding: utf-8 -*-
"""PositionModel 组合资金模型测试（P0-1 · 回测/实盘资金口径统一）。

物理意图：replay 旧净值模型是"每笔固定 1% 风险复利"（RISK_FRAC=0.01），实盘是
"capital × pos_cap(5%) 单笔仓位、最多 N 笔并发"。本测试钉死新 PositionModel 的
净值语义：单笔仓位 pos_cap 加总、并发上限 max_positions、现金不足跳过、滑点扣减，
以及旧 risk_frac 口径的向后兼容。
"""
import pytest

from backtest.models import PositionModel, build_equity_curve


def _t(sym, entry, exit_, rr, pnl_pct):
    """构造 trades dict（与 _compute_stats 产出的流水同键集）。"""
    return {
        "symbol": sym, "entry_date": entry, "exit_date": exit_,
        "rr": rr, "avg_pnl_pct": pnl_pct,
    }


def test_pos_cap_additive_equity_hand_derived():
    """pos_cap 模式：每笔收益 = pos_cap × pnl%，加总到净值（不复利）。

    生产代码若改成复利（1.005×1.0025）或按 rr 放大会在此失败。
    """
    trades = [
        _t("A", "2024-01-02", "2024-01-05", 2.0, 10.0),
        _t("B", "2024-02-01", "2024-02-03", -1.0, -5.0),
    ]
    curve = build_equity_curve(trades, PositionModel(capital=1_000_000, pos_cap=0.05, max_positions=10))
    assert len(curve) == 2
    assert curve[0]["equity"] == pytest.approx(1.005)
    assert curve[1]["equity"] == pytest.approx(1.0025)
    assert curve[1]["cumulative_rr"] == pytest.approx(1.0)
    assert curve[1]["pnl_pct"] == pytest.approx(-5.0)


def test_max_positions_skips_overlapping_trades():
    """并发持仓上限：区间重叠的第 3 笔不进净值（也不进 cumulative_rr）。"""
    trades = [
        _t("A", "2024-01-02", "2024-01-10", 1.0, 5.0),
        _t("B", "2024-01-03", "2024-01-11", 1.0, 5.0),
        _t("C", "2024-01-04", "2024-01-12", 1.0, 5.0),
    ]
    curve = build_equity_curve(trades, PositionModel(capital=1_000_000, pos_cap=0.05, max_positions=2))
    assert len(curve) == 2
    assert curve[-1]["equity"] == pytest.approx(1.005)
    assert curve[-1]["cumulative_rr"] == pytest.approx(2.0)


def test_cash_insufficient_skips_trade():
    """现金账：单笔 allocation=capital×pos_cap，现金不足则跳过（防超买）。"""
    trades = [
        _t("A", "2024-01-02", "2024-01-10", 1.0, 5.0),
        _t("B", "2024-01-03", "2024-01-11", 1.0, 5.0),
    ]
    curve = build_equity_curve(trades, PositionModel(capital=100.0, pos_cap=0.6, max_positions=10))
    assert len(curve) == 1
    assert curve[0]["equity"] == pytest.approx(1.03)


def test_sequential_positions_not_counted_concurrent():
    """区间不重叠（前一笔已到期）→ 不占并发额度，均能进场。"""
    trades = [
        _t("A", "2024-01-02", "2024-01-05", 1.0, 5.0),
        _t("B", "2024-01-06", "2024-01-09", 1.0, 5.0),
    ]
    curve = build_equity_curve(trades, PositionModel(capital=1_000_000, pos_cap=0.05, max_positions=1))
    assert len(curve) == 2


def test_risk_frac_legacy_compounds_rr():
    """向后兼容：risk_frac 非 None 时沿用旧口径 Π(1+rr×risk_frac)。"""
    trades = [_t("A", "2024-01-02", "2024-01-05", 2.0, 10.0),
              _t("B", "2024-02-01", "2024-02-03", -1.0, -5.0)]
    curve = build_equity_curve(trades, PositionModel(risk_frac=0.01))
    assert curve[0]["equity"] == pytest.approx(1.02)
    assert curve[1]["equity"] == pytest.approx(1.0098)
    assert curve[1]["cumulative_rr"] == pytest.approx(1.0)


def test_slippage_reduces_position_return():
    """滑点扣减：双边 slippage_bps 从每笔收益中扣除（10% 收益 - 10bps×2）。"""
    trades = [_t("A", "2024-01-02", "2024-01-05", 2.0, 10.0)]
    curve = build_equity_curve(trades, PositionModel(pos_cap=0.05, slippage_bps=10))
    assert curve[0]["equity"] == pytest.approx(1.0 + 0.05 * (0.10 - 0.002))


def test_empty_trades_returns_empty_curve():
    """空流水 → 空曲线（不除零、不抛）。"""
    assert build_equity_curve([], PositionModel()) == []
