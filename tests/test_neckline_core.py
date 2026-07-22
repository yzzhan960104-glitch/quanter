# -*- coding: utf-8 -*-
"""颈线法核心算法回归测试（P0 · docs/neckline-method.md §6）。

物理意图（Why 本文件存在）：
    simulate_exit 是 ~130 行的完整状态机（挂单回踩 + max_wait + cancel_on 撤单 +
    分级止盈 tp1/tp2 + 颈线−ATR 止损 + trailing 时间驱动移动止损 + 超时），
    kelly_metrics 是 param_iter 的目标函数（2026-07-20 刚连修两个复利爆炸 bug：
    pos_cap 封单笔仓位 + freq_cap 封年信号数）。两者承载调参与实盘的
    "single source of truth"（strategies/neckline_method.py 适配器复用 simulate_exit），
    却长期零单测——本文件用合成 OHLCV 构造确定性场景，把每个出场分支、v6 成交价修复、
    trailing 收紧、kelly 双封顶防爆钉成断言。

    后续任何 simulate_exit/kelly_metrics 改动（trailing 正式落地、宽度顺势加权、
    凯利仓位自适应）必须保持这些断言绿，否则即行为回归。

覆盖：
    - simulate_exit 六分支：stop_loss / tp1→tp2 / timeout / skip_no_pullback /
      skip_target_met / 跳空低开成交价（v6 修复点）
    - trailing 移动止损收紧（构造 base_stop 不触发、trailing 才触发的对照场景）
    - kelly_metrics 双封顶防爆（高频高 f* 序列 ann 不爆炸）
    - dedup_signals cooldown 去重

未覆盖（留 follow-up）：
    - scan_symbol 端到端（需合成 detect_neckline_method 可识别形态）
    - detect_neckline_method 识别层（同上）
"""
import sys
from pathlib import Path

import pandas as pd
import pytest

# 颈线法算法已收口进 strategies/neckline/ 子包（Layer2 Task 1.5），
# 不再加 scripts/ 到 sys.path——改走包 import（项目根在 pytest rootdir 即 sys.path）。
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from strategies.neckline.backtest import (  # noqa: E402
    simulate_exit,
    kelly_metrics,
    dedup_signals,
    EXEC_DEFAULTS,
)
from strategies.neckline.method_v0 import DEFAULTS  # noqa: E402  （确保 import 链可达）


# ============================================================================
# 合成 OHLCV 辅助
# ============================================================================
def _ohlc(rows, start="2024-01-01"):
    """rows: [(open, high, low, close, volume), ...] → DatetimeIndex DataFrame。

    物理意图：构造确定性 OHLCV，让 simulate_exit 每个分支精确触发。DatetimeIndex
    模拟真实 sym_df（simulate_exit 用 sym_df.index[idx].date() 取信号日/离场日）。
    freq="B"（工作日）避免周末。调用方需自行保证 OHLC 物理一致性
    （high ≥ max(open,close)，low ≤ min(open,close)）。
    """
    dates = pd.date_range(start, periods=len(rows), freq="B")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"], index=dates)


# 基准场景：颈线 c*=100 / 谷底 bottom=90 / 形态高度 H=10 / ATR=2。
# 代入默认 EXEC_DEFAULTS + DEFAULTS 推出关键价位（数值好算、分支边界清晰）：
#   buy_limit   = c* + buy_limit_atr_mult·ATR = 100 + 1.0·2 = 102  （挂单价）
#   base_stop   = c* − stop_atr_mult·ATR      = 100 − 1.0·2 = 98   （固定止损基准）
#   tp1         = c* + tp1_h_mult·H           = 100 + 1.0·10 = 110 （第一止盈）
#   tp2         = c* + tp_h_mult·H            = 100 + 2.0·10 = 120 （第二止盈）
#   cancel_on   = c* + cancel_thresh_mult·H   = 100 + 1.0·10 = 110 （撤单阈值）
#   max_wait=5, max_holding=15, tp1_portion=0.5
C_STAR, BOTTOM, ATR = 100.0, 90.0, 2.0

