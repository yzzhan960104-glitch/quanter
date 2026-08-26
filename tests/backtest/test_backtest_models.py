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
    curve = build_equity_curve(trades, PositionModel(
        capital=1_000_000, pos_cap=0.05, max_positions=10, slippage_bps=0))
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
    curve = build_equity_curve(trades, PositionModel(
        capital=1_000_000, pos_cap=0.05, max_positions=2, slippage_bps=0))
    assert len(curve) == 2
    assert curve[-1]["equity"] == pytest.approx(1.005)
    assert curve[-1]["cumulative_rr"] == pytest.approx(2.0)


def test_cash_insufficient_skips_trade():
    """现金账：单笔 allocation=capital×pos_cap，现金不足则跳过（防超买）。"""
    trades = [
        _t("A", "2024-01-02", "2024-01-10", 1.0, 5.0),
        _t("B", "2024-01-03", "2024-01-11", 1.0, 5.0),
    ]
    curve = build_equity_curve(trades, PositionModel(
        capital=100.0, pos_cap=0.6, max_positions=10, slippage_bps=0))
    assert len(curve) == 1
    assert curve[0]["equity"] == pytest.approx(1.03)


def test_sequential_positions_not_counted_concurrent():
    """区间不重叠（前一笔已到期）→ 不占并发额度，均能进场。"""
    trades = [
        _t("A", "2024-01-02", "2024-01-05", 1.0, 5.0),
        _t("B", "2024-01-06", "2024-01-09", 1.0, 5.0),
    ]
    curve = build_equity_curve(trades, PositionModel(
        capital=1_000_000, pos_cap=0.05, max_positions=1, slippage_bps=0))
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


def test_default_slippage_is_conservative_5bps():
    """P0-2（2026-08-03）：PositionModel 默认双边滑点 5bps（共 10bps）。

    物理意图：主回测链路此前零滑点，而真实止损/超时卖出有冲击成本。默认保守
    5bps 让回测收益不再系统性高估；任务级可显式覆盖（如零费率研究任务传 0）。
    """
    assert PositionModel().slippage_bps == 5.0
    trades = [_t("A", "2024-01-02", "2024-01-05", 2.0, 10.0)]
    curve = build_equity_curve(trades, PositionModel(pos_cap=0.05))
    # 10% 收益 − 双边 10bps → 净 9.9%，pos_cap 5% 贡献 = 0.00495
    assert curve[0]["equity"] == pytest.approx(1.0 + 0.05 * (0.10 - 0.0010))


# ============================================================================
# R7-P2 质量分层（2026-08-26 信号质量方案）：quality_alloc 按笔弹性仓位
# ============================================================================
def test_quality_alloc_uses_per_trade_pos_cap():
    """quality_alloc=True：流水带 pos_cap 时每笔仓位=capital×该笔 pos_cap。

    手推：A 笔 pos_cap=0.10 × 10% → +0.010；B 笔 pos_cap=0.03 × −5% → −0.0015
    → equity 终值 1.0085（固定 5% 口径则为 1.0025——两口径在此分叉）。
    """
    a = _t("A", "2024-01-02", "2024-01-05", 2.0, 10.0)
    b = _t("B", "2024-02-01", "2024-02-03", -1.0, -5.0)
    a["pos_cap"], b["pos_cap"] = 0.10, 0.03
    curve = build_equity_curve([a, b], PositionModel(
        capital=1_000_000, pos_cap=0.05, max_positions=10, slippage_bps=0,
        quality_alloc=True))
    assert curve[0]["equity"] == pytest.approx(1.010)
    assert curve[-1]["equity"] == pytest.approx(1.0085)


def test_quality_alloc_off_ignores_pos_cap_key():
    """默认关（零回归）：流水带 pos_cap 键也不生效——固定 pos_cap 口径逐位不变。"""
    a = _t("A", "2024-01-02", "2024-01-05", 2.0, 10.0)
    a["pos_cap"] = 0.10
    curve = build_equity_curve([a], PositionModel(
        capital=1_000_000, pos_cap=0.05, slippage_bps=0))
    assert curve[0]["equity"] == pytest.approx(1.005)


def test_quality_alloc_missing_key_falls_back():
    """quality_alloc=True 但流水无 pos_cap 键 → 回落固定 pos_cap（不炸）。"""
    curve = build_equity_curve([_t("A", "2024-01-02", "2024-01-05", 2.0, 10.0)],
                               PositionModel(pos_cap=0.05, slippage_bps=0,
                                             quality_alloc=True))
    assert curve[0]["equity"] == pytest.approx(1.005)


def test_return_taken_identifies_taken_subset():
    """return_taken=True：返回真正进净值的流水子集（被并发闸跳过的不在）。

    R7-P2 拥挤日闸需要逐笔归属（taken 集的 same_day_n 分层统计）。
    """
    trades = [
        _t("A", "2024-01-02", "2024-01-10", 1.0, 5.0),
        _t("B", "2024-01-03", "2024-01-11", 1.0, 5.0),
        _t("C", "2024-01-04", "2024-01-12", 1.0, 5.0),   # 并发满 2 被跳过
    ]
    curve, taken = build_equity_curve(trades, PositionModel(
        pos_cap=0.05, max_positions=2, slippage_bps=0), return_taken=True)
    assert len(curve) == 2 and len(taken) == 2
    assert [t["symbol"] for t in taken] == ["A", "B"]
    # 默认返回形状不变（零回归）
    assert build_equity_curve(trades, PositionModel(
        pos_cap=0.05, max_positions=2, slippage_bps=0))[0]["equity"] == curve[0]["equity"]
