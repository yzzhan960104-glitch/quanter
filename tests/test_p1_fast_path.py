# -*- coding: utf-8 -*-
"""P1 向量化 fast path 专项测试（TDD：先红后绿 · spec §2 行为等价红线）。

物理意图（Why 本文件存在）：P1 把识别热路径从「逐日 DataFrame 切片 + Python 双循环」
改造为「全序列 numpy 数组 + 极值掩码 + 布尔矩阵聚类」，本文件是 fast path 的等价性防线：
  ① local_extrema_mask 与旧 local_minima/local_maxima 的位置语义逐位一致（含平原/并列极值）；
  ② _neckline_cluster 与旧 search_neckline 的聚集定位+压制验证逐位一致（含衰减权重改判）；
  ③ _detect_core_window 与 detect_neckline_method 全守卫逐字段一致；
  ④ detect_signal_fast 与 detect_signal 的 Signal 装配逐字段一致（数组路径==df 路径）。

不测模拟层（simulate_exit 非 hot path——P0-1 实测 cumtime 仅 0.3%，P1 不碰，已由
test_neckline_core.py 覆盖）。最终端到端等价由 P0-3 冻结基线 compare() 兜底
（15 信号/10 标的，旧实现 golden 输出）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

from tests._neckline_fixtures import ohlc as _ohlc, synth_pattern as _synth_pattern
import pytest

# 项目根挂 sys.path（与 test_neckline_recognition.py 同口径）
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from strategies.neckline.method_v0 import (  # noqa: E402
    DEFAULTS,
    compute_atr,
    detect_neckline_method,
    detect_signal,
    detect_signal_fast,
    local_extrema_mask,
    local_minima,
    local_maxima,
    _neckline_cluster,
    _detect_core_window,
)
from strategies.neckline.backtest import EXEC_DEFAULTS  # noqa: E402


# ============================================================================
# 参考实现（旧算法逐位复刻，测试内独立 ground truth——不复用生产函数防循环论证）
# ============================================================================
def _ref_min_positions(vals: np.ndarray, w: int):
    """旧 local_minima 位置语义（<= 左右窗，排除首尾各 w 根）。"""
    n = len(vals)
    return [i for i in range(w, n - w)
            if vals[i] <= vals[i - w:i].min() and vals[i] <= vals[i + 1:i + w + 1].min()]


def _ref_max_positions(vals: np.ndarray, w: int):
    """旧 local_maxima/search_neckline 内联 tops 位置语义（>= 左右窗）。"""
    n = len(vals)
    return [i for i in range(w, n - w)
            if vals[i] >= vals[i - w:i].max() and vals[i] >= vals[i + 1:i + w + 1].max()]


def _ref_cluster(highs: np.ndarray, closes: np.ndarray, atr_val: float,
                 min_touches: int, min_supp: float, top_window: int = 3,
                 decay_tau=None):
    """旧 search_neckline 双循环逐位复刻（含首最大语义 + 衰减压制）。"""
    import math
    n = len(highs)
    if n == 0:
        return None, 0.0
    tops = []
    for i in range(top_window, n - top_window):
        left = highs[i - top_window:i]
        right = highs[i + 1:i + top_window + 1]
        if highs[i] >= left.max() and highs[i] >= right.max():
            tops.append((i, float(highs[i])))
    if len(tops) < min_touches:
        return None, 0.0
    best_c, best_score = None, 0.0
    for ci, c in tops:
        touches_eq = 0
        score = 0.0
        for ti, t in tops:
            if abs(t - c) <= atr_val:
                touches_eq += 1
                if decay_tau and decay_tau > 0:
                    dt = (n - 1) - ti
                    score += math.exp(-dt / decay_tau)
                else:
                    score += 1.0
        if score > best_score and touches_eq >= min_touches:
            best_c, best_score = float(c), score
    if best_c is None:
        return None, 0.0
    if decay_tau and decay_tau > 0:
        weights = [math.exp(-((n - 1) - i) / decay_tau) for i in range(n)]
        sup_num = sum(w for i, w in enumerate(weights) if closes[i] < best_c)
        suppression = float(sup_num / sum(weights))
    else:
        suppression = float((closes < best_c).sum() / n)
    if suppression < min_supp:
        return None, 0.0
    return best_c, suppression


# ============================================================================
# ① local_extrema_mask 与旧 local_minima/local_maxima 位置语义等价
# ============================================================================
_VALS = np.array([5.0, 3.0, 3.0, 4.0, 2.0, 2.0, 6.0, 1.0, 3.0, 3.0, 7.0, 3.0, 5.0])


@pytest.mark.parametrize("w", [1, 2, 3])
def test_local_extrema_mask_matches_local_minima(w):
    """掩码 True 位 == 旧 local_minima 位置集合（含平原并列：连续相等低点都算）。"""
    mask = local_extrema_mask(_VALS, w, kind="min")
    assert np.flatnonzero(mask).tolist() == _ref_min_positions(_VALS, w)


@pytest.mark.parametrize("w", [1, 2, 3])
def test_local_extrema_mask_matches_local_maxima(w):
    """掩码 True 位 == 旧 local_maxima 位置集合（>= 并列极大都算）。"""
    mask = local_extrema_mask(_VALS, w, kind="max")
    assert np.flatnonzero(mask).tolist() == _ref_max_positions(_VALS, w)


def test_local_extrema_mask_matches_old_functions_directly():
    """直接对拍旧 local_minima/local_maxima 返回值序列（旧函数保留，是行为锚）。"""
    for w in (1, 2, 3):
        mask_min = local_extrema_mask(_VALS, w, kind="min")
        assert local_minima(_VALS, w) == [float(v) for v in _VALS[mask_min]]
        mask_max = local_extrema_mask(_VALS, w, kind="max")
        assert local_maxima(_VALS, w) == [float(v) for v in _VALS[mask_max]]


def test_local_extrema_mask_short_series():
    """短序列（n < 2w+1）→ 全 False（旧 range 空循环等价）；边界 n==2w+1 仅位置 w 可 True。"""
    short = np.array([1.0, 2.0, 1.0])
    assert not local_extrema_mask(short, w=2, kind="min").any()
    assert not local_extrema_mask(short, w=2, kind="max").any()
    # n == 2w+1 = 5，w=2：唯一候选位置 2（左 2 右 2）——值 2.0 是极大 → True
    edge = np.array([1.0, 1.0, 2.0, 1.0, 1.0])
    assert local_extrema_mask(edge, w=2, kind="max").tolist() == [False, False, True, False, False]


# ============================================================================
# ② _neckline_cluster 与旧 search_neckline 聚集定位+压制验证等价
# ============================================================================
def test_neckline_cluster_matches_ref_equal_weight():
    """等权聚类：双 tops ±ATR 带内 → 首最大（平局取位置靠前）；压制时长同口径。

    n=12/w=3 有效 tops 位置 [3,8]：pos3(100) + pos8(101) 两处 local max（间隔 5 ≥ w+1，
    互为独立极值），|101-100|=1 ≤ atr=3 带内聚集。首最大 = pos3 的 100。
    """
    highs = np.array([92.0, 93.0, 94.0, 100.0, 95.0, 94.0, 93.0, 92.0, 101.0, 96.0, 95.0, 94.0])
    closes = np.array([95.0] * 12, dtype=float)
    mask = local_extrema_mask(highs, 3, kind="max")
    got = _neckline_cluster(highs, closes, atr_val=3.0, min_touches=2, min_supp=0.6,
                            tops_mask=mask, decay_tau=None)
    want = _ref_cluster(highs, closes, 3.0, 2, 0.6)
    assert got[0] == want[0]
    assert got[1] == pytest.approx(want[1], rel=1e-12)
    assert got[0] == 100.0                      # 100/101 平局 → 首个（pos3 的 100）
    assert got[1] == pytest.approx(1.0)         # 12/12 close<100


def test_neckline_cluster_matches_ref_decay_weighting():
    """衰减权重改判：等权下远集群（位置靠前）首最大胜；衰减下近集群（近期 tops）胜。

    n=20/w=3 有效 tops [3,16]：A 簇={pos3 100, pos7 101}（远），B 簇={pos11 103,
    pos15 104}（近），ATR=1.5——簇内 |差值|=1 ≤ 1.5 聚集，簇间 |101-103|=2 > 1.5 隔断。
    等权 score 各 2 → 首最大=100（pos3）；衰减 tau=2 下 B 簇权重（exp(-4)+exp(-2)≈0.153）
    >> A 簇（exp(-8)+exp(-6)≈0.0028）→ 103（pos11）胜——验证权重参与选位（非仅计数）。
    """
    highs = np.array([96.0, 97.0, 98.0, 100.0, 98.0, 97.0, 96.0, 101.0, 98.0, 97.0,
                      96.0, 103.0, 99.0, 98.0, 97.0, 104.0, 99.0, 98.0, 97.0, 96.0])
    closes = np.array([95.0] * 20, dtype=float)
    mask = local_extrema_mask(highs, 3, kind="max")
    # 等权：首最大 100
    got_eq = _neckline_cluster(highs, closes, atr_val=1.5, min_touches=2, min_supp=0.5,
                               tops_mask=mask, decay_tau=None)
    want_eq = _ref_cluster(highs, closes, 1.5, 2, 0.5)
    assert got_eq[0] == want_eq[0]
    assert got_eq[1] == pytest.approx(want_eq[1], rel=1e-12)
    assert got_eq[0] == 100.0
    # 衰减 tau=2：近期簇 103 胜
    got_decay = _neckline_cluster(highs, closes, atr_val=1.5, min_touches=2, min_supp=0.5,
                                  tops_mask=mask, decay_tau=2)
    want_decay = _ref_cluster(highs, closes, 1.5, 2, 0.5, decay_tau=2)
    assert got_decay[0] == want_decay[0]
    assert got_decay[1] == pytest.approx(want_decay[1], rel=1e-12)
    assert got_decay[0] == 103.0


def test_neckline_cluster_matches_ref_rejections():
    """拒绝分支：tops 不足 / touches 不足 / 压制不足 → (None, 0.0)。"""
    # tops 不足（mask 只有 1 个 True，min_touches=2）
    highs = np.array([90.0, 91.0, 100.0, 92.0, 91.0, 90.0, 89.0, 91.0, 90.0, 89.0])
    closes = np.array([88.0] * 10, dtype=float)
    mask = local_extrema_mask(highs, 3, kind="max")
    assert _neckline_cluster(highs, closes, 3.0, 2, 0.6, mask, None) == (None, 0.0)
    # 压制不足（close 全在颈线上 → suppression=0）
    highs2 = np.array([90.0, 91.0, 100.0, 92.0, 91.0, 90.0, 89.0, 101.0, 94.0, 93.0])
    closes2 = np.array([110.0] * 10, dtype=float)
    mask2 = local_extrema_mask(highs2, 3, kind="max")
    got = _neckline_cluster(highs2, closes2, 3.0, 2, 0.6, mask2, None)
    assert got == _ref_cluster(highs2, closes2, 3.0, 2, 0.6)
    assert got == (None, 0.0)


def test_neckline_cluster_matches_ref_random_sweep():
    """随机扫场：100 组随机 highs/closes/tau 下与旧参考实现逐位一致（含 None 分支）。

    c_star 是 top 原始值 → 精确相等；suppression 允许 rel=1e-12（np.exp/np.sum 与
    math.exp/Python sum 的末位 ULP 差，生产上被 round(...,3) 抹平，非语义差异）。
    """
    rng = np.random.default_rng(20260813)
    for _ in range(100):
        n = int(rng.integers(8, 40))
        highs = np.round(rng.uniform(90, 110, n), 2)
        closes = np.round(rng.uniform(85, 115, n), 2)
        atr = float(rng.uniform(0.5, 8))
        min_touches = int(rng.integers(2, 4))
        min_supp = float(rng.uniform(0.2, 0.9))
        tau = rng.choice([None, 5, 30])
        mask = local_extrema_mask(highs, 3, kind="max")
        got = _neckline_cluster(highs, closes, atr, min_touches, min_supp, mask, tau)
        want = _ref_cluster(highs, closes, atr, min_touches, min_supp, 3, tau)
        assert got[0] == want[0], f"c_star 分歧: highs={highs} closes={closes} atr={atr} tau={tau}"
        assert got[1] == pytest.approx(want[1], rel=1e-12), (
            f"suppression 分歧: highs={highs} closes={closes} atr={atr} tau={tau}")


# ============================================================================
# ③ _detect_core_window 与 detect_neckline_method 全守卫等价（合成形态）
# ============================================================================
def _synth_df():
    # 薄壳（2026-08-19 评审收口：与 _neckline_fixtures.ohlc 构造逐字重复）
    return _ohlc(_synth_pattern())


_CFG_W20 = {**DEFAULTS, "window": 20}


def test_detect_core_window_matches_detect_success():
    """内核 == detect_neckline_method（成功路径）：合成形态 7 守卫全过，dict 逐字段相等。"""
    df = _synth_df()
    want = detect_neckline_method(df, cfg=_CFG_W20)
    assert want is not None
    # 内核直调：窗口切片 + 窗口相对掩码（与 wrapper 同口径）
    highs = df["high"].values
    lows = df["low"].values
    closes = df["close"].values
    vols = df["volume"].values
    atr_full = compute_atr(df["high"], df["low"], df["close"], window=_CFG_W20["window"])
    atr_val = float(atr_full.iloc[-1])
    tops_mask = local_extrema_mask(highs, 3, kind="max")
    lows_mask = local_extrema_mask(lows, _CFG_W20["local_extrema_window"], kind="min")
    got = _detect_core_window(highs, lows, closes, vols, df.index, atr_val, _CFG_W20,
                              tops_mask, lows_mask)
    assert got == want


@pytest.mark.parametrize("mutate", ["no_breakout", "too_deep"])
def test_detect_core_window_matches_detect_rejections(mutate):
    """内核 == detect_neckline_method（拒绝边界）：守卫逐位一致返 None。

    参数轴瘦身（2026-08-19 精简评审 W1）：原 5 参中 no_volume/few_tops/low_suppression
    与保留两参分别同轴（数据末根变异 / cfg 收紧变异），外层拒绝已由
    test_neckline_recognition 的 5 个 test_detect_reject_* 全量钉死——本测只守
    「拒绝发生在内核而非外层丢弃」的等价性，每轴 1 个代表参数即可传递该保证。
    """
    rows = _synth_pattern()
    cfg = dict(_CFG_W20)
    if mutate == "no_breakout":
        rows[-1] = (99, 101, 98, 99, 500)
    elif mutate == "no_volume":
        rows[-1] = (102, 106, 98, 102, 100)
    elif mutate == "too_deep":
        cfg["max_h_atr"] = 2.0
    elif mutate == "few_tops":
        cfg["min_touches"] = 3
    elif mutate == "low_suppression":
        cfg["min_suppression"] = 0.99
    dates = pd.date_range("2024-01-01", periods=len(rows), freq="B")
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"], index=dates)
    assert detect_neckline_method(df, cfg=cfg) is None
    highs = df["high"].values
    lows = df["low"].values
    closes = df["close"].values
    vols = df["volume"].values
    atr_full = compute_atr(df["high"], df["low"], df["close"], window=cfg["window"])
    atr_val = float(atr_full.iloc[-1])
    tops_mask = local_extrema_mask(highs, 3, kind="max")
    lows_mask = local_extrema_mask(lows, cfg["local_extrema_window"], kind="min")
    got = _detect_core_window(highs, lows, closes, vols, df.index, atr_val, cfg,
                              tops_mask, lows_mask)
    assert got is None


# ============================================================================
# ④ detect_signal_fast == detect_signal（Signal 装配逐字段，df 路径==数组路径）
# ============================================================================
def _arr_ctx(df):
    """df → fast path 上下文（与 scan_symbol 预计算同口径）。"""
    return {
        "high": df["high"].values,
        "low": df["low"].values,
        "close": df["close"].values,
        "volume": df["volume"].values,
        "index": df.index,
    }


def _fast_ctx(df, id_cfg):
    """fast path 完整上下文：arr + 掩码 + ATR + decay_weights（对齐 scan_symbol 预计算）。"""
    arr = _arr_ctx(df)
    atr_arr = compute_atr(df["high"], df["low"], df["close"], window=id_cfg["window"]).to_numpy()
    tops_mask = local_extrema_mask(arr["high"], 3, kind="max")
    lows_mask = local_extrema_mask(arr["low"], id_cfg["local_extrema_window"], kind="min")
    tau = id_cfg.get("decay_tau")
    decay_weights = None
    if tau and tau > 0:
        from strategies.neckline.method_v0 import decay_weights_of
        decay_weights = decay_weights_of(id_cfg["window"], tau)
    return arr, atr_arr, tops_mask, lows_mask, decay_weights


def test_detect_signal_fast_equals_detect_signal():
    """突破日 T：detect_signal(df_upto) == detect_signal_fast(arr, pos)（Signal 逐字段）。

    df 路径 = 实盘 scan_live/scan_at 口径；fast path = 研究侧 scan_symbol 口径——
    两者必须装配出同一 Signal（识别单源：共享 _detect_core_window + _post_detect）。
    """
    rows = _synth_pattern() + [
        (102, 106, 98, 102, 500),     # pos20 突破日（close=102>100 带量）
        (103, 104, 102, 102.5, 100),  # pos21（凑长，不影响识别）
    ]
    dates = pd.date_range("2024-01-01", periods=len(rows), freq="B")
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"], index=dates)
    id_cfg = dict(_CFG_W20)
    T = df.index[20]
    df_T = df.loc[:T]
    base = detect_signal("TEST", df_T, id_cfg, dict(EXEC_DEFAULTS), T)
    arr, atr_arr, tops_mask, lows_mask, decay = _fast_ctx(df, id_cfg)
    fast = detect_signal_fast("TEST", arr, 20, id_cfg, dict(EXEC_DEFAULTS), T, atr_arr,
                              tops_mask=tops_mask, lows_mask=lows_mask,
                              decay_weights=decay)
    assert base is not None, "突破日必须真命中（否则本测试空转）"
    assert fast == base


def test_detect_signal_fast_equals_detect_signal_sweep():
    """随机扫场：多组随机形态 × 多 cfg × decay 档，df 路径 == 数组路径（含窗口边界陷阱）。

    物理意图：全序列掩码在「窗口首尾各 w 根的边界区」的 True 位（其局部极值判定用了
    窗口外邻居）若未被裁剪会伪局部极值混入聚集候选——P0-3 冻结基线曾抓到该分叉
    （neckline 11.226 vs 11.313 类差异）。随机数据天然高频产出边界极值，本扫场是
    该裁剪语义的回归防线：fast path 与 df 路径（现场在窗口上算掩码）必须逐字段相等。
    """
    rng = np.random.default_rng(20260813)
    for trial in range(60):
        n = int(rng.integers(30, 70))
        high = np.round(rng.uniform(8, 12, n), 3)
        low = high - np.round(rng.uniform(0.1, 1.8, n), 3)
        close = np.round((high + low) / 2 + rng.uniform(-0.4, 0.4, n), 3)
        vol = np.round(rng.uniform(100, 3000, n), 1)
        idx = pd.date_range("2024-01-01", periods=n, freq="B")
        df = pd.DataFrame({"high": high, "low": low, "close": close, "volume": vol}, index=idx)
        window = int(rng.choice([12, 20, 30]))
        id_cfg = {**DEFAULTS, "window": window,
                  "decay_tau": rng.choice([None, 30]),
                  "local_extrema_window": int(rng.choice([3, 5]))}
        arr, atr_arr, tops_mask, lows_mask, decay = _fast_ctx(df, id_cfg)
        # 多 pos 扫场（2026-08-13 外部评审 P1-I2 补实）：滚动扫描中每个 i 都是一次
        # 边界裁剪——只测末窗口会让中间窗口的掩码裁剪语义裸奔
        for pos in sorted({n - 1, n - 7, window + 7}):
            if pos < window or pos >= n:
                continue
            date = df.index[pos]
            df_T = df.iloc[:pos + 1]
            base = detect_signal("TEST", df_T, id_cfg, dict(EXEC_DEFAULTS), date)
            fast = detect_signal_fast("TEST", arr, pos, id_cfg, dict(EXEC_DEFAULTS), date,
                                      atr_arr, tops_mask=tops_mask, lows_mask=lows_mask,
                                      decay_weights=decay)
            assert fast == base, (
            f"trial={trial} window={window} w_ext={id_cfg['local_extrema_window']} "
            f"tau={id_cfg['decay_tau']}\nbase={base}\nfast={fast}")


def test_detect_signal_fast_cancel_on_equivalent():
    """cancel_on 守卫等价：close 冲天突破场景下 fast 与 df 路径同为 None。"""
    rows = _synth_pattern() + [
        (115.0, 118.0, 110.0, 115.0, 5000.0),  # pos20 冲天突破（close 远超 cancel_on）
    ]
    dates = pd.date_range("2024-01-01", periods=len(rows), freq="B")
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"], index=dates)
    id_cfg = dict(_CFG_W20)
    T = df.index[20]
    df_T = df.loc[:T]
    base = detect_signal("TEST", df_T, id_cfg, dict(EXEC_DEFAULTS), T)
    arr, atr_arr, tops_mask, lows_mask, decay = _fast_ctx(df, id_cfg)
    fast = detect_signal_fast("TEST", arr, 20, id_cfg, dict(EXEC_DEFAULTS), T, atr_arr,
                              tops_mask=tops_mask, lows_mask=lows_mask,
                              decay_weights=decay)
    assert fast is None and base is None


def test_detect_signal_fast_decay_tau_equivalent():
    """decay_tau=30 激活下 fast 与 df 路径逐字段一致（衰减权重/压制口径对齐）。"""
    rows = _synth_pattern() + [
        (102, 106, 98, 102, 500),
        (103, 104, 102, 102.5, 100),
    ]
    dates = pd.date_range("2024-01-01", periods=len(rows), freq="B")
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"], index=dates)
    id_cfg = {**_CFG_W20, "decay_tau": 30}
    T = df.index[20]
    df_T = df.loc[:T]
    base = detect_signal("TEST", df_T, id_cfg, dict(EXEC_DEFAULTS), T)
    arr, atr_arr, tops_mask, lows_mask, decay = _fast_ctx(df, id_cfg)
    fast = detect_signal_fast("TEST", arr, 20, id_cfg, dict(EXEC_DEFAULTS), T, atr_arr,
                              tops_mask=tops_mask, lows_mask=lows_mask,
                              decay_weights=decay)
    assert fast == base