# 信号日统一取 bar0（signal_idx=0）；simulate_exit 从 signal_idx+1=bar1 起等回踩。


# ============================================================================
# simulate_exit · 出场六分支
# ============================================================================
def test_stop_loss():
    """成交后持有期 low≤base_stop → stop_loss，lot1=lot2=(stop−entry)/entry。"""
    df = _ohlc([
        (100, 101, 99, 100.5, 1000),   # bar0 信号日（位置锚，simulate_exit 不读其 close）
        (102, 103, 102, 102.5, 1000),  # bar1 low=102≤buy_limit=102 成交，entry=102
        (99, 100, 97, 98, 1000),       # bar2 low=97≤base_stop=98 → stop_loss
    ])
    res = simulate_exit(df, 0, C_STAR, BOTTOM, ATR)
    assert res["exit_reason"] == "stop_loss"
    assert res["entry"] == 102.0
    # 两手同价止损：avg = (98−102)/102 = −3.92%
    assert res["avg_pnl_pct"] == round((98 - 102) / 102 * 100, 2)


def test_tp1_then_tp2():
    """先触 tp1（卖 lot1，lot2 续持）后续触 tp2（卖 lot2）→ exit_reason=tp2，tp1_portion 加权。"""
    df = _ohlc([
        (100, 101, 99, 100.5, 1000),   # bar0 信号
        (102, 103, 102, 102.5, 1000),  # bar1 成交 entry=102
        (109, 111, 108, 110.5, 1000),  # bar2 high=111≥tp1=110（<tp2=120）→ 卖 lot1
        (119, 121, 118, 120.5, 1000),  # bar3 high=121≥tp2=120 → 卖 lot2（lot1 已卖）
    ])
    res = simulate_exit(df, 0, C_STAR, BOTTOM, ATR)
    assert res["exit_reason"] == "tp2"
    lot1_pct = (110 - 102) / 102 * 100   # +7.84%
    lot2_pct = (120 - 102) / 102 * 100   # +17.65%
    expected_avg = 0.5 * lot1_pct + 0.5 * lot2_pct
    assert res["lot1_pnl_pct"] == round(lot1_pct, 2)
    assert res["lot2_pnl_pct"] == round(lot2_pct, 2)
    assert res["avg_pnl_pct"] == round(expected_avg, 2)


def test_timeout():
    """max_holding 内无 stop/tp1/tp2 触发 → timeout，两手按末根收盘价平。"""
    rows = [
        (100, 101, 99, 100.5, 1000),   # bar0 信号
        (102, 103, 102, 102.5, 1000),  # bar1 成交 entry=102
    ]
    # bar2..bar16（持有期至 end_idx = buy_idx+max_holding = 1+15 = 16）flat 105：
    # high=105<tp1(110)、low=105>stop(98) → 全程无触发，末根 is_last → timeout
    rows += [(105, 105, 105, 105, 1000) for _ in range(15)]
    df = _ohlc(rows)
    res = simulate_exit(df, 0, C_STAR, BOTTOM, ATR)
    assert res["exit_reason"] == "timeout"
    # 两手按收盘 105：avg = (105−102)/102 = +2.94%
    assert res["avg_pnl_pct"] == round((105 - 102) / 102 * 100, 2)


def test_skip_no_pullback():
    """max_wait 内 low>buy_limit（不回踩）且 high<cancel_on（不撤单）→ skip_no_pullback。"""
    rows = [(100, 101, 99, 100.5, 1000)]   # bar0 信号
    # bar1..bar5（max_wait=5）：low=103>buy_limit=102（不回踩）、high=104<cancel_on=110（不撤单）
    rows += [(103, 104, 103, 103.5, 1000) for _ in range(5)]
    df = _ohlc(rows)
    res = simulate_exit(df, 0, C_STAR, BOTTOM, ATR)
    assert res["exit_reason"] == "skip_no_pullback"
    assert res["entry"] is None     # 未成交


