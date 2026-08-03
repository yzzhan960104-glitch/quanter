# -*- coding: utf-8 -*-
"""replay 核心统计测试（P1-4 · 此前 replay()/_compute_stats/ReplayReport 无直接覆盖）。

物理意图：回测引擎的聚合统计（胜率/平均盈亏比/回撤/月度/持仓天数）、净值曲线
（PositionModel）、字段契约（threshold_recommendation / metadata.cfg_min_rr）与
单 T 异常跳过语义，全部在此用真实 replay() + 合成策略钉死——不再依赖 worker 状态机
测试"顺带跑过"。
"""
import pandas as pd
import pytest

from backtest.models import PositionModel
from backtest.replay import ReplayReport, _compute_stats, replay
from strategies.neckline.signal import Signal


def _sig(symbol="A", formed="2024-01-02", entry="2024-01-03", exit="2024-01-10",
         rr=1.5, holding=5, pnl=4.0, reason="tp2"):
    return Signal(
        symbol=symbol, signal_type="neckline",
        formed_at=pd.Timestamp(formed), entry_date=pd.Timestamp(entry),
        entry_price=10.0, exit_date=pd.Timestamp(exit), exit_price=10.4,
        exit_reason=reason, rr=rr, holding_bars=holding, avg_pnl_pct=pnl,
    )


def _df(n=10, start="2024-01-01"):
    idx = pd.bdate_range(start, periods=n)
    return pd.DataFrame({
        "close": [10.0] * n, "high": [10.5] * n, "low": [9.5] * n,
        "volume": [1000.0] * n, "amount": [1e8] * n,
    }, index=idx)


class _FakeStrategy:
    """合成策略：指定 T → Signal；可注入单 T 异常。"""

    def __init__(self, signals_by_T=None, raise_on=None, id_cfg=None):
        self.signals_by_T = signals_by_T or {}
        self.raise_on = set(raise_on or [])
        self.id_cfg = id_cfg or {"min_rr": 1.5}

    def precompute(self, symbol, full_df):
        return {}

    def scan_at(self, symbol, df_T, T, state):
        if T in self.raise_on:
            raise RuntimeError("boom")
        return [self.signals_by_T[T]] if T in self.signals_by_T else []


def test_compute_stats_aggregation_hand_derived():
    """三笔 rr=2/-1/3：胜率 2/3、均 rr 4/3、月度 4。

    P1-1（2026-08-03）：max_drawdown 改为净值口径（equity 曲线峰谷，百分比），
    旧 rr 曲线口径保留为 max_dd_signal（累计 rr 2→1→4 → -1.0）。
    """
    hits = [
        _sig(rr=2.0, pnl=10.0),
        _sig(symbol="B", rr=-1.0, pnl=-5.0),
        _sig(symbol="C", rr=3.0, pnl=12.0),
    ]
    stats = _compute_stats(hits, PositionModel(slippage_bps=0))
    assert stats["n_hits"] == 3
    assert stats["win_rate"] == pytest.approx(2 / 3)
    assert stats["avg_rr"] == pytest.approx(4 / 3)
    # 净值口径：equity 1.005 → 1.0025（-0.2488%）→ 1.0085
    assert stats["max_drawdown"] == pytest.approx(1.0025 / 1.005 - 1.0)
    assert stats["max_dd_signal"] == pytest.approx(-1.0)
    assert stats["monthly_returns"] == {"2024-01": 4.0}
    assert stats["avg_holding_bars"] == 5
    assert stats["pattern_dist"] == {"neckline": 3}
    assert len(stats["trades"]) == 3
    # 净值：pos_cap=5% × (10% − 5% + 12%) 加总。
    assert stats["equity_curve"][-1]["equity"] == pytest.approx(1.0085)


def test_compute_stats_empty():
    """空命中 → 全零统计 + 空净值曲线（不除零）。"""
    stats = _compute_stats([], PositionModel(slippage_bps=0))
    assert stats == {
        "n_hits": 0, "win_rate": 0.0, "avg_rr": 0.0, "max_drawdown": 0.0,
        "pattern_dist": {}, "monthly_returns": {}, "avg_holding_bars": 0.0,
        "max_dd_signal": 0.0, "equity_curve": [], "trades": [],
    }


def test_compute_stats_first_trade_loss_captures_drawdown():
    """回归（2026-08-02 + P1-1）：首笔即亏损时回撤必须被记录。

    净值口径：equity 从 1.0 起，首笔 -5%×5% 仓位 → 0.9975，回撤 -0.25%；
    signal 口径保留（rr=-1 → -1.0），两条口径都不许漏算。
    """
    hits = [_sig(rr=-1.0, pnl=-5.0)]
    stats = _compute_stats(hits, PositionModel(slippage_bps=0))
    assert stats["n_hits"] == 1
    assert stats["win_rate"] == 0.0
    assert stats["max_drawdown"] == pytest.approx(0.9975 - 1.0)
    assert stats["max_dd_signal"] == pytest.approx(-1.0)


def test_replay_report_uses_renamed_threshold_field_with_legacy_alias():
    """P1-5：min_rr_ratio_recommendation → threshold_recommendation（旧名保留别名）。"""
    df = _df()
    T = df.index[2]
    strat = _FakeStrategy(signals_by_T={T: _sig(formed=str(T.date()), entry=str(df.index[3].date()),
                                                exit=str(df.index[5].date()))})
    report = replay({"A": df}, strat, "2024-01-01", "2024-01-12")
    assert isinstance(report, ReplayReport)
    assert "样本不足" in report.threshold_recommendation
    assert report.min_rr_ratio_recommendation == report.threshold_recommendation


