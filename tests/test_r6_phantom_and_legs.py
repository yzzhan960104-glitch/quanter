# -*- coding: utf-8 -*-
"""R6-4 幽灵成交修复 + R6-5 腿 A/B 原型参数守护（2026-08-26 用户裁决「修复/都测试一下」）。

R6-4 修复语义：tp2 全平时 lot1「同日一并卖」的成交价必须是当根真实到达过的价位
——high≥tp1 → tp1 记账（tp1≤tp2 全部常规配置逐位不变，golden 钉死）；tp1>tp2 且
high<tp1 → 按 tp2 同价平仓（原版按从未到达的 tp1 记账=幽灵成交，R6-1 实锤
tp1_h_mult=5 档 6590/18445 笔污染）。tp1=None 同落 tp2（全量 tp2 语义）+ decide_exit
priority 3 None 守卫（原潜伏 TypeError 消除）。

R6-5 腿 A（chase_entry，默认 False）/ 腿 B（timeout_extend，默认 days=0）：
默认关=零行为变化（scan 级逐位等价守护），开=行为按文档语义。
"""
import sys
from pathlib import Path

import pandas as pd
import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from strategies.neckline import backtest as bk  # noqa: E402
from strategies.neckline.method_v0 import DEFAULTS  # noqa: E402

_NEW_EXEC_KEYS = ("chase_entry", "timeout_extend_days", "timeout_extend_min_pnl")


@pytest.fixture(autouse=True)
def _fresh_cache():
    bk._clear_scan_id_cache()
    yield
    bk._clear_scan_id_cache()


def _df(rows):
    """rows: [(open, high, low, close, volume)] → DatetimeIndex OHLCV df。"""
    idx = pd.date_range("2024-01-01", periods=len(rows), freq="B")
    return pd.DataFrame(rows, index=idx,
                        columns=["open", "high", "low", "close", "volume"])


def _exec(**over):
    base = {**bk.EXEC_DEFAULTS,
            "commission_rate": 0.0, "stamp_rate": 0.0, "transfer_rate": 0.0,
            "cancel_thresh_mult": None}
    base.update(over)
    return base


def _id_cfg(**over):
    return {**DEFAULTS, **over}


# 几何锚：neckline=100, bottom=90（H=10）, atr=2；stop=98（stop_atr_mult=1.0）
NLK, BOT, ATR = 100.0, 90.0, 2.0
BUY_LIMIT = NLK + 1.0 * ATR   # buy_limit_atr_mult=1.0 → 102


# ============================================================================
# R6-4 幽灵成交修复三态
# ============================================================================
def test_tp2_lot1_unreachable_tp1_booked_at_tp2():
    """tp1>tp2 且当根 high<tp1：lot1 按 tp2 同价平（修复前按从未到达的 tp1=幽灵）。"""
    df = _df([
        (100, 101, 99, 100, 1000),    # 0 信号日
        (103, 104, 101, 103, 1000),   # 1 回踩成交：low 101≤102 → entry=102
        (110, 116, 108, 112, 1000),   # 2 high 116≥tp2=115 触发；116<tp1=150 → lot1@tp2
    ] + [(112, 113, 110, 112, 1000)] * 5)
    sim = bk.simulate_exit(df, 0, NLK, BOT, ATR,
                           exec=_exec(tp1_h_mult=5.0),
                           id_cfg=_id_cfg(tp_h_mult=1.5))   # tp1=150 tp2=115
    assert sim["exit_reason"] == "tp2"
    assert sim["lot1_pnl_pct"] == sim["lot2_pnl_pct"] == round(
        (115.0 - 102.0) / 102.0 * 100, 2)   # 12.75（修复前 lot1=(150-102)/102=47.06 幽灵）


def test_tp2_lot1_reached_tp1_booked_at_tp1():
    """tp1>tp2 但当根 high≥tp1（巨型 K 线真到达）：lot1 按 tp1 记账（合法）。"""
    df = _df([
        (100, 101, 99, 100, 1000),
        (103, 104, 101, 103, 1000),   # entry=102
        (140, 151, 138, 145, 1000),   # high 151≥tp1=150 → lot1@150，lot2@115
    ] + [(145, 146, 143, 145, 1000)] * 5)
    sim = bk.simulate_exit(df, 0, NLK, BOT, ATR,
                           exec=_exec(tp1_h_mult=5.0),
                           id_cfg=_id_cfg(tp_h_mult=1.5))
    assert sim["lot1_pnl_pct"] == round((150.0 - 102.0) / 102.0 * 100, 2)
    assert sim["lot2_pnl_pct"] == round((115.0 - 102.0) / 102.0 * 100, 2)


