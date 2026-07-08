# -*- coding: utf-8 -*-
"""W 底识别测试（蔡森多头买进讯号）。

覆盖三类用例：
- test_standard_w_bottom_detected：合成标准 W 底（右脚≥左脚 + 颈线突破 + 跨度/幅度/
  幅宽张力/量价/颈线斜率 全满足）→ is_valid=True；
- test_too_short_span_rejected：跨度 < min_pattern_bars → 不识别；
- test_right_breaks_left_rejected：【Task 1 硬规则】右脚破左脚（P3 < P1×(1-tolerance)）
  → 直接否决（is_valid=False 或 None）；
- test_ma26w_filter_rejects_below_ma26w：【Task 1 校准】右底在 26 周均线之下且
  ma26w_filter=True 时否决（样本不足则放行兜底，本用例构造足量样本使 26 周线生效）。

合成序列设计要点（与 causal_pivots 阈值机制对齐）：
  causal_pivots 的 thresh = max(0.005, (atr_level/base_price)*zigzag_threshold_atr)。
  本测试用常数 atr=1.0 + 基准价 10 → thresh = max(0.005, 0.1*cfg.zigzag_threshold_atr)。
  设 cfg.zigzag_threshold_atr=0.5 → thresh=0.05（5%），即反转幅度需 >5% 方确认 pivot。
  因此合成 W 底的"左底→颈线→右底→突破"各段幅度均需 ≥6% 才能稳定产生
  谷-峰-谷-峰 四 pivot。
"""
import numpy as np
import pandas as pd
import pytest

from caisen.config import StrategyConfig
from caisen.patterns.zigzag_causal import causal_pivots, compute_atr
from caisen.patterns.w_bottom import detect, WBottom


def _atr_const(n: int, val: float = 1.0) -> pd.Series:
    """构造常数 ATR 序列（val 元/股），使 thresh 完全由 cfg.zigzag_threshold_atr 决定。

    base_price=10、atr=1.0、zigzag_threshold_atr=0.5 → thresh=0.05（5%），
    即 <6% 的小波动不构成 pivot，保证合成序列的峰谷落点稳定可预期。
    """
    return pd.Series(val, index=pd.RangeIndex(n), dtype=float)


def _mk_cfg(**overrides) -> StrategyConfig:
    """构造测试用 StrategyConfig：放宽默认约束以便短合成序列通过。

    默认 min_pattern_bars=11 严格执行蔡森原著"至少 11 根"约束；
    confirm_bars=2 短确认窗使末段突破 pivot 可被因果确认；
    w_price_tolerance=0.05（右脚可在左脚 ±5% 内，right_above_left=True 时仅约束下限）。
    """
    base = dict(
        min_pattern_bars=11,
        max_pattern_bars=60,
        zigzag_threshold_atr=0.5,
        confirm_bars=2,
        w_price_tolerance=0.05,
        min_pattern_depth=0.05,
        max_pattern_depth=0.50,
        pattern_tension_ratio=0.05,
        right_vol_shrink=1.5,         # 测试合成序列量价差异不大，放宽
        breakout_vol_multiplier=0.5,
        right_above_left=True,
        ma26w_filter=False,           # 默认关闭 26 周线（短合成序列样本不足）
        abc_wave_detect=False,        # 默认关闭 ABC 波（合成序列区段未必严格 C>A）
    )
    base.update(overrides)
    return StrategyConfig(**base)