def test_replay_metadata_reports_cfg_min_rr_from_strategy():
    """P1-5：metadata.cfg_min_rr 从策略 id_cfg["min_rr"] 读取（旧实现恒 None）。"""
    df = _df()
    strat = _FakeStrategy(id_cfg={"min_rr": 1.8})
    report = replay({"A": df}, strat, "2024-01-01", "2024-01-12")
    assert report.metadata["cfg_min_rr"] == 1.8
    assert "cfg_min_rr_ratio" not in report.metadata


def test_replay_skips_single_t_exception_but_keeps_later_hits():
    """单 T 异常不中断回放：前 2 个 T 抛错，第 3 个 T 仍正常产出命中。"""
    df = _df()
    T = df.index[2]
    strat = _FakeStrategy(signals_by_T={T: _sig()},
                          raise_on=[df.index[0], df.index[1]])
    report = replay({"A": df}, strat, "2024-01-01", "2024-01-12")
    assert report.n_hits == 1
    assert report.trades[0]["symbol"] == "A"


def test_replay_annualized_return_uses_equity_end_and_trading_days():
    """年化 CAGR = equity_end^(252/n_trading_days) − 1（手工推导）。"""
    df = _df()
    T = df.index[2]
    strat = _FakeStrategy(signals_by_T={T: _sig(rr=1.0, pnl=10.0)})
    # 零滑点显式传入：本测试聚焦年化公式，滑点扣减由
    # test_replay_default_position_model_applies_slippage_and_records_it 专项钉死。
    report = replay({"A": df}, strat, "2024-01-01", "2024-01-12",
                    position_model=PositionModel(slippage_bps=0))
    n_days = report.n_trading_days
    assert n_days == len(df)
    equity_end = 1.0 + 0.05 * 0.10
    assert report.equity_curve[-1]["equity"] == pytest.approx(equity_end)
    assert report.annualized_return == pytest.approx(equity_end ** (252.0 / n_days) - 1.0)


def test_replay_position_model_metadata_and_legacy_risk_frac():
    """PositionModel 随任务冻结进 metadata；risk_frac 非 None 走旧复利口径。"""
    df = _df()
    T = df.index[2]
    sig = _sig(rr=2.0, pnl=10.0)
    strat = _FakeStrategy(signals_by_T={T: sig})
    model = PositionModel(risk_frac=0.01)
    report = replay({"A": df}, strat, "2024-01-01", "2024-01-12", position_model=model)
    assert report.metadata["position_model"] == model.to_dict()
    assert report.equity_curve[-1]["equity"] == pytest.approx(1.02)


def test_replay_default_position_model_applies_slippage_and_records_it():
    """P0-2：replay 默认资金模型带保守 5bps 双边滑点，且 metadata 可见（可审计）。"""
    df = _df()
    T = df.index[2]
    strat = _FakeStrategy(signals_by_T={T: _sig(rr=1.0, pnl=10.0)})
    report = replay({"A": df}, strat, "2024-01-01", "2024-01-12")
    assert report.metadata["position_model"]["slippage_bps"] == 5.0
    # 默认模型：10% 收益 − 双边 10bps，pos_cap=5% 贡献 0.00495
    assert report.equity_curve[-1]["equity"] == pytest.approx(1.0 + 0.05 * (0.10 - 0.0010))


def test_replay_metadata_counts_single_t_exceptions_and_marks_degraded():
    """P1-4：单 T 异常不再静默——计数进 metadata，异常存在即 degraded=True。"""
    df = _df()
    strat = _FakeStrategy(raise_on=[df.index[0], df.index[1]])
    report = replay({"A": df}, strat, "2024-01-01", "2024-01-12")
    assert report.metadata["n_exceptions"] == 2
    assert report.metadata["degraded"] is True


def test_replay_clean_run_not_degraded():
    """无异常 → n_exceptions=0、degraded=False（自主管道只信任干净报告）。"""
    df = _df()
    strat = _FakeStrategy()
    report = replay({"A": df}, strat, "2024-01-01", "2024-01-12")
    assert report.metadata["n_exceptions"] == 0
    assert report.metadata["degraded"] is False


def test_replay_metadata_includes_strategy_skip_stats():
    """A3：策略级 skip/事件统计（same_day_both/stop_gap）透传进 metadata 供归因。"""
    df = _df()
    strat = _FakeStrategy()
    strat.skip_stats = {"n_skipped": 3, "same_day_both": 1, "stop_gap": 2}
    report = replay({"A": df}, strat, "2024-01-01", "2024-01-12")
    assert report.metadata["strategy_stats"] == {
        "n_skipped": 3, "same_day_both": 1, "stop_gap": 2,
    }


def test_replay_rejects_reused_strategy_instance():
    """P1-5：策略实例带跨 T 状态（cooldown 锚点）复用 → fail-fast 拒绝。"""
    df = _df()
    strat = _FakeStrategy()
    strat._last_signal_pos = {"A": 5}   # 模拟已跑过一次的颈线策略残留状态
    with pytest.raises(ValueError, match="新建策略实例"):
        replay({"A": df}, strat, "2024-01-01", "2024-01-12")
