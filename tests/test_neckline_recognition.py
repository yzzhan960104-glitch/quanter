# -*- coding: utf-8 -*-
"""颈线法识别层 + scan_symbol 编排回归测试（P0 follow-up · docs/neckline-method.md §6）。

物理意图（Why 本文件存在）：
    detect_neckline_method 是颈线法识别主流程（6 个调用者、零单测），耦合 7 个守卫
    （顶部聚集 + 压制时长 + 双底 + 突破 + 带量 + 形态深度 H/ATR + 盈亏比 rr）；
    scan_symbol 是单标的「滚动识别 → cooldown 去重 → simulate_exit → 收集 filled」编排。
    本文件分层钉死：
      ① 基元（local_minima/maxima/compute_atr）——纯函数，精确断言
      ② search_neckline——颈线聚集定位 + 压制验证（识别的灵魂）
      ③ detect_neckline_method 成功路径 + 5 个拒绝边界（每个守卫单独证伪）
      ④ scan_symbol 编排（monkeypatch detect 隔离识别，专测去重+模拟+收集链路）

    与 tests/test_neckline_core.py（执行层 simulate_exit/kelly_metrics）互补，
    合起来覆盖颈线法 detect → simulate → kelly 完整链路的 single source of truth。

合成形态设计（_synth_pattern）：
    颈线=100 / bottom=90 / H=10，20 根 OHLCV，手算 ATR≈3.6 → H/ATR≈2.78 < max_h_atr=4。
    顶部高点：pos5(100) + pos12(101) 两处 local maxima（±ATR 带内聚集）
    底部谷：  pos8(90) + pos15/16(91) 两处 local minima（[min, min+ATR] 带内，bottom_set={90,91}）
    压制：    19/20 根 close<100 → suppression=0.95 ≥ 0.6
    突破：    末根 pos19 close=102 > 100
    带量：    末根 vol=500 ≥ 1.5×vol5(=180)
"""
import sys
from pathlib import Path

import pandas as pd
import pytest

# scripts/ 无 __init__.py（namespace 包），直接加 sys.path（与 test_neckline_core 同范式）
_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import neckline_backtest as nb  # noqa: E402  （monkeypatch scan_symbol 的 detect 用）
from neckline_backtest import scan_symbol  # noqa: E402
from neckline_method_v0 import (  # noqa: E402
    detect_neckline_method,
    search_neckline,
    local_minima,
    local_maxima,
    compute_atr,
    DEFAULTS,
)


# ============================================================================
# 合成 OHLCV 辅助
# ============================================================================
def _ohlc(rows, start="2024-01-01"):
    """rows: [(open, high, low, close, volume), ...] → DatetimeIndex DataFrame（工作日）。"""
    dates = pd.date_range(start, periods=len(rows), freq="B")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"], index=dates)


def _synth_pattern():
    """合成 20 根可识别颈线形态（颈线=100 / bottom=90 / H=10 / ATR≈3.6）。

    每根显式指定，确保 detect 的 7 个守卫全部通过（见模块 docstring 的设计推演）。
    返回 [(open, high, low, close, volume), ...]。拒绝路径测试基于此单一条件破坏。
    """
    return [
        (91, 93, 90, 91, 100),       # pos0
        (92, 94, 91, 92, 100),       # pos1
        (93, 95, 92, 93, 100),       # pos2
        (94, 96, 93, 94, 100),       # pos3
        (95, 97, 94, 95, 100),       # pos4
        (97, 100, 96, 98, 100),      # pos5  ← top1（local max, high=100）
        (95, 96, 93, 94, 100),       # pos6
        (93, 94, 91, 92, 100),       # pos7
        (91, 93, 90, 91, 100),       # pos8  ← bottom1（local min, low=90=min_price）
        (93, 95, 92, 93, 100),       # pos9
        (95, 97, 94, 95, 100),       # pos10
        (97, 99, 96, 97, 100),       # pos11
        (99, 101, 97, 99, 100),      # pos12 ← top2（local max, high=101）
        (97, 99, 95, 96, 100),       # pos13
        (94, 96, 92, 93, 100),       # pos14
        (92, 94, 91, 92, 100),       # pos15 ← bottom2 区（local min, low=91）
        (92, 94, 91, 92, 100),       # pos16 ← bottom（local min, low=91，与 pos15 连续平原）
        (93, 95, 92, 93, 100),       # pos17
        (96, 98, 95, 97, 100),       # pos18
        (102, 106, 98, 102, 500),    # pos19 ← 突破日（close=102>100, vol=500 放量）
    ]