def test_tp2_lot1_normal_config_bit_unchanged():
    """tp1≤tp2 常规配置：high≥tp2≥tp1 恒真 → lot1 按 tp1 记账（迁移前语义逐位保留）。"""
    df = _df([
        (100, 101, 99, 100, 1000),
        (103, 104, 101, 103, 1000),   # entry=102
        (118, 121, 116, 120, 1000),   # high 121≥tp2=120 触发；≥tp1=110 → lot1@110
    ] + [(120, 121, 118, 120, 1000)] * 5)
    sim = bk.simulate_exit(df, 0, NLK, BOT, ATR,
                           exec=_exec(tp1_h_mult=1.0),
                           id_cfg=_id_cfg(tp_h_mult=2.0))   # tp1=110 tp2=120
    assert sim["lot1_pnl_pct"] == round((110.0 - 102.0) / 102.0 * 100, 2)   # 7.84
    assert sim["lot2_pnl_pct"] == round((120.0 - 102.0) / 102.0 * 100, 2)   # 17.65


def test_tp1_none_full_tp2_semantics_no_crash():
    """tp1=None（未配置一档）：tp2 全平两 lot 同价（全量 tp2 语义）；timeout 路径不炸。"""
    df = _df([
        (100, 101, 99, 100, 1000),
        (103, 104, 101, 103, 1000),   # entry=102
        (110, 116, 108, 112, 1000),   # tp2=115 触发
    ] + [(112, 113, 110, 112, 1000)] * 5)
    sim = bk.simulate_exit(df, 0, NLK, BOT, ATR,
                           exec=_exec(tp1_h_mult=None),
                           id_cfg=_id_cfg(tp_h_mult=1.5))
    assert sim["exit_reason"] == "tp2"
    assert sim["lot1_pnl_pct"] == sim["lot2_pnl_pct"] == round(
        (115.0 - 102.0) / 102.0 * 100, 2)
    # timeout 路径（high 恒<tp2、lot1 仍开）：decide_exit priority 3 None 守卫不炸
    df2 = _df([
        (100, 101, 99, 100, 1000),
        (103, 104, 101, 103, 1000),
    ] + [(103, 104, 100, 103, 1000)] * 6)
    sim2 = bk.simulate_exit(df2, 0, NLK, BOT, ATR,
                            exec=_exec(tp1_h_mult=None, max_holding=3),
                            id_cfg=_id_cfg(tp_h_mult=1.5))
    assert sim2["exit_reason"] == "timeout"
    assert sim2["lot1_pnl_pct"] == sim2["lot2_pnl_pct"]


# ============================================================================
# R6-5 腿 A：chase_entry
# ============================================================================
def test_chase_entry_engages_at_next_open():
    """等待期无回踩：chase_entry=True → wait_end 次日开盘市价追入（entry=开盘价本身）。"""
    rows = [(100, 101, 99, 100, 1000)]          # 0 信号日
    rows += [(105, 107, 104, 106, 1000)] * 5    # 1-5 等待期：low 恒 104>102 不回踩
    rows += [(105, 106, 103, 105, 1000)]        # 6 次日 open=105 < tp2=120 → 追入
    rows += [(108, 121, 107, 118, 1000)]        # 7 high≥tp2=120 → tp2
    rows += [(118, 119, 116, 118, 1000)] * 5
    sim = bk.simulate_exit(_df(rows), 0, NLK, BOT, ATR,
                           exec=_exec(chase_entry=True),
                           id_cfg=_id_cfg(tp_h_mult=2.0))
    assert sim["exit_reason"] == "tp2"
    assert sim["entry"] == 105.0                 # 开盘价本身（非 min(buy_limit,open)=102 幽灵）
    assert sim["buy_date"] == pd.Timestamp("2024-01-09").date()   # 第 7 根（0 起算 idx6）
    assert sim["lot1_pnl_pct"] == round((110.0 - 105.0) / 105.0 * 100, 2)
    assert sim["lot2_pnl_pct"] == round((120.0 - 105.0) / 105.0 * 100, 2)