def test_skip_target_met():
    """等待期 high≥cancel_on → 涨幅已兑现、回踩是退潮 → 撤单 skip_target_met。"""
    df = _ohlc([
        (100, 101, 99, 100.5, 1000),   # bar0 信号
        (109, 111, 108, 110.5, 1000),  # bar1 high=111≥cancel_on=110 → 撤单（优先于回踩判定）
    ])
    res = simulate_exit(df, 0, C_STAR, BOTTOM, ATR)
    assert res["exit_reason"] == "skip_target_met"
    assert res["entry"] is None


def test_gap_down_entry_uses_open():
    """限价买单跳空低开（open<buy_limit）→ 成交 open 而非 buy_limit（v6 修复点）。

    旧版 entry=buy_limit 高估跳空低开的买入价（早期悲观结论元凶之一）。
    正确：entry = min(buy_limit, open) = open（市价<挂单价，更优）。
    """
    df = _ohlc([
        (100, 101, 99, 100.5, 1000),   # bar0 信号
        (100, 101, 99, 100.5, 1000),   # bar1 open=100<buy_limit=102，low=99≤102 成交 → entry=100
        (99, 100, 97, 98, 1000),       # bar2 low=97≤base_stop=98 → stop_loss（驱动出场，聚焦 entry）
    ])
    res = simulate_exit(df, 0, C_STAR, BOTTOM, ATR)
    assert res["entry"] == 100.0       # v6 修复：跳空低开成交 open，非 buy_limit=102
    assert res["exit_reason"] == "stop_loss"


# ============================================================================
# trailing 移动止损（时间驱动 · 海龟风格）
# ============================================================================
def test_trailing_tightens_stop():
    """trailing 开启后 stop 随持有天数收紧；构造 base_stop 不触发、trailing 才触发的场景。

    exec: grace=2 / step=0.1 / floor=0.5（stop_atr_mult=1.0, atr=2 → base_stop=98）。
    持有天数 holding_days = i − buy_idx（buy_idx=1），分支：
        hd ≤ grace(2)          → stop = base_stop = 98（宽限期，给趋势确认空间）
        hd = 3                 → eff_mult = 1.0 − (3−2)·0.1 = 0.9 → stop = 100 − 0.9·2 = 98.2
        hd = 4                 → eff_mult = 0.8                     → stop = 98.4
        hd = 5                 → eff_mult = 0.7                     → stop = 98.6  ← bar6 触发
    bar6 low=98.3：> base_stop=98（固定止损不触发）但 ≤ trailing stop=98.6 → 证明 trailing 生效。
    """
    exec_cfg = {**EXEC_DEFAULTS, "trailing_grace": 2, "trailing_step": 0.1, "trailing_floor": 0.5}
    df = _ohlc([
        (100, 101, 99, 100.5, 1000),   # bar0 信号
        (102, 103, 102, 102.5, 1000),  # bar1 成交 entry=102（hd=0, grace, stop=98）
        (99, 100, 99, 99.5, 1000),     # bar2 hd=1 grace, low=99>98 不触
        (99, 100, 99, 99.5, 1000),     # bar3 hd=2 grace, low=99>98 不触
        (99, 99.5, 98.5, 99, 1000),    # bar4 hd=3 stop=98.2, low=98.5>98.2 不触
        (99, 99.5, 98.5, 99, 1000),    # bar5 hd=4 stop=98.4, low=98.5>98.4 不触
        (98.5, 99, 98.3, 98.5, 1000),  # bar6 hd=5 stop=98.6, low=98.3≤98.6 → 触发
    ])
    res = simulate_exit(df, 0, C_STAR, BOTTOM, ATR, exec=exec_cfg)
    assert res["exit_reason"] == "stop_loss"
    # stop=98.6, entry=102 → (98.6−102)/102 = −3.33%
    assert res["avg_pnl_pct"] == round((98.6 - 102) / 102 * 100, 2)

    # 对照：同样 sym_df 但 trailing 关闭（默认 grace=0/step=0 → 固定 base_stop=98），
    # bar6 low=98.3>98 不触发 stop_loss@bar6 —— 证明 trailing 是 bar6 触发的唯一原因。
    res_no_trail = simulate_exit(df, 0, C_STAR, BOTTOM, ATR)
    same_bar_stop = (
        res_no_trail["exit_reason"] == "stop_loss"
        and res_no_trail["exit_date"] == res["exit_date"]
    )
    assert not same_bar_stop, "trailing 关闭时 bar6 不应 stop_loss（证明 trailing 是触发原因）"