# cfg：识别窗口缩到 20（默认 60），便于用 20 根合成形态测试；其余参数沿用 DEFAULTS
_CFG_W20 = {**DEFAULTS, "window": 20}


# ============================================================================
# ① 基元
# ============================================================================
def test_local_minima():
    """局部极小值：某点 ≤ 左右各 w 根 → 离散低点；排除首尾各 w 根。"""
    # 基元接收 pd.Series/numpy（内部用 .min()/.max()，detect 调用时传 .values）
    assert local_minima(pd.Series([5, 4, 3, 2, 1, 2, 3, 4, 5]), w=2) == [1.0]
    # 多个极小（w=1）：pos1=3、pos3=4、pos5=2 都是局部谷
    assert local_minima(pd.Series([5, 3, 5, 4, 6, 2, 6]), w=1) == [3.0, 4.0, 2.0]
    # 平原（连续相等）≤ 邻域 → 都算（去重在调用方处理）


def test_local_maxima():
    """局部极大值：某点 ≥ 左右各 w 根 → 离散高点（颈线顶部聚集的输入）。"""
    assert local_maxima(pd.Series([1, 2, 3, 4, 5, 4, 3, 2, 1]), w=2) == [5.0]
    assert local_maxima(pd.Series([2, 5, 3, 6, 2, 4, 1]), w=1) == [5.0, 6.0, 4.0]


def test_compute_atr():
    """ATR = TR 的 window 日均值；TR = max(H−L, |H−前C|, |L−前C|)，含跳空缺口。

    构造 3 根、每根 TR 恰为 4（H−L=4 且与前收的缺口 ≤4）→ rolling 均值恒为 4。
    """
    df = pd.DataFrame({
        "high": [12, 13, 11],
        "low": [8, 9, 7],
        "close": [10, 11, 9],
    })
    atr = compute_atr(df["high"], df["low"], df["close"], window=14)
    # bar0: TR=max(4, NaN, NaN)=4（NaN 被 max 忽略）；bar1: max(4,|13−10|=3,|9−10|=1)=4；
    # bar2: max(4,|11−11|=0,|7−11|=4)=4。min_periods=1 逐根扩窗均值皆 4。
    assert atr.iloc[0] == pytest.approx(4.0)
    assert atr.iloc[1] == pytest.approx(4.0)
    assert atr.iloc[2] == pytest.approx(4.0)


# ============================================================================
# ② search_neckline · 颈线聚集定位 + 压制验证
# ============================================================================
def test_search_neckline_finds_cluster():
    """两步角色分离：① 顶部高点聚集定位 c*（±ATR 带内含最多顶部的价位）
    ② 压制时长 P(close<c*) 验证。

    构造：pos5/pos12 两个 local max（100/101，±ATR=3 带内聚集），19/20 close<100。
    旧版 bug：用压制最大化选位会选到窗口最高价——压制只能验证、不能选位。
    """
    highs = pd.Series([90, 91, 92, 93, 94, 100, 93, 92, 91, 90, 89, 94, 101, 94, 93, 92, 91, 90, 89, 88])
    closes = pd.Series([88, 89, 90, 91, 92, 95, 93, 92, 91, 90, 89, 95, 96, 94, 93, 92, 91, 90, 89, 105])
    # 末根 close=105（≥100）不计入压制分子 → 压制 = 19/20 = 0.95
    c_star, suppression = search_neckline(
        highs, closes, atr_val=3.0, min_touches=2, min_supp=0.6,
    )
    assert c_star == 100.0                      # 聚集定位到 100（100/101 平局，首个胜出）
    assert suppression == pytest.approx(0.95)   # 19/20 close < 100


def test_search_neckline_reject_few_tops():
    """顶部高点数 < min_touches → 连不成颈线，返回 (None, 0)。"""
    highs = pd.Series([90, 91, 92, 93, 94, 100, 93, 92, 91, 90])   # 仅 pos5 一个 local max
    closes = pd.Series([88, 89, 90, 91, 92, 95, 93, 92, 91, 90])
    c_star, suppression = search_neckline(highs, closes, atr_val=3.0, min_touches=2, min_supp=0.6)
    assert c_star is None
    assert suppression == 0.0