def test_chase_entry_skipped_when_target_exhausted():
    """追入价≥tp2（形态目标透支）：仍按原语义弃单（skip_no_pullback）。"""
    rows = [(100, 101, 99, 100, 1000)]
    rows += [(105, 107, 104, 106, 1000)] * 5    # 无回踩
    rows += [(121, 122, 118, 121, 1000)]        # 次日 open=121 ≥ tp2=120 → 不追
    rows += [(121, 122, 119, 121, 1000)] * 3
    sim = bk.simulate_exit(_df(rows), 0, NLK, BOT, ATR,
                           exec=_exec(chase_entry=True),
                           id_cfg=_id_cfg(tp_h_mult=2.0))
    assert sim["exit_reason"] == "skip_no_pullback"
    assert sim["entry"] is None


def test_chase_entry_off_keeps_skip_semantics():
    """chase_entry 默认关：无回踩仍弃单（零行为变化）。"""
    rows = [(100, 101, 99, 100, 1000)]
    rows += [(105, 107, 104, 106, 1000)] * 5
    rows += [(105, 106, 103, 105, 1000)] * 3
    sim = bk.simulate_exit(_df(rows), 0, NLK, BOT, ATR,
                           exec=_exec(), id_cfg=_id_cfg(tp_h_mult=2.0))
    assert sim["exit_reason"] == "skip_no_pullback"


# ============================================================================
# R6-5 腿 B：timeout_extend
# ============================================================================
def _legb_rows():
    rows = [(100, 101, 99, 100, 1000)]          # 0 信号日
    rows += [(103, 104, 101, 103, 1000)]        # 1 回踩成交 entry=102（buy_idx=1）
    rows += [(104, 108, 103, 105, 1000),        # 2-5 缓涨：high<tp1=110 low>stop=98
             (105, 109, 104, 107, 1000),
             (106, 109, 105, 108, 1000),
             (107, 109, 106, 109, 1000)]        # 5 = end_idx(1+4) is_last：浮盈 6.9%≥5%
    rows += [(108, 121, 107, 118, 1000)]        # 6 延长期内 high≥tp2=120
    rows += [(118, 119, 116, 118, 1000)] * 4
    return rows


def test_timeout_extend_rides_to_tp2():
    """超时日浮盈≥门槛 → 一次性延长，延长期内 tp2 兑现（原版截断在 timeout）。"""
    sim = bk.simulate_exit(_df(_legb_rows()), 0, NLK, BOT, ATR,
                           exec=_exec(max_holding=4, timeout_extend_days=3),
                           id_cfg=_id_cfg(tp_h_mult=2.0))
    assert sim["exit_reason"] == "tp2"
    assert sim["exit_date"] == pd.Timestamp("2024-01-09").date()   # idx6
    assert sim["holding_bars"] == 5   # exit_pos 6 - buy_idx 1


def test_timeout_extend_default_off_times_out():
    """timeout_extend_days 默认 0：同 K 线序列仍 timeout 截断（零行为变化）。"""
    sim = bk.simulate_exit(_df(_legb_rows()), 0, NLK, BOT, ATR,
                           exec=_exec(max_holding=4),
                           id_cfg=_id_cfg(tp_h_mult=2.0))
    assert sim["exit_reason"] == "timeout"
    assert sim["exit_date"] == pd.Timestamp("2024-01-08").date()   # idx5 = end_idx


def test_timeout_extend_below_threshold_no_extend():
    """超时日浮盈<门槛：不延长（仍 timeout）。"""
    rows = [(100, 101, 99, 100, 1000),
            (103, 104, 101, 103, 1000)]
    rows += [(103, 104, 102, 103.5, 1000)] * 4   # 缓涨至 is_last 浮盈 ~1.5%<5%
    rows += [(103, 104, 102, 103, 1000)] * 3
    sim = bk.simulate_exit(_df(rows), 0, NLK, BOT, ATR,
                           exec=_exec(max_holding=4, timeout_extend_days=3),
                           id_cfg=_id_cfg(tp_h_mult=2.0))
    assert sim["exit_reason"] == "timeout"