def _build_standard_w_bottom() -> tuple:
    """合成标准 W 底序列（右脚略高于左脚 + 颈线突破）。

    序列构造（每段幅度均 >6% 触发 5% thresh 的 pivot）：
        下跌至左底 → 反弹至颈线高点 → 回落至右底 → 突破颈线 → 末尾持平确认突破 pivot。
    关键：突破高点 P4 必须距序列末尾 ≥ confirm_bars（causal_pivots 末尾隔离要求），
    故在 P4 之后追加 confirm_bars 根持平/小幅波动 K 线（不构成新 pivot）以确认 P4。

    最终序列（20 根，confirm_bars=2 → P4 后需 2 根确认）：
        [12, 11, 10, 9, 8, 7.5,   左侧下跌至左底 P1=7.5 (idx5)
         8, 8.5, 9, 10, 11,       反弹至颈线高点 P2=11 (idx10，多一根使 span>min)
         10, 9, 8.0,              回落至右底 P3=8.0 (idx13，>7.5 右脚抬高)
         9, 10, 11, 13,           突破至 P4=13 (idx17，突破颈线)
         12.5, 12.0]              末尾 2 根回踩（>5% 回撤）触发反转，确认 P4 峰
     i:  0  1  2  3  4  5  6  7  8  9 10 11 12 13 14 15 16 17 18 19
    P1=idx5(7.5)、P2=idx10(11)、P3=idx13(8.0)、P4=idx17(13)；跨度=17-5=12 > min(11)。
    注：末尾需有 >5% 回撤（13→12.0 跌 7.7%）触发 zigzag 反转，方能在 idx 17 确认峰值；
        单纯持平不会触发反转，P4 无法被 causal_pivots 确认。
    """
    close = pd.Series(
        [12.0, 11.0, 10.0, 9.0, 8.0, 7.5,   # 左侧下跌至左底 P1
         8.0, 8.5, 9.0, 10.0, 11.0,         # 反弹至颈线高点 P2（多一根使 span > min）
         10.0, 9.0, 8.0,                    # 回落至右底 P3（8.0 > 7.5 右脚抬高）
         9.0, 10.0, 11.0, 13.0,             # 突破至 P4（13 > 11 突破颈线）
         12.5, 12.0],                       # 末尾回踩确认 P4（confirm_bars=2）
        dtype=float,
    )
    high = close + 0.3
    low = close - 0.3
    # 量价：右底缩量 + 突破放量（左底量大、右底量小、突破日放大）
    vol = pd.Series(
        [200, 200, 200, 200, 200, 300,      # 左底区放量下杀
         150, 150, 150, 150, 200,           # 反弹温和
         100, 100, 100,                     # 右底缩量（< 左底）
         150, 200, 300, 500,                # 突破日放量
         150, 150],                         # 末尾回踩缩量
        dtype=float,
    )
    return close, high, low, vol


def test_standard_w_bottom_detected():
    """合成标准 W 底（右脚≥左脚 + 颈线突破）应被识别为 is_valid=True。"""
    close, high, low, vol = _build_standard_w_bottom()
    cfg = _mk_cfg()
    atr = _atr_const(len(close))
    piv = causal_pivots(close, atr, cfg)
    # 前置：合成序列应产生 ≥4 个 pivot（谷-峰-谷-峰）
    assert piv.isin([1, -1]).sum() >= 4, f"pivot 不足：{piv.tolist()}"

    res = detect(close, piv, high, low, vol, cfg)
    # 关键断言：识别成功
    assert res is not None, f"未识别 W 底，piv={piv.tolist()}"
    assert isinstance(res, WBottom)
    assert res.is_valid, f"W 底被判否决：{res.reason}"
    # 结构断言：四点齐全 + 右脚≥左脚
    assert res.p3_price >= res.p1_price * (1 - cfg.w_price_tolerance), \
        f"右脚 {res.p3_price} 破左脚 {res.p1_price}"
    # 颈线价在两高点之间
    assert res.depth > 0
    assert res.tension > 0


def test_too_short_span_rejected():
    """跨度 < min_pattern_bars → 不识别。

    构造极短 W 形（跨度仅 4，远小于 min_pattern_bars=11），应被否决。
    """
    close = pd.Series([10.0, 7.5, 11.0, 8.0, 13.0], dtype=float)
    high = close + 0.3
    low = close - 0.3
    vol = pd.Series([200, 300, 200, 100, 500], dtype=float)
    cfg = _mk_cfg(min_pattern_bars=11)
    atr = _atr_const(len(close))
    piv = causal_pivots(close, atr, cfg)
    res = detect(close, piv, high, low, vol, cfg)
    assert res is None or not res.is_valid, \
        f"短跨度 W 形不应被识别：span={ (piv.tolist()) }"


def test_right_breaks_left_rejected():
    """【Task 1 硬规则】右脚破左脚（P3 < P1×(1-tolerance)）→ 直接否决。

    构造右脚明显低于左脚的"假 W 底"（实为下跌中继）：
        左底 P1=8.0，右底 P3=7.0（破左底 12.5%，> tolerance=5%）
    right_above_left=True 时强制 P3 ≥ P1×(1-0.05)=7.6，7.0 < 7.6 → 否决。
    """
    close = pd.Series(
        [12.0, 11.0, 10.0, 9.0, 8.0,          # 下跌至左底 P1=8.0
         8.5, 9.0, 9.5, 10.0, 10.5,           # 反弹至颈线高点 P2=10.5
         9.5, 9.0, 8.5, 8.0, 7.5, 7.0,        # 再次下跌破左底至 P3=7.0（破位）
         8.0, 9.0, 10.0, 11.0],               # 反弹突破至 P4=11.0
        dtype=float,
    )
    high = close + 0.3
    low = close - 0.3
    vol = pd.Series([200] * len(close), dtype=float)
    cfg = _mk_cfg(right_above_left=True)
    atr = _atr_const(len(close))
    piv = causal_pivots(close, atr, cfg)
    res = detect(close, piv, high, low, vol, cfg)
    # 右脚破左脚 → 否决（is_valid=False 或 None）
    assert res is None or not res.is_valid, \
        f"右脚破左脚应被否决（下跌中继），但识别为有效：{res}"