# ============================================================================
# ③ detect_neckline_method · 成功路径 + 5 个拒绝边界
# ============================================================================
def test_detect_recognizes_pattern():
    """合成形态完整通过 7 守卫 → 返回候选 dict（颈线=100/bottom=90/H=10/rr=2）。"""
    df = _ohlc(_synth_pattern())
    res = detect_neckline_method(df, cfg=_CFG_W20)
    assert res is not None
    assert res["neckline"] == 100.0
    assert res["bottom"] == 90.0
    assert res["n_bottoms"] == 2          # bottom_set = {90.0, 91.0}
    assert res["H"] == 10.0
    assert res["rr"] == 2.0               # 结构恒 2H/H = 2.0
    assert res["entry"] == 100.0          # 进场 = 颈线（挂单等回踩，不追涨）
    assert res["H_over_ATR"] < 4.0        # 深度守卫通过（防暴跌反弹）


def test_detect_reject_no_breakout():
    """守卫·突破：末根 close ≤ c* → 未突破，返回 None。"""
    rows = _synth_pattern()
    rows[-1] = (99, 101, 98, 99, 500)     # 末根 close=99 ≤ 颈线 100 → 未突破
    df = _ohlc(rows)
    assert detect_neckline_method(df, cfg=_CFG_W20) is None


def test_detect_reject_no_volume():
    """守卫·带量：末根 vol < breakout_vol_mult × vol5 → 突破未带量，返回 None。"""
    rows = _synth_pattern()
    rows[-1] = (102, 106, 98, 102, 100)   # vol=100，vol5=100 → 100 < 1.5×100=150
    df = _ohlc(rows)
    assert detect_neckline_method(df, cfg=_CFG_W20) is None


def test_detect_reject_too_deep():
    """守卫·深度：H/ATR > max_h_atr → 暴跌反弹嫌疑（实证深形态胜率 27%），返回 None。

    合成形态 H/ATR≈2.78，把 max_h_atr 收紧到 2.0 → 2.78 > 2.0 触发拒绝。
    """
    df = _ohlc(_synth_pattern())
    cfg = {**_CFG_W20, "max_h_atr": 2.0}
    assert detect_neckline_method(df, cfg=cfg) is None


def test_detect_reject_few_tops():
    """守卫·聚集足够性：min_touches 提到 3（合成仅 2 个顶部）→ 连不成颈线，返回 None。"""
    df = _ohlc(_synth_pattern())
    cfg = {**_CFG_W20, "min_touches": 3}
    assert detect_neckline_method(df, cfg=cfg) is None


def test_detect_reject_low_suppression():
    """守卫·压制时长：min_suppression 提到 0.99（合成实际 0.95）→ 颈线无效，返回 None。"""
    df = _ohlc(_synth_pattern())
    cfg = {**_CFG_W20, "min_suppression": 0.99}
    assert detect_neckline_method(df, cfg=cfg) is None