# ============================================================================
# 默认关 scan 级逐位等价（新键加入 EXEC_DEFAULTS 后旧 exec dict == 新默认）
# ============================================================================
def test_new_exec_keys_default_off_bit_exact_real_data():
    """腿 A/B + 幽灵修复后的 EXEC_DEFAULTS（含新键默认关）与旧 21 键 exec 输出逐位一致。

    幽灵修复本身对 tp1≤tp2 配置逐位不变（上面三态测试 + 全量 golden 守护），
    此处补 scan 级：真实数据 + 默认档（tp1=1.0<tp2=2.0）下新默认键零行为变化。
    """
    lake_path = _ROOT / "data_lake" / "a_shares_daily.parquet"
    if not lake_path.exists():
        pytest.skip("data_lake 缺失，跳过真实数据等价守护（CI 无数据环境）")
    lake = pd.read_parquet(lake_path, filters=[("date", ">=", pd.Timestamp("2024-01-01"))])
    checked = 0
    for sym in ("688041.SH", "300308.SZ"):
        try:
            sym_df = lake.xs(sym, level="symbol").sort_index()
        except Exception:
            continue
        legacy = {k: v for k, v in bk.EXEC_DEFAULTS.items() if k not in _NEW_EXEC_KEYS}
        a = bk.scan_symbol(sym_df, DEFAULTS["window"], exec=legacy)
        b = bk.scan_symbol(sym_df, DEFAULTS["window"], exec=dict(bk.EXEC_DEFAULTS))
        assert a == b, f"{sym} 新默认键（默认关）引入 scan 级行为漂移"
        checked += 1
    assert checked >= 1, "候选标的全取失败（数据环境异常）"


# ============================================================================
# R6-10 B3 tp 锚自适应（C 线④）：默认关零变化 + 深形态缩近
# ============================================================================
def test_tp_adapt_default_off_bit_exact():
    """tp_adapt_h_atr=None（默认）：价位与不传参逐位一致（golden 语义延伸）。"""
    from strategies.neckline.price_levels import compute_price_levels, PRICE_LEVEL_DEFAULTS as D
    kw = dict(c_star=100.0, high=8.0, atr=2.0, stop_atr_mult=1.0,
              buy_limit_atr_mult=1.0, tp1_h_mult=1.0, tp_h_mult=2.0,
              cancel_thresh_mult=1.0)
    a = compute_price_levels(**kw)
    b = compute_price_levels(**kw, tp_adapt_h_atr=None, tp_adapt_scale=0.5)
    assert (a.tp1, a.tp2, a.stop, a.buy_limit, a.cancel_on) == \
           (b.tp1, b.tp2, b.stop, b.buy_limit, b.cancel_on)
    assert a.tp2 == 116.0 and a.tp1 == 108.0   # 无自适应：100+2×8 / 100+1×8


def test_tp_adapt_deep_form_shrinks_tp_anchors():
    """H/ATR 超阈值：tp2/tp1 乘数 ×scale（100/2=5.0 > 4.0 → 2.0→1.0 / 1.0→0.5）。"""
    from strategies.neckline.price_levels import compute_price_levels
    lv = compute_price_levels(c_star=100.0, high=10.0, atr=2.0,   # H/ATR=5>4
                              stop_atr_mult=1.0, buy_limit_atr_mult=1.0,
                              tp1_h_mult=1.0, tp_h_mult=2.0,
                              cancel_thresh_mult=1.0,
                              tp_adapt_h_atr=4.0, tp_adapt_scale=0.5)
    assert lv.tp2 == 110.0      # 100 + (2.0×0.5)×10
    assert lv.tp1 == 105.0      # 100 + (1.0×0.5)×10
    assert lv.stop == 98.0      # stop 不动
    assert lv.buy_limit == 102.0 and lv.cancel_on == 110.0   # buy/cancel 不动


def test_tp_adact_shallow_form_unchanged():
    """H/ATR 未超阈值：自适应不触发（3.5 < 4.0）。"""
    from strategies.neckline.price_levels import compute_price_levels
    lv = compute_price_levels(c_star=100.0, high=7.0, atr=2.0,   # H/ATR=3.5<4
                              stop_atr_mult=1.0, buy_limit_atr_mult=1.0,
                              tp1_h_mult=1.0, tp_h_mult=2.0,
                              cancel_thresh_mult=1.0,
                              tp_adapt_h_atr=4.0, tp_adapt_scale=0.5)
    assert lv.tp2 == 114.0 and lv.tp1 == 107.0   # 原乘数
