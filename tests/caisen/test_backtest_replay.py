# -*- coding: utf-8 -*-
"""历史回放验证器测试（蔡森形态学流水线 Phase 2 · Task 10）。

物理意图与覆盖节点（CLAUDE.md 极简 + 无前视红线）：
  本测试验证蔡森策略上线 gate——历史回放验证器。它对每个交易日 T 用【T 及之前】
  数据滚动跑 PatternScreener→TradePlanGenerator，模拟 T+1 回踩成交 + 止盈/止损/
  时间止损离场，统计胜率/平均盈亏比/最大回撤/命中数/形态分布。

  核心红线（无前视断言）：对序列裁剪末段，前段回放结果必须一致——因为 T 日决策
  严格只用 .loc[:T]，末段未来数据不参与 T 日计算，裁剪不应改变历史判定。

  覆盖节点：
    - test_replay_no_lookahead：合成历史序列（W底 + 满足涨幅段），回放统计合理；
      裁剪末段前段结果一致（无前视红线）；
    - test_replay_records_hits_and_exits：命中数 + 离场类型（止盈/止损/时间止损）
      记录正确；
    - test_replay_stats：胜率/平均盈亏比/最大回撤计算正确（合成已知结果序列验证）；
    - test_replay_min_rr_sensitivity：宽松 min_rr_ratio(1.5) 比严格(3.0) 命中更多，
      证明 rr 阈值影响样本量（Task 9 rr 张力承袭）。

合成序列设计（复用 Task 6/8 已验证的 _build_standard_w_bottom 序列并延伸满足段）：
  - 前段（~20 根）：标准 W 底（depth≈0.467，右脚抬高，颈线突破）；
  - 后段（延伸）：回踩颈线（触发买入）→ 第一波满足（止盈）→ 第二波满足（止盈），
    或破底（止损）或横盘超时（时间止损），视用例需要构造不同结局。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from caisen.config import StrategyConfig
from caisen.risk import RiskManager
from caisen.patterns.screener import PatternScreener
from caisen.backtest_replay import replay, ReplayReport


# ---------------------------------------------------------------------------
# 合成序列构造（复用 Task 8 标准W底序列，延伸满足/止损/超时段）
# ---------------------------------------------------------------------------
def _atr_const(n: int, val: float = 1.0) -> pd.Series:
    """构造常数 ATR 序列（同 Task 6/8），使 causal_pivots 阈值稳定。"""
    return pd.Series(val, index=pd.RangeIndex(n), dtype=float)


def _w_vol_pattern(n: int, p1_i: int, p2_i: int, p3_i: int, p4_i: int) -> pd.Series:
    """W 底量价模式（同 Task 8）：左底放量 + 右底缩量 + 突破放量。"""
    vol = pd.Series(200.0, index=pd.RangeIndex(n))
    vol.iloc[p1_i] = 300.0   # 左底放量
    vol.iloc[p3_i] = 100.0   # 右底缩量
    vol.iloc[p4_i] = 500.0   # 突破日放量
    return vol


def _mk_cfg(**overrides) -> StrategyConfig:
    """构造回放测试用 StrategyConfig（对齐 Task 8 _mk_cfg 的形态/量价参数）。

    关键（承 Task 9 rr 张力）：min_rr_ratio 用宽松值（默认 1.5）以保证标准 W 底
    （breakout≈neckline 时 rr≈1.0）的计划能进入回放样本。生产默认 3.0 会过滤掉
    所有标准突破入场计划，回放无样本可统计。
    """
    base = dict(
        min_pattern_bars=11,
        max_pattern_bars=60,
        zigzag_threshold_atr=0.5,
        confirm_bars=2,
        w_price_tolerance=0.05,
        min_pattern_depth=0.05,
        max_pattern_depth=0.50,        # 标准W底 depth≈0.467 通过
        hs_max_pattern_depth=1.0,
        pattern_tension_ratio=0.05,
        right_vol_shrink=0.8,
        breakout_vol_multiplier=1.5,
        right_above_left=True,
        ma26w_filter=False,
        abc_wave_detect=False,
        liquidity_min_amount=1e8,
        hv_window=20,
        hv_max_quantile=0.95,
        min_rr_ratio=1.5,              # 宽松 rr——保证标准突破入场计划不被过滤
        pullback_window_bars=3,
        max_holding_bars=15,
        timeout_exit_threshold=0.01,
    )
    base.update(overrides)
    return StrategyConfig(**base)


def _build_w_bottom_with_rise(n_tail: int = 18, target_mult: float = 1.5) -> tuple:
    """合成"W底 + 后续满足涨幅"序列。

    前段：Task 8 标准 W 底（20 根，右脚8.0抬高/左脚7.5，颈线≈11，depth≈0.467，
          末根收盘≈12.0≈颈线突破点附近）。
    后段（n_tail 根）：构造回踩 + 上涨——
        回踩段：close 回落到 breakout(≈12) 的 pullback 区间（≤2% 内）触发买入；
        上涨段：close 单边上扬至 neck + n×H（满足点）。

    参数：
        n_tail:    后段延伸根数（含回踩+上涨）；
        target_mult: 上涨目标相对颈线的倍数（1.5 ≈ 第一波满足 neck+H=12.5 附近）。
    返回 (close, high, low, vol) 四序列（pd.Series，index=RangeIndex）。
    """
    # —— 前段标准 W 底（20 根，breakout 处 neck≈11, P2高点11，depth≈0.467）——
    pre_close = pd.Series(
        [12.0, 11.0, 10.0, 9.0, 8.0, 7.5,
         8.0, 8.5, 9.0, 10.0, 11.0,
         10.0, 9.0, 8.0,
         9.0, 10.0, 11.0, 13.0,
         12.5, 12.0],
        dtype=float,
    )
    # —— 后段：回踩 + 单边上涨到 target_mult × 颈线(11) ≈ 16.5 ——
    neck = 11.0
    target = neck * target_mult            # 上涨目标
    pullback_price = 12.0 * 0.99           # 回踩到 11.88（breakout 12 的 1% 回踩，落入 pullback 区间）
    # 回踩 2 根 + 上涨 (n_tail-2) 根（线性插值到 target）
    pullback_seg = [pullback_price, pullback_price - 0.1]
    rise_seg = np.linspace(pullback_seg[-1], target, n_tail - 2).tolist()
    tail_close = pullback_seg + rise_seg

    close = pd.concat([pre_close, pd.Series(tail_close, dtype=float)], ignore_index=True)
    high = close + 0.3
    low = close - 0.3
    # vol：前段标准 W 底量价模式，后段温和放量上涨
    vol = _w_vol_pattern(len(close), p1_i=5, p2_i=10, p3_i=13, p4_i=17)
    tail_vol = pd.Series(250.0, index=pd.RangeIndex(len(pre_close), len(close)))
    vol.iloc[len(pre_close):] = tail_vol.values
    return close, high, low, vol


def _mk_price_df(close, high, low, vol, amount_per_bar: float = 2e8) -> pd.DataFrame:
    """把合成 close/high/low/vol 拼成 price_data 项 DataFrame（index=RangeIndex）。

    复用 Task 8 _mk_price_df 契约。amount 取常数（≥ liquidity_min_amount=1e8 通过）。
    """
    n = len(close)
    return pd.DataFrame({
        "close": close.values,
        "high": high.values,
        "low": low.values,
        "volume": vol.values,
        "amount": pd.Series(amount_per_bar, index=pd.RangeIndex(n), dtype=float).values,
    }, index=pd.RangeIndex(n))


# ---------------------------------------------------------------------------
# 1. 无前视红线（核心 gate）+ 基本回放
# ---------------------------------------------------------------------------
class TestReplayNoLookahead:
    """回放严格无前视：T 日决策只用 .loc[:T]，裁剪末段前段结果一致。"""

    def test_replay_no_lookahead(self):
        """合成历史序列（W底 + 满足涨幅段），回放胜率/盈亏比合理；裁剪末段前段一致。

        红线断言：对完整序列回放得到的命中记录，与裁剪末段 K 根后回放得到的【前段】
        命中记录完全一致——因为 T 日决策只用 .loc[:T]，末段未来数据不参与 T 日计算。
        若回放实现意外前视（如对全序列预算 pivot/plan），裁剪后前段结果会漂移。
        """
        cfg = _mk_cfg()
        rm = RiskManager(cfg)
        close, high, low, vol = _build_w_bottom_with_rise(n_tail=18, target_mult=1.5)
        df = _mk_price_df(close, high, low, vol)
        price_data = {"TEST": df}

        # 完整序列回放（T 在 [start, end] 滚动）
        start = df.index[15]
        end = df.index[-1]
        report_full = replay(price_data, cfg, rm, start=start, end=end, aum=1e6)

        # —— 基本合理性：报告字段类型正确，胜率/盈亏比在合理范围 ——
        assert isinstance(report_full, ReplayReport)
        assert 0.0 <= report_full.win_rate <= 1.0
        assert report_full.n_hits >= 0
        assert isinstance(report_full.pattern_dist, dict)
        assert isinstance(report_full.monthly_returns, dict)
        assert report_full.max_drawdown <= 0.0  # 回撤非正（0 或负）

        # —— 无前视红线：裁剪末 8 根，前段命中记录应与完整序列的前段完全一致 ——
        df_short = df.iloc[:-8].copy()
        price_data_short = {"TEST": df_short}
        start_s = df_short.index[15]
        end_s = df_short.index[-1]
        report_short = replay(price_data_short, cfg, rm, start=start_s, end=end_s, aum=1e6)

        # —— 无前视红线（精确语义）——
        # 裁剪末 8 根后，回放在裁剪序列上的【入场决策】必须与完整序列相同（T 日决策只用
        # .loc[:T]）。但【离场】只有在裁剪点之前完成的交易才可比较——裁剪点之后离场的
        # 交易在裁剪序列中无对应数据（变为 still_open），这是物理事实而非前视。
        # 故精确红线断言：
        #   1. 入场集合（entry_day ≤ cutoff 且 entry_price）完全一致（无前视核心）；
        #   2. 在 cutoff 之前离场的交易（exit_day ≤ cutoff），离场字段完全一致。
        cutoff = df_short.index[-1]
        full_hits = report_full.metadata.get("hits", []) if hasattr(report_full, "metadata") else []
        short_hits = report_short.metadata.get("hits", []) if hasattr(report_short, "metadata") else []

        # 1. 入场集合一致：entry_day ≤ cutoff 的命中，entry_day/entry_price 应一一对应
        full_entries = [(h["entry_day"], round(h["entry_price"], 4))
                        for h in full_hits if h["entry_day"] <= cutoff]
        short_entries = [(h["entry_day"], round(h["entry_price"], 4)) for h in short_hits]
        assert full_entries == short_entries, (
            f"无前视红线违反：入场决策不一致。\n完整序列前段入场={full_entries}\n"
            f"裁剪序列入场={short_entries}"
        )

        # 2. 在 cutoff 之前离场的交易，离场字段完全一致（formed_at/exit_reason/exit_price）
        full_exited_early = {h["entry_day"]: h for h in full_hits
                             if h["exit_day"] <= cutoff}
        short_exited_early = {h["entry_day"]: h for h in short_hits
                              if h["exit_day"] <= cutoff and h["exit_reason"] != "still_open"}
        for ed, full_h in full_exited_early.items():
            assert ed in short_exited_early, (
                f"无前视红线违反：entry_day={ed} 在完整序列于 cutoff 前离场，"
                f"但裁剪序列未记录该离场"
            )
            sh = short_exited_early[ed]
            assert full_h["exit_reason"] == sh["exit_reason"], (
                f"entry_day={ed} 离场类型不一致：full={full_h['exit_reason']} "
                f"short={sh['exit_reason']}"
            )
            assert full_h["exit_price"] == pytest.approx(sh["exit_price"], rel=1e-6), (
                f"entry_day={ed} 离场价不一致：full={full_h['exit_price']} "
                f"short={sh['exit_price']}"
            )


# ---------------------------------------------------------------------------
# 2. 命中记录 + 离场类型（止盈/止损/时间止损）
# ---------------------------------------------------------------------------
class TestReplayRecordsHitsAndExits:
    """回放记录命中数与离场类型（take_profit / stop_loss / timeout）。"""

    def test_replay_records_hits_and_exits(self):
        """合成序列含 W底 + 回踩 + 上涨到第一波满足 → 至少 1 命中 + 离场类型合法。

        断言：命中记录非空，每条命中的 exit_reason ∈ {take_profit, stop_loss,
        timeout, still_open}，entry_price 落在回踩区间内。
        """
        cfg = _mk_cfg()
        rm = RiskManager(cfg)
        close, high, low, vol = _build_w_bottom_with_rise(n_tail=18, target_mult=1.5)
        df = _mk_price_df(close, high, low, vol)
        price_data = {"TEST": df}

        start = df.index[15]
        end = df.index[-1]
        report = replay(price_data, cfg, rm, start=start, end=end, aum=1e6)

        # 命中记录应非空（W底在前段形成，回踩在后段触发）
        hits = report.metadata.get("hits", []) if hasattr(report, "metadata") else []
        assert len(hits) >= 1, "W底序列回放应至少产生 1 条命中记录"

        legal_exits = {"take_profit", "stop_loss", "timeout", "still_open"}
        for h in hits:
            assert h["exit_reason"] in legal_exits, (
                f"离场类型非法：{h['exit_reason']} 不在 {legal_exits}"
            )
            # entry_price 应落在回踩区间 [entry_lower, entry_upper]（由 plan 定义）
            assert "entry_price" in h
            assert "entry_upper" in h
            assert "entry_lower" in h
            assert h["entry_lower"] - 1e-6 <= h["entry_price"] <= h["entry_upper"] + 1e-6
            # 盈亏比 rr（单笔）应有记录
            assert "rr" in h
            assert isinstance(h["rr"], (int, float))


# ---------------------------------------------------------------------------
# 3. 胜率/平均盈亏比/最大回撤计算（合成已知结果序列验证）
# ---------------------------------------------------------------------------
class TestReplayStats:
    """胜率/平均盈亏比/最大回撤计算正确性（用合成已知结果序列验证）。"""

    def test_replay_stats_known_sequence(self):
        """合成已知 rr 序列：3 笔 +1R、1 笔 -1R → 胜率 75%、平均 rr 0.5R。

        用 replay 的纯统计函数（_compute_stats）直接喂数据验证计算逻辑，
        避免依赖完整回放链路的随机性（合成 W底 的具体 rr 受 screener 影响）。
        """
        from caisen.backtest_replay import _compute_stats

        # 4 笔交易：3 盈 1 亏，rr 分别 +1.0/+1.0/+1.0/-1.0，每笔持仓 5 个交易日
        trades = [
            {"rr": 1.0, "entry_date": pd.Timestamp("2024-01-15"),
             "exit_date": pd.Timestamp("2024-01-20"), "exit_reason": "take_profit",
             "pattern_type": "w_bottom", "holding_bars": 5},
            {"rr": 1.0, "entry_date": pd.Timestamp("2024-02-05"),
             "exit_date": pd.Timestamp("2024-02-10"), "exit_reason": "take_profit",
             "pattern_type": "w_bottom", "holding_bars": 5},
            {"rr": -1.0, "entry_date": pd.Timestamp("2024-02-20"),
             "exit_date": pd.Timestamp("2024-02-25"), "exit_reason": "stop_loss",
             "pattern_type": "w_bottom", "holding_bars": 5},
            {"rr": 1.0, "entry_date": pd.Timestamp("2024-03-01"),
             "exit_date": pd.Timestamp("2024-03-06"), "exit_reason": "take_profit",
             "pattern_type": "head_shoulder", "holding_bars": 5},
        ]
        stats = _compute_stats(trades)

        # 胜率 = 3/4 = 0.75
        assert stats["win_rate"] == pytest.approx(0.75, abs=1e-9)
        # 平均 rr = (1+1-1+1)/4 = 0.5
        assert stats["avg_rr"] == pytest.approx(0.5, abs=1e-9)
        # n_hits = 4
        assert stats["n_hits"] == 4
        # 形态分布：w_bottom 3, head_shoulder 1
        assert stats["pattern_dist"]["w_bottom"] == 3
        assert stats["pattern_dist"]["head_shoulder"] == 1
        # 最大回撤：累计 rr 曲线 [1,2,1,2]，峰=2 谷=1 → 回撤 -1.0（按 rr 累计近似）
        # 回撤定义：peak-to-trough 最大跌幅。序列 [1,2,1,2]：
        #   running_max=[1,2,2,2], drawdown=[0,0,-1,0] → max_drawdown=-1.0
        assert stats["max_drawdown"] == pytest.approx(-1.0, abs=1e-9)
        # 平均持仓天数
        assert stats["avg_holding_bars"] == pytest.approx(5.0, abs=1e-9)  # 每笔 5 天

    def test_replay_stats_empty(self):
        """空交易序列 → 全零统计（不抛异常，不除零）。"""
        from caisen.backtest_replay import _compute_stats
        stats = _compute_stats([])
        assert stats["n_hits"] == 0
        assert stats["win_rate"] == 0.0
        assert stats["avg_rr"] == 0.0
        assert stats["max_drawdown"] == 0.0
        assert stats["pattern_dist"] == {}


# ---------------------------------------------------------------------------
# 3.5 时间止损语义（B-3：回测须与实盘 check_exit 对齐为「砍亏」）
# ---------------------------------------------------------------------------
class TestTimeoutExitSemantics:
    """时间止损语义统一：超时 + 浮盈不足 → 砍亏离场（与实盘 check_exit 一致）。

    B-3 缺陷：旧回测 _simulate_one_trade 用 `unrealized>=threshold`(R 分母)→锁盈离场，
    与实盘 check_exit 的 `profit<threshold`(% 分母)→砍亏离场 运算符/分母/意图全反。
    后果：超时浮亏单在回测中不实现亏损（继续持有到末尾记 still_open），系统性虚高
    回测胜率/盈亏比，可能放行实盘亏损策略通过上线 gate。

    本测试构造「超时浮亏」场景，断言回测应 timeout 砍亏（rr<0），而非 still_open。
    """

    def test_simulate_timeout_cuts_loser(self):
        """超时且浮亏 → timeout 离场 + 负 rr（砍亏），非 still_open 持有到末尾。"""
        from caisen.backtest_replay import _simulate_one_trade
        from caisen.plan import TradePlan

        plan = TradePlan(
            plan_id="t", symbol="X.SZ", pattern_type="w_bottom",
            formed_at=pd.Timestamp("2024-01-01"),
            breakout_price=10.0, neckline_price=10.0, bottom_price=8.0, H=2.0,
            entry_upper=10.0, entry_lower=9.8,
            stop_loss=8.0, take_profit=12.0, take_profit_2x=14.0,
            rr_ratio=1.0, valid_until=pd.Timestamp("2024-01-05"),
            max_holding_until=pd.Timestamp("2024-01-20"),
            timeout_exit_threshold=0.01, shares=100, metadata={},
        )
        # entry_day=0；T+1(=1) 回踩触发（low<=10 且 high>=9.8）；其后收盘 9.9 缓慢阴跌。
        # entry_price=10.0；close=9.9 → profit=(9.9-10)/10=-0.01 < threshold 0.01 → 砍亏。
        # 全程不触止损(8.0)/止盈(12/14)。
        closes = [10.5, 9.9] + [9.9] * 8
        highs = [11.0, 10.2] + [10.1] * 8
        lows = [10.0, 9.7] + [9.8] * 8
        df = pd.DataFrame(
            {"close": closes, "high": highs, "low": lows,
             "volume": [100] * 10, "amount": [1e8] * 10},
            index=pd.RangeIndex(10),
        )
        hit = _simulate_one_trade(df, plan, entry_day=0, max_holding_bars=3)
        assert hit is not None, "回踩应触发成交"
        assert hit["exit_reason"] == "timeout", (
            f"超时浮亏应 timeout 砍亏（B-3），实际 {hit['exit_reason']}"
        )
        assert hit["rr"] < 0, "砍亏离场 rr 应为负"

    def test_simulate_timeout_holds_when_profit_meets_threshold(self):
        """超时但浮盈 ≥ threshold → 不砍亏，继续持有（未达砍亏条件）。"""
        from caisen.backtest_replay import _simulate_one_trade
        from caisen.plan import TradePlan

        plan = TradePlan(
            plan_id="t2", symbol="Y.SZ", pattern_type="w_bottom",
            formed_at=pd.Timestamp("2024-01-01"),
            breakout_price=10.0, neckline_price=10.0, bottom_price=8.0, H=2.0,
            entry_upper=10.0, entry_lower=9.8,
            stop_loss=8.0, take_profit=12.0, take_profit_2x=14.0,
            rr_ratio=1.0, valid_until=pd.Timestamp("2024-01-05"),
            max_holding_until=pd.Timestamp("2024-01-20"),
            timeout_exit_threshold=0.01, shares=100, metadata={},
        )
        # 超时点收盘 10.2 → profit=(10.2-10)/10=0.02 ≥ 0.01 → 不砍亏，持有到末尾 still_open。
        closes = [10.5, 10.0] + [10.2] * 8
        highs = [11.0, 10.3] + [10.3] * 8
        lows = [10.0, 9.8] + [10.1] * 8
        df = pd.DataFrame(
            {"close": closes, "high": highs, "low": lows,
             "volume": [100] * 10, "amount": [1e8] * 10},
            index=pd.RangeIndex(10),
        )
        hit = _simulate_one_trade(df, plan, entry_day=0, max_holding_bars=3)
        assert hit is not None
        # 浮盈达标不砍亏 → 不应是 timeout（持有到末尾 still_open）
        assert hit["exit_reason"] != "timeout", "浮盈≥阈值不应砍亏"


# ---------------------------------------------------------------------------
# 4. min_rr_ratio 敏感性（Task 9 rr 张力承袭）
# ---------------------------------------------------------------------------
class TestReplayMinRRSensitivity:
    """宽松 min_rr_ratio(1.5) 比严格(3.0) 命中更多——证明 rr 阈值影响样本量。

    承 Task 9 review 张力：spec 默认 min_rr_ratio=3.0 与蔡森等额累加（标准突破入场
    breakout≈neckline 时 rr≈1.0）有冲突——rr≥3 会过滤掉所有标准突破入场计划。
    本用例数据驱动证明：宽松阈值收集更多样本，严格阈值样本匮乏。
    """

    def test_loose_min_rr_collects_more_hits(self):
        """同一历史序列，min_rr=1.5 命中数 ≥ min_rr=3.0 命中数。

        物理事实（Task 9 review）：标准 W底 breakout≈neckline 时 rr=(tp-entry)/(entry-stop)
        ≈ H/(neck-bottom+H) ≈ 1.0。故 min_rr=3.0 会丢弃几乎所有标准突破计划，
        min_rr=1.5 能保留部分浅形态计划。本用例验证该样本量差异。
        """
        close, high, low, vol = _build_w_bottom_with_rise(n_tail=18, target_mult=1.5)
        df = _mk_price_df(close, high, low, vol)
        start = df.index[15]
        end = df.index[-1]

        # 宽松 rr=1.5
        cfg_loose = _mk_cfg(min_rr_ratio=1.5)
        rm_loose = RiskManager(cfg_loose)
        report_loose = replay({"TEST": df}, cfg_loose, rm_loose,
                              start=start, end=end, aum=1e6)

        # 严格 rr=3.0（生产默认，预期样本显著更少）
        cfg_strict = _mk_cfg(min_rr_ratio=3.0)
        rm_strict = RiskManager(cfg_strict)
        report_strict = replay({"TEST": df}, cfg_strict, rm_strict,
                               start=start, end=end, aum=1e6)

        # 关键断言：宽松阈值命中数 ≥ 严格阈值（通常严格阈值下命中=0 或极少）
        assert report_loose.n_hits >= report_strict.n_hits, (
            f"宽松 rr=1.5 命中数({report_loose.n_hits}) 应 ≥ 严格 rr=3.0 命中数"
            f"({report_strict.n_hits})——证明 rr 阈值过滤标准突破入场计划"
        )

    def test_min_rr_recommendation_present(self):
        """ReplayReport 应包含数据驱动的 min_rr_ratio 建议（非空字符串）。"""
        close, high, low, vol = _build_w_bottom_with_rise(n_tail=18, target_mult=1.5)
        df = _mk_price_df(close, high, low, vol)
        cfg = _mk_cfg()
        rm = RiskManager(cfg)
        report = replay({"TEST": df}, cfg, rm,
                        start=df.index[15], end=df.index[-1], aum=1e6)

        assert isinstance(report.min_rr_ratio_recommendation, str)
        assert len(report.min_rr_ratio_recommendation) > 0, (
            "min_rr_ratio 建议应非空（数据驱动：基于胜率/平均盈亏比给出）"
        )


# ---------------------------------------------------------------------------
# 去重：同一形态连续 T 日只计一次（防重复计数 bug）
# ---------------------------------------------------------------------------
def test_replay_dedups_same_pattern_across_consecutive_T(monkeypatch):
    """【去重】同一形态在连续 T 日被 screener 反复识别时，replay 只计一次。

    背景：replay 对每个 T 日独立 screener.screen(.loc[:T])。某形态形成后尾部 4 pivot
    在后续 T 日不变 → screener 反复识别同一形态 → 每 T 都 plan + _simulate → 重复计数
    （实盘 T 日入场后 T+1 已持仓不会重入）。去重：per-symbol 跟踪形态签名
    (neckline_price, bottom_price)，同形态只模拟首次。
    """
    import pandas as pd
    from caisen import backtest_replay as br
    from caisen import plan as plan_mod
    from caisen.plan import TradePlan

    cfg = StrategyConfig(min_rr_ratio=0.0, abc_wave_detect=False, ma26w_filter=False)
    risk = RiskManager(cfg)

    # 单标的 30 根横盘（让 _iter_trading_days 产多 T，T>=min_pattern_bars=11 起处理）
    n = 30
    df = pd.DataFrame({
        "open": [10.0] * n, "high": [10.5] * n, "low": [9.5] * n, "close": [10.0] * n,
        "volume": [1000.0] * n, "amount": [1e8] * n,
    }, index=pd.RangeIndex(n))
    price_data = {"TEST": df}

    # FAKE_PLAN：所有 T 返同一 plan（同 neckline/bottom = 同形态签名）
    fake_plan = TradePlan(
        plan_id="x", symbol="TEST", pattern_type="w_bottom",
        formed_at=pd.Timestamp("2024-01-01"),
        breakout_price=10.0, neckline_price=11.0, bottom_price=8.0, H=3.0,
        entry_upper=10.0, entry_lower=9.8, stop_loss=8.0,
        take_profit=14.0, take_profit_2x=17.0, rr_ratio=2.0,
        valid_until=pd.Timestamp("2024-01-10"), max_holding_until=pd.Timestamp("2024-02-01"),
        timeout_exit_threshold=0.5, shares=100, metadata={},
    )
    fake_cands = pd.DataFrame([{"symbol": "TEST"}])   # 非空，让 replay 不 skip

    # mock screener（每 T 返同 candidates）+ plan.generate（每 T 返同 plan）
    # replay 优化后调 screen_with_pivots（复用预算 pivots），mock 需同步提供该方法。
    class _FakeScreener:
        def __init__(self, cfg, risk): pass
        def screen(self, pd_data, date): return fake_cands
        def screen_with_pivots(self, pd_data, pivots_map, hv_map, date): return fake_cands
    monkeypatch.setattr(br, "PatternScreener", _FakeScreener)
    monkeypatch.setattr(plan_mod, "generate", lambda *a, **k: [fake_plan])

    report = replay(price_data, cfg, risk, start=0, end=29, aum=1e6)
    hits = report.metadata["hits"]
    # 去重：同形态连续 T 日只计 1 次（旧实现每 T 一个 hit = 多个）
    assert len(hits) == 1, f"同形态连续T日应去重只计1次，实际 {len(hits)}"


# ---------------------------------------------------------------------------
# 5. 性能基准 + pivot 复用等价（回测跑通批次）
# ---------------------------------------------------------------------------
class TestReplayPerfAndReuse:
    """性能回归基准 + pivot 复用路径与逐T .loc[:T] 路径的等价守护。

    背景：旧 replay 每 T 重算整个历史的 compute_atr+causal_pivots → O(标的×T²)，
    实测 10只×2年 114s（全市场 16h，前端 90s 超时 = 用户看到"跑不出"）。
    pivot 复用优化（全df一次算 atr+pivots，每T复用截断+confirm_bars 过滤）后应
    O(标的×T) <15s。本类守护：①性能不回退；②复用路径与逐T金标准严格等价（无前视）。
    """

    def test_replay_perf_10sym_500bars_under_30s(self):
        """性能基准：10 标的 × 500 K线 replay < 30s。

        旧 O(T²) 实现此规模 ~100s+（红，确认基线）；pivot 复用后应 <15s（绿）。
        合成随机游走序列（无稳定形态，但 screener 每 T 跑全链路 = 真实负载，测的就是
        per-T screener 调用次数的性能，与是否命中形态无关）。
        Why 30s 而非 15s：留 CI/不同机器余量防 flaky；显著低于旧 ~100s 即证明优化生效。
        """
        import time

        cfg = _mk_cfg()
        rm = RiskManager(cfg)
        rng = np.random.default_rng(42)
        price_data = {}
        for i in range(10):
            n = 500
            rets = rng.normal(0, 0.02, n)
            close = 10.0 * np.exp(np.cumsum(rets))
            df = pd.DataFrame({
                "close": close,
                "high": close * 1.01,
                "low": close * 0.99,
                "volume": [1000.0] * n,
                "amount": [2e8] * n,
            }, index=pd.RangeIndex(n))
            price_data[f"S{i}"] = df

        t0 = time.perf_counter()
        replay(price_data, cfg, rm, start=20, end=499, aum=1e6)
        elapsed = time.perf_counter() - t0
        assert elapsed < 30.0, (
            f"replay 10标的×500K线 耗时 {elapsed:.1f}s > 30s——pivot 复用优化未生效？"
            f"（旧 O(T²) 实现此规模 ~100s+）"
        )

    def test_replay_pivot_reuse_equiv_manual_loc(self):
        """pivot 复用路径入场集合 == 手动逐T screener.screen(.loc[:T]) 金标准。

        金标准：测试内手动对每个 T 调 screener.screen({sym:df.loc[:T]}, T) +
        plan.generate + _simulate_one_trade（逐T重算 atr/pivots，无复用）。replay（内部
        走 pivot 复用）应产出完全相同的 (entry_day, entry_price) 入场集合。

        Why：pivot 复用的正确性 = 与逐T .loc[:T] 严格等价。此测试直接对比，比
        test_replay_no_lookahead（裁剪等价）更精确地守护复用实现——一旦复用路径的
        confirm_bars 过滤或截断出错，入场集合会与金标准分歧。
        """
        from caisen import plan as plan_mod
        from caisen.backtest_replay import _simulate_one_trade

        cfg = _mk_cfg()
        rm = RiskManager(cfg)
        close, high, low, vol = _build_w_bottom_with_rise(n_tail=18, target_mult=1.5)
        df = _mk_price_df(close, high, low, vol)
        price_data = {"TEST": df}
        start = df.index[15]
        end = df.index[-1]

        # —— replay（内部 pivot 复用路径）——
        report = replay(price_data, cfg, rm, start=start, end=end, aum=1e6)
        replay_entries = sorted(
            (h["entry_day"], round(h["entry_price"], 4)) for h in report.metadata["hits"]
        )

        # —— 金标准：手动逐 T .loc[:T]（无复用，每T重算 atr+pivots）——
        screener = PatternScreener(cfg, rm)
        manual_entries = []
        last_sig = None
        for T in range(int(start), int(end) + 1):
            df_T = df.loc[:T]
            if len(df_T) < cfg.min_pattern_bars:
                continue
            cands = screener.screen({"TEST": df_T}, T)
            if cands.empty:
                continue
            plans = plan_mod.generate(cands, cfg, rm, 1e6, T)
            for p in plans:
                sig = (round(p.neckline_price, 6), round(p.bottom_price, 6))
                if sig == last_sig:
                    continue
                last_sig = sig
                hit = _simulate_one_trade(df, p, T, cfg.max_holding_bars)
                if hit is not None:
                    manual_entries.append((hit["entry_day"], round(hit["entry_price"], 4)))
        manual_entries = sorted(manual_entries)

        assert replay_entries == manual_entries, (
            f"pivot 复用路径与逐T .loc[:T] 金标准入场集合不一致：\n"
            f"复用={replay_entries}\n金标准={manual_entries}"
        )

    def test_replay_equity_curve_trades_present(self):
        """输出增强：有命中时 equity_curve/trades 非空且字段完整 + 年化收益合理。

        验回测跑通批次新增的 equity_curve（资金曲线）/trades（买卖流水）/annualized_return
        （年化 CAGR）/n_trading_days（区间交易日数）四字段。
        """
        cfg = _mk_cfg()
        rm = RiskManager(cfg)
        close, high, low, vol = _build_w_bottom_with_rise(n_tail=18, target_mult=1.5)
        df = _mk_price_df(close, high, low, vol)
        report = replay({"TEST": df}, cfg, rm, start=df.index[15], end=df.index[-1], aum=1e6)

        hits = report.metadata.get("hits", [])
        if not hits:
            pytest.skip("合成序列无命中，跳过 equity_curve/trades 验证")

        # equity_curve / trades 长度 == 命中数（每笔一个点/一条流水）
        assert len(report.equity_curve) == len(hits)
        assert len(report.trades) == len(hits)
        # equity_curve 每点字段完整（date/cumulative_rr/equity）
        for pt in report.equity_curve:
            assert {"date", "cumulative_rr", "equity"} <= set(pt.keys())
        # trades 每笔字段完整（前端流水表所需）
        for t in report.trades:
            assert {"symbol", "entry_date", "entry_price", "exit_date",
                    "exit_price", "exit_reason", "rr"} <= set(t.keys())
        # n_trading_days > 0（回放区间非空）
        assert report.n_trading_days > 0
        # annualized_return 是有限数（CAGR）
        assert isinstance(report.annualized_return, (int, float))