# ============================================================================
# ④ scan_symbol · 编排链路（mock detect 隔离识别）
# ============================================================================
def test_scan_symbol_orchestration(monkeypatch):
    """scan_symbol: 滚动 detect → cooldown 去重 → simulate_exit → 收集 filled。

    用 monkeypatch 隔离识别（fake_detect 仅在 i=signal_idx 返回形态），聚焦测编排：
      预算全序列 ATR → for i in range(window, len): detect → dedup_signals →
      对每个 signal 调 simulate_exit → skip 类不计入 filled，成交类计入。

    合成 25 根：pos0-19 形态区，pos20 突破日（mock 在此识别），pos21 回踩成交，
    pos22 stop_loss 出场。scan_symbol 应返回 1 signal、1 filled（stop_loss）。
    """
    rows = _synth_pattern()                                # pos0-19（形态区）
    rows += [
        (102, 106, 98, 102, 500),   # pos20 突破日（detect 在此识别，由 mock 返回）
        (103, 104, 102, 102.5, 100),  # pos21 回踩：low=102≤buy_limit=103.6 → 成交 entry=103
        (96, 99, 95, 96, 100),      # pos22 stop：low=95≤base_stop=96.4 → stop_loss
        (100, 101, 99, 100, 100),   # pos23 凑长（持有期循环到此已被 stop break）
        (100, 101, 99, 100, 100),   # pos24
    ]
    df = _ohlc(rows)
    signal_idx = 20
    fake_res = {
        "formed_at": df.index[signal_idx],
        "neckline": 100.0, "bottom": 90.0, "atr": 3.6,
        "suppression": 0.95, "H_over_ATR": 2.78,
    }

    def fake_detect(d, cfg, atr_series=None):
        # 仅在输入序列长度 == signal_idx+1（即 i=signal_idx 这一轮）返回形态
        if len(d) == signal_idx + 1:
            return fake_res
        return None

    monkeypatch.setattr(nb, "detect_neckline_method", fake_detect)
    filled, n_signals, n_skip = scan_symbol(df, window=20)

    assert n_signals >= 1                  # 识别到 ≥1 个信号
    assert len(filled) == 1                # 1 笔成交（skip 类不计入）
    assert n_skip == 0
    hit = filled[0]
    assert hit["exit_reason"] == "stop_loss"
    # entry=103, stop=96.4 → avg_pnl = (96.4−103)/103 = −6.41%
    assert hit["avg_pnl_pct"] == round((96.4 - 103) / 103 * 100, 2)


# ============================================================================
# P1-b · 双轨一致性（scan_symbol 批量 == NecklineMethodStrategy.scan_at 逐 T 累积）
# ============================================================================
def test_scan_symbol_matches_strategy(monkeypatch):
    """P1-b 双轨一致性守护：研究侧 scan_symbol（param_iter 路径）== 编排侧 scan_at（execution 路径）。

    mock detect 在固定 T 返回信号（detect 是两侧共享的同一函数，非分叉点；真实分叉在
    去重 + simulate + 收集链路），断言两侧产出相同成交（signal_date/entry/exit_date/exit_reason）。
    守护 param_iter 直调 scan_symbol 与 execution 经 scan_at 不分叉——任何 simulate_exit/
    去重逻辑改动若导致两侧不一致，本测试即失败。
    """
    import neckline_backtest as nb
    import strategies.neckline_method as nm
    from strategies.neckline_method import NecklineMethodStrategy

    rows = _synth_pattern()[:20] + [
        (102, 106, 98, 102, 500),      # pos20  mock 在此返回信号
        (103, 104, 102, 102.5, 100),   # pos21  回踩成交 entry=103
        (96, 99, 95, 96, 100),         # pos22  stop_loss
        (100, 101, 99, 100, 100),
        (100, 101, 99, 100, 100),
    ]
    df = _ohlc(rows)
    signal_idx = 20
    fake_res = {"formed_at": df.index[signal_idx], "neckline": 100.0, "bottom": 90.0,
                "atr": 3.6, "suppression": 0.95, "H_over_ATR": 2.78}

    def fake_detect(d, cfg, atr_series=None):
        if len(d) == signal_idx + 1:
            return fake_res
        return None

    # 两处 import 绑定都要 patch（scan_symbol 在 neckline_backtest，scan_at 在 strategies）
    monkeypatch.setattr(nb, "detect_neckline_method", fake_detect)
    monkeypatch.setattr(nm, "detect_neckline_method", fake_detect)

    # ① 研究侧：scan_symbol 批量（param_iter 路径），id_cfg 参数化（P1-b 收敛后）
    id_cfg = {**DEFAULTS, "window": 20}
    filled_A, _, _ = scan_symbol(df, window=20, id_cfg=id_cfg)

    # ② 编排侧：NecklineMethodStrategy 逐 T 累积（execution 路径）
    strat = NecklineMethodStrategy(cfg_override={"window": 20})
    state = strat.precompute("TEST", df)
    hits_B = []
    for T in df.index[20:]:
        hits_B += strat.scan_at("TEST", df.loc[:T], T, state)

    # 断言双轨一致：成交笔数 + 关键字段对齐
    assert len(filled_A) == len(hits_B) == 1
    a, b = filled_A[0], hits_B[0]
    assert a["signal_date"] == pd.Timestamp(b["formed_at"]).date()
    assert a["entry"] == b["entry_price"]
    assert a["exit_date"] == b["exit_date"]
    assert a["exit_reason"] == b["exit_reason"]