def test_right_above_left_disabled_allows_lower_right():
    """right_above_left=False 时不再强制右脚≥左脚（仅用 |P3-P1|/P1 容忍度）。

    构造右脚略低于左脚但仍在 |P3-P1|/P1 ≤ tolerance 内的 W 底，应被放行。
    """
    # 左底 8.0、右底 7.7（破 3.75%，< tolerance 5%），关 right_above_left 应放行
    # 末尾加回踩确认 P4（>5% 回撤触发 zigzag 反转）
    close = pd.Series(
        [12.0, 11.0, 10.0, 9.0, 8.0,
         8.5, 9.0, 10.0, 11.0,
         10.5, 9.5, 8.5, 7.7,
         8.5, 9.5, 11.0, 13.0,
         12.0, 11.5],
        dtype=float,
    )
    high = close + 0.3
    low = close - 0.3
    vol = pd.Series([200] * len(close), dtype=float)
    cfg = _mk_cfg(right_above_left=False)
    atr = _atr_const(len(close))
    piv = causal_pivots(close, atr, cfg)
    res = detect(close, piv, high, low, vol, cfg)
    # 关 right_above_left 后，右脚在容忍度内应放行
    assert res is not None and res.is_valid, \
        f"right_above_left=False 时容忍度内右脚略低应放行：piv={piv.tolist()}"


def test_ma26w_filter_rejects_below_ma26w():
    """【Task 1 校准】右底在 26 周线之下且 ma26w_filter=True 时否决。

    构造足量样本（≥ ma26w_window=130 根）使 26 周线生效：前段长期下行（均价高），
    末段在低位构筑 W 底，使右底 P3 处的 close 明显低于 26 周均线 → ma26w_filter 否决。
    """
    n_pre = 130  # 前段长趋势，使 ma26w 在末段处于高位
    # 前段：从 50 缓慢下行至 15（均价约 30），使 ma26w 在末段 ≈ 20-30
    pre = np.linspace(50.0, 15.0, n_pre).tolist()
    # 末段：在 8-13 区间构筑 W 底（明显低于 26 周线 ~20-30）
    tail = [15.0, 12.0, 10.0, 8.0, 9.0, 11.0, 10.0, 9.0, 8.5, 9.0, 10.0, 12.0, 13.0]
    close = pd.Series(pre + tail, dtype=float)
    high = close + 0.3
    low = close - 0.3
    vol = pd.Series([200.0] * len(close))
    # ma26w_filter=True 开启 26 周线过滤
    cfg = _mk_cfg(ma26w_filter=True, ma26w_window=130, min_pattern_bars=11)
    atr = _atr_const(len(close))
    piv = causal_pivots(close, atr, cfg)
    res = detect(close, piv, high, low, vol, cfg)
    # 右底 8.5 明显低于 26 周线（约 20-30）→ ma26w_filter 否决
    assert res is None or not res.is_valid, \
        f"右底在 26 周线下应被 ma26w_filter 否决：{res}"


def test_ma26w_filter_passthrough_when_insufficient_samples():
    """【Task 1 校准·兜底】样本不足 ma26w_window 时 ma26w_filter 放行（不阻断）。

    物理意图：26 周线是长期均线，样本不足时无法计算，蔡森原著无明确兜底规则，
    本实现保守放行（避免过度过滤扼杀新上市标的的机会）。
    """
    close, high, low, vol = _build_standard_w_bottom()
    # ma26w_filter=True 但序列长度 17 < ma26w_window=130 → 兜底放行
    cfg = _mk_cfg(ma26w_filter=True, ma26w_window=130)
    atr = _atr_const(len(close))
    piv = causal_pivots(close, atr, cfg)
    res = detect(close, piv, high, low, vol, cfg)
    # 样本不足应放行（与关闭 ma26w_filter 等价）
    assert res is not None and res.is_valid, \
        f"样本不足 ma26w_window 时应兜底放行，但被否决：{res}"
