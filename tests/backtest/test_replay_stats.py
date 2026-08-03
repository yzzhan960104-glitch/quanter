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
    """三笔 rr=2/-1/3：胜率 2/3、均 rr 4/3、最大回撤 -1（累计 2→1→4）、月度 4。"""
    hits = [
        _sig(rr=2.0, pnl=10.0),
        _sig(symbol="B", rr=-1.0, pnl=-5.0),
        _sig(symbol="C", rr=3.0, pnl=12.0),
    ]
    stats = _compute_stats(hits, PositionModel())
    assert stats["n_hits"] == 3
    assert stats["win_rate"] == pytest.approx(2 / 3)
    assert stats["avg_rr"] == pytest.approx(4 / 3)
    assert stats["max_drawdown"] == pytest.approx(-1.0)
    assert stats["monthly_returns"] == {"2024-01": 4.0}
    assert stats["avg_holding_bars"] == 5
    assert stats["pattern_dist"] == {"neckline": 3}
    assert len(stats["trades"]) == 3
    # 净值：pos_cap=5% × (10% − 5% + 12%) 加总。
    assert stats["equity_curve"][-1]["equity"] == pytest.approx(1.0085)


def test_compute_stats_empty():
    """空命中 → 全零统计 + 空净值曲线（不除零）。"""
    stats = _compute_stats([], PositionModel())
    assert stats == {
        "n_hits": 0, "win_rate": 0.0, "avg_rr": 0.0, "max_drawdown": 0.0,
        "pattern_dist": {}, "monthly_returns": {}, "avg_holding_bars": 0.0,
        "equity_curve": [], "trades": [],
    }


def test_compute_stats_first_trade_loss_captures_drawdown():
    """回归（2026-08-02）：首笔即亏损时回撤必须被记录。

    旧实现 peak 从 -inf 起，把亏损首笔当作「峰值」，单笔 rr=-1 的回测显示
    max_drawdown=0.0（漏算）——2026-07-12 回测的「胜率 0.0% / 回撤 0.0% /
    年化 -10.9%」即此病灶。净值归一化 equity_0=1.0，peak 必须从 0.0 起。
    """
    hits = [_sig(rr=-1.0, pnl=-5.0)]
    stats = _compute_stats(hits, PositionModel())
    assert stats["n_hits"] == 1
    assert stats["win_rate"] == 0.0
    assert stats["max_drawdown"] == pytest.approx(-1.0)


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
    report = replay({"A": df}, strat, "2024-01-01", "2024-01-12")
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