# ============================================================================
# kelly_metrics · 双封顶防爆（pos_cap + freq_cap）
# ============================================================================
def test_kelly_no_explosion():
    """高频 + 高 f* 序列：pos_cap=0.05 + freq_cap=150 双封顶 → ann 不爆炸。

    构造 [1, 1, −0.5] 循环 1260 笔（胜率 2/3、盈亏比 b=2 → kelly f*=0.5）跨 1.5 年。
    旧版 curve=Π(1+f*·r/100) 满仓复利假设所有信号可同时下注 → 爆炸至 7257%~16495%；
    新版 pos=min(f*, pos_cap) 封单笔仓位 + 按年 head(freq_cap) 封年信号数 → ann 落实盘区间。
    断言 ann < 100%（远低于旧版 72×），且正收益（高胜率高盈亏比应增长）。
    """
    pnls = [1.0, 1.0, -0.5] * 420        # 1260 笔
    dates = pd.date_range("2024-01-01", "2025-07-01", periods=len(pnls))
    kelly, curve, ann = kelly_metrics(pnls, dates)
    assert 0 < kelly <= 0.5              # 凯利约束 [0, 0.5]
    assert 0 < ann < 1.0                 # 防爆：远低于旧版 7257%（< 100%）
    assert curve > 1.0                   # 正期望序列，资金曲线应增长


def test_risk_metrics_sharpe_and_drawdown():
    """risk_metrics：信号夏普 + 资金曲线最大回撤（与 kelly_metrics 同源 sampling）。

    构造 [+2, +2, −1] 循环（胜率 2/3、盈亏比 2 → kelly=0.5、pos=0.05）。
    断言：正夏普（正期望）、回撤 ∈ (0,1)（有回撤未爆仓）、ann 与 kelly_metrics 完全一致
    （证明 risk_metrics 复用了 kelly_metrics 的 sampling，前三维同源）。
    """
    from strategies.neckline.backtest import risk_metrics, kelly_metrics
    pnls = [2.0, 2.0, -1.0] * 60     # 180 笔
    dates = pd.date_range("2024-01-01", "2025-01-01", periods=len(pnls))
    kelly, curve, ann, sharpe, max_dd = risk_metrics(pnls, dates)
    assert kelly == pytest.approx(0.5, abs=0.01)
    assert sharpe > 0                  # 胜率 2/3 + 盈亏比 2 → 正期望 → 正夏普
    assert 0 < max_dd < 1             # 有回撤但未爆仓
    # 与 kelly_metrics 同源：ann 必须一致（证明复用 sampling）
    _, _, ann_kelly = kelly_metrics(pnls, dates)
    assert ann == pytest.approx(ann_kelly, abs=1e-9)


# ============================================================================
# dedup_signals · cooldown 去重
# ============================================================================
def test_dedup_signals_cooldown():
    """相邻信号 idx 差 < cooldown 合并、≥ cooldown 保留（同形态多日触发只交易首次）。"""
    signals = [
        (10, {"neckline": 100}),
        (12, {"neckline": 100}),   # 12−10=2 <5 → 合并
        (20, {"neckline": 100}),   # 20−10=10 ≥5 → 保留
    ]
    deduped = dedup_signals(signals, cooldown=5)
    assert [idx for idx, _ in deduped] == [10, 20]


def test_dedup_signals_empty():
    """空信号列表 → 空结果（防 None/异常）。"""
    assert dedup_signals([], cooldown=5) == []
