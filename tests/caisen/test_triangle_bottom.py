# -*- coding: utf-8 -*-
"""收敛三角形底部识别测试（蔡森招12 · 多头买进讯号 · 白皮书权威）。

覆盖以下用例（仿 test_w_bottom 范式：所有否决用例基于已验证的"峰-谷-峰-谷-峰"
五 pivot 序列构造，确保真实触达被测校验分支，杜绝假阳性）：

- test_standard_triangle_detected：合成标准收敛三角形（上缘降+下缘升+带量突破上缘
  + 突破进度≈0.69∈[1/2,3/4]）→ is_valid=True；
- test_not_converging_rejected + 对照：【原著硬规则】上缘不降（P3≥P1）→ 收敛校验
  否决；对照（抬高 P3 使其 < P1）→ 通过；
- test_breakout_too_late_rejected：【原著硬规则★】突破进度 >3/4（接近顶点易假
  突破）→ 位置校验否决；
- test_breakout_too_early_rejected：突破进度 <1/2（形态未成熟）→ 位置校验否决；
- test_no_volume_breakout_rejected + 对照：突破日未放量 → 量价校验否决；放量 → 通过；
- test_short_span_rejected + 对照：跨度 < min_pattern_bars → 跨度校验否决；
- test_field_regressions：neckline=上缘投影、bottom=min(P2,P4)、edge_height=P1−P2。

合成序列设计要点（与 causal_pivots 阈值机制对齐，杜绝假阳性）：
  causal_pivots 的 thresh = max(0.005, (atr_level/base_price)*zigzag_threshold_atr)。
  常数 atr=1.0 + base_price=15 → thresh=max(0.005, 0.5/15)=0.0333（约 3.3%），
  即 <3.3% 的小波动不构成 pivot。合成三角形的各段幅度均 >8% >> thresh，稳定产出
  峰-谷-峰-谷-峰 五 pivot。

  关键不变量（防 review Important#1 假阳性）：detect 从尾部取最后 5 个 pivot 必须
  是 峰(1)-谷(-1)-峰(1)-谷(-1)-峰(1)。合成序列开头先有背景下跌段（产出 P1 之前
  的峰+谷两个 pivot），使尾部 5 pivot 恰为 P1..P5。末尾追加 confirm_bars 根回踩
  （>5% 回撤）以确认 P5 突破峰 pivot。
"""
import numpy as np
import pandas as pd
import pytest

from caisen.config import StrategyConfig
from caisen.patterns.zigzag_causal import causal_pivots
from caisen.patterns.triangle_bottom import detect, TriangleBottom


def _atr_const(n: int, val: float = 1.0) -> pd.Series:
    """构造常数 ATR 序列（val 元/股），使 thresh 完全由 base_price × cfg 决定。

    base_price=15、atr=1.0、zigzag_threshold_atr=0.5 → thresh=max(0.005, 0.0333)=0.0333
    （约 3.3%），合成三角形各段幅度 >8% >> thresh，峰谷落点稳定可预期。
    """
    return pd.Series(val, index=pd.RangeIndex(n), dtype=float)


def _mk_cfg(**overrides) -> StrategyConfig:
    """构造测试用 StrategyConfig（收敛三角形参数对齐白皮书招12）。

    设计意图：
      - max_pattern_depth 覆盖为 triangle_max_pattern_depth=0.6（模拟 screener 的
        model_copy 覆写——detect 内部只读 cfg.max_pattern_depth 单字段，无分类型概念，
        仿 test_head_shoulder 的 max_pattern_depth=1.0 模式）；
      - 其他参数与 test_w_bottom 一致（min_pattern_bars=11、confirm_bars=2 等）；
      - ma26w_filter/abc_wave_detect 默认关（短合成序列）。
    """
    base = dict(
        min_pattern_bars=11,
        max_pattern_bars=60,
        zigzag_threshold_atr=0.5,
        confirm_bars=2,
        min_pattern_depth=0.05,
        max_pattern_depth=0.6,            # 模拟 screener 覆写为 triangle_max_pattern_depth
        pattern_tension_ratio=0.05,
        breakout_vol_multiplier=1.5,
        right_above_left=True,            # 三角形不校验此项，但保持 cfg 完整
        ma26w_filter=False,
        abc_wave_detect=False,
        triangle_breakout_min=0.5,
        triangle_breakout_max=0.75,
    )
    base.update(overrides)
    return StrategyConfig(**base)


def _vol_pattern(n: int, p5_i: int, baseline: float = 200.0) -> pd.Series:
    """收敛三角形量价模式：颈线段(P1→P4)温和 baseline + P5 突破日放量。

    物理意图（白皮书招12）：三角形主体为蓄势收敛（温和量能 baseline），P5 突破日
    放量（资金增量进场选择方向）≥ baseline × breakout_vol_multiplier(1.5)。
    """
    vol = pd.Series(baseline, index=pd.RangeIndex(n), dtype=float)
    vol.iloc[p5_i] = baseline * 2.5   # P5 突破放量（500 ≥ 200×1.5=300）
    return vol


def _build_standard_triangle() -> tuple:
    """合成标准收敛三角形序列（上缘降+下缘升+突破进度≈0.69∈[1/2,3/4]）。

    序列构造（base=15，thresh≈3.3%，各段幅度 >8% 稳定触发 pivot）：
        背景下跌 15→10（产出 P_-1峰@0 + P0谷@5，使尾部5 pivot 恰为 P1..P5）
        → 反弹 P1峰=12.5@8 → 跌 P2谷=10.8@11（>P0=10）→ 反弹 P3峰=12.0@14（<P1 上缘降）
        → 跌 P4谷=10.9@17（>P2 下缘升）→ 突破 P5峰=13.0@20（>上缘投影）
        → 末尾 2 根回踩（>5% 回撤）确认 P5 峰。

    收敛校验：P3=12.0<12.5=P1 ✓；P4=10.9>10.8=P2 ✓。
    边长 edge_height=P1-P2=1.7；depth=1.7/10.8=0.157∈(0.05,0.6]。
    突破进度：(20-8)/(apex-8)，apex≈25.5 → progress≈0.69∈[0.5,0.75] ✓。
    P5 处上缘投影≈11.5 < P5=13.0 → 突破确认 ✓。
    """
    close = pd.Series(
        [15.0, 14.0, 13.0, 12.0, 11.0, 10.0,   # idx0-5 背景跌到 P0谷=10
         10.8, 11.7, 12.5,                      # idx6-8 反弹到 P1峰=12.5@8
         12.0, 11.4, 10.8,                      # idx9-11 跌到 P2谷=10.8@11
         11.2, 11.6, 12.0,                      # idx12-14 反弹到 P3峰=12.0@14
         11.6, 11.25, 10.9,                     # idx15-17 跌到 P4谷=10.9@17
         11.5, 12.2, 13.0,                      # idx18-20 突破到 P5峰=13.0@20
         12.5, 12.0],                           # idx21-22 回踩确认 P5
        dtype=float,
    )
    high = close + 0.3
    low = close - 0.3
    vol = _vol_pattern(len(close), p5_i=20)
    return close, high, low, vol


def _last5_pivots(piv: pd.Series) -> list:
    """提取因果 pivot 序列尾部最后 5 个 pivot（用于断言顺序正确性）。"""
    nz = [i for i in range(len(piv)) if piv.iloc[i] != 0]
    return [int(piv.iloc[i]) for i in nz[-5:]]


# ---------------------------------------------------------------------------
# 用例 1：标准收敛三角形识别
# ---------------------------------------------------------------------------
def test_standard_triangle_detected():
    """合成标准收敛三角形（上缘降+下缘升+带量突破+进度∈[1/2,3/4]）→ is_valid=True。

    前置断言：causal_pivots 在尾部稳定产出 峰-谷-峰-谷-峰 五 pivot（防 Important#1
    假阳性——若顺序错误，detect 第一步即 return None，所有后续断言无意义）。
    """
    close, high, low, vol = _build_standard_triangle()
    cfg = _mk_cfg()
    atr = _atr_const(len(close))
    piv = causal_pivots(close, atr, cfg)
    # 前置：合成序列应产生 ≥5 个 pivot，且尾部 5 pivot 顺序为 峰-谷-峰-谷-峰
    assert piv.isin([1, -1]).sum() >= 5, f"pivot 不足：{piv.tolist()}"
    assert _last5_pivots(piv) == [1, -1, 1, -1, 1], \
        f"尾部 5 pivot 顺序错误（应为 峰-谷-峰-谷-峰）：{_last5_pivots(piv)}"

    res = detect(close, piv, high, low, vol, cfg)
    # 关键断言：识别成功
    assert res is not None, f"未识别收敛三角形，piv={piv.tolist()}"
    assert isinstance(res, TriangleBottom)
    assert res.is_valid, f"收敛三角形被判否决：{res.reason}"
    # 结构断言：收敛 + 边长 + 进度
    assert res.p3_price < res.p1_price, f"上缘应递减 P3<P1"
    assert res.p4_price > res.p2_price, f"下缘应递增 P4>P2"
    assert res.edge_height > 0
    assert res.depth > 0
    assert res.tension > 0
    # 突破进度应在 [1/2, 3/4]（白皮书招12硬规则）
    assert cfg.triangle_breakout_min <= res.breakout_progress <= cfg.triangle_breakout_max, \
        f"突破进度 {res.breakout_progress} 应在 [{cfg.triangle_breakout_min}, {cfg.triangle_breakout_max}]"


# ---------------------------------------------------------------------------
# 用例 2：字段回归（neckline/bottom/edge_height 定义）
# ---------------------------------------------------------------------------
def test_field_regressions():
    """neckline=上缘投影、bottom=min(P2,P4)、edge_height=P1-P2（契约一致性回归）。"""
    close, high, low, vol = _build_standard_triangle()
    cfg = _mk_cfg()
    atr = _atr_const(len(close))
    piv = causal_pivots(close, atr, cfg)
    res = detect(close, piv, high, low, vol, cfg)
    assert res is not None and res.is_valid
    # bottom_price = min(P2, P4)（真实最低谷，止损用）
    assert res.bottom_price == pytest.approx(min(res.p2_price, res.p4_price), abs=1e-9)
    # edge_height = P1 - P2（边长，满足点用）
    assert res.edge_height == pytest.approx(res.p1_price - res.p2_price, abs=1e-9)
    # neckline_price = 上缘(P1-P3)在突破点投影（应 < P1，因上缘递减且突破点>P3）
    assert res.neckline_price < res.p1_price


# ---------------------------------------------------------------------------
# 用例 3：非收敛否决 + 对照（上缘不降 P3≥P1）
# ---------------------------------------------------------------------------
def test_not_converging_rejected():
    """【原著硬规则】上缘不降（P3≥P1）→ 收敛校验否决。

    构造 P3=P1（上缘水平，非递减），尾部 5 pivot 仍为 峰-谷-峰-谷-峰，唯一否决源
    是收敛校验（P3<P1 不成立）。
    """
    # 基于 standard 序列，把 P3 抬到 = P1（12.5），使上缘水平
    close, high, low, vol = _build_standard_triangle()
    close = close.copy()
    # P3@idx14 原 12.0 → 抬到 12.5（=P1），上缘不再递减
    close.iloc[12:15] = [11.7, 12.1, 12.5]   # idx12-14 反弹到 P3=12.5=P1
    high = close + 0.3
    low = close - 0.3
    cfg = _mk_cfg()
    atr = _atr_const(len(close))
    piv = causal_pivots(close, atr, cfg)
    # 前置：尾部 5 pivot 顺序正确（防顺序错误假阳性）
    assert _last5_pivots(piv) == [1, -1, 1, -1, 1], \
        f"尾部 5 pivot 顺序错误：{_last5_pivots(piv)}"

    res = detect(close, piv, high, low, vol, cfg)
    # P3≥P1（上缘不降）→ 收敛校验否决
    assert res is None or not res.is_valid, \
        f"上缘不降（P3≥P1）应被收敛校验否决，但识别为有效：{res}"


def test_not_converging_control_passes_when_upper_descending():
    """【唯一否决源对照】上缘递减（P3<P1）时同一结构应通过（即 standard 序列）。

    物理意图：与 test_not_converging_rejected 对照，standard 序列 P3=12.0<12.5=P1
    上缘递减，detect 应 is_valid=True，证明非收敛序列的否决唯一来自收敛校验。
    """
    close, high, low, vol = _build_standard_triangle()
    cfg = _mk_cfg()
    atr = _atr_const(len(close))
    piv = causal_pivots(close, atr, cfg)
    res = detect(close, piv, high, low, vol, cfg)
    assert res is not None and res.is_valid, \
        f"上缘递减应通过（证明非收敛否决唯一来自收敛校验）：{res}"


# ---------------------------------------------------------------------------
# 用例 4：突破位置硬规则（>3/4 太晚 / <1/2 太早）
# ---------------------------------------------------------------------------
def test_breakout_too_late_rejected():
    """【原著硬规则★】突破进度 >3/4（接近三角形顶点）→ 位置校验否决。

    构造下缘更陡（P4 明显高于 P2）使 apex 提前、progress>0.75：把 P4 从 10.9 抬到
    11.4（下缘急升），apex 大幅提前，P5 突破点落在 apex 附近 → progress>0.75。
    """
    close, high, low, vol = _build_standard_triangle()
    close = close.copy()
    # P4@idx15-17 原 10.9 → 抬到 11.4（下缘急升，apex 提前）
    close.iloc[15:18] = [11.65, 11.55, 11.4]
    high = close + 0.3
    low = close - 0.3
    cfg = _mk_cfg()
    atr = _atr_const(len(close))
    piv = causal_pivots(close, atr, cfg)
    assert _last5_pivots(piv) == [1, -1, 1, -1, 1], \
        f"尾部 5 pivot 顺序错误：{_last5_pivots(piv)}"

    res = detect(close, piv, high, low, vol, cfg)
    # 前置：若识别成功，进度应 >0.75（证明确实是"太晚"场景）
    if res is not None and res.is_valid:
        assert res.breakout_progress > cfg.triangle_breakout_max, \
            f"本用例应触发进度>0.75否决，但进度={res.breakout_progress}"
    # 突破太晚 → 位置校验否决
    assert res is None or not res.is_valid, \
        f"突破进度>3/4（接近顶点易假突破）应被位置校验否决：{res}"


def test_breakout_too_early_rejected():
    """【原著硬规则★】突破进度 <1/2（形态未成熟）→ 位置校验否决。

    构造上下缘近乎平行（收敛极慢）使 apex 极远、progress<0.5：把 P3 抬到接近 P1、
    P4 压到接近 P2，两缘接近平行，apex 跑到极远处，P5 突破点 progress<0.5。
    """
    close, high, low, vol = _build_standard_triangle()
    close = close.copy()
    # P3@14 抬到 12.4（接近 P1=12.5，上缘近水平）；P4@17 压到 10.85（接近 P2=10.8）
    close.iloc[12:15] = [11.3, 11.9, 12.4]
    close.iloc[15:18] = [11.55, 11.2, 10.85]
    high = close + 0.3
    low = close - 0.3
    cfg = _mk_cfg()
    atr = _atr_const(len(close))
    piv = causal_pivots(close, atr, cfg)
    assert _last5_pivots(piv) == [1, -1, 1, -1, 1], \
        f"尾部 5 pivot 顺序错误：{_last5_pivots(piv)}"

    res = detect(close, piv, high, low, vol, cfg)
    # 两缘接近平行/apex 极远 → progress<0.5 或平行无交点 → 否决
    assert res is None or not res.is_valid, \
        f"突破进度<1/2（未成熟）或两缘平行应被位置校验否决：{res}"


# ---------------------------------------------------------------------------
# 用例 5：突破未放量否决 + 对照
# ---------------------------------------------------------------------------
def test_no_volume_breakout_rejected():
    """P5 突破日未放量（< baseline×multiplier）→ 量价校验否决。"""
    close, high, low, _ = _build_standard_triangle()
    n = len(close)
    # 全段 baseline=200，P5@20 也仅 200（未放量，<200×1.5=300）
    vol = pd.Series(200.0, index=pd.RangeIndex(n), dtype=float)
    cfg = _mk_cfg()
    atr = _atr_const(len(close))
    piv = causal_pivots(close, atr, cfg)
    assert _last5_pivots(piv) == [1, -1, 1, -1, 1], \
        f"尾部 5 pivot 顺序错误：{_last5_pivots(piv)}"

    res = detect(close, piv, high, low, vol, cfg)
    assert res is None or not res.is_valid, \
        f"突破未放量应被量价校验否决：{res}"


def test_volume_breakout_control_passes():
    """【对照】P5 突破日放量（≥baseline×multiplier）→ 通过（即 standard 序列）。"""
    close, high, low, vol = _build_standard_triangle()   # P5@20=500 放量
    cfg = _mk_cfg()
    atr = _atr_const(len(close))
    piv = causal_pivots(close, atr, cfg)
    res = detect(close, piv, high, low, vol, cfg)
    assert res is not None and res.is_valid, \
        f"突破放量应通过（证明无放量否决唯一来自量价校验）：{res}"


# ---------------------------------------------------------------------------
# 用例 6：跨度不足否决 + 对照
# ---------------------------------------------------------------------------
def test_short_span_rejected():
    """跨度 < min_pattern_bars → 跨度校验否决。

    构造紧凑三角形（每段 1 根），尾部 5 pivot 仍是 峰-谷-峰-谷-峰，但 span<11。
    """
    # 紧凑序列：背景2根 + P1-P5各1根 + 回踩2根 = 9根，span=P5_i-P1_i 较小
    close = pd.Series(
        [15.0, 10.0,        # idx0-1 背景跌到 P0谷=10
         12.5,               # idx2 P1峰
         10.8,               # idx3 P2谷
         12.0,               # idx4 P3峰
         10.9,               # idx5 P4谷
         13.0,               # idx6 P5峰（突破）
         12.0, 11.5],        # idx7-8 回踩确认 P5（使 P5@6 距末尾 ≥ confirm_bars 保留）
        dtype=float,
    )
    high = close + 0.3
    low = close - 0.3
    vol = _vol_pattern(len(close), p5_i=6)
    cfg = _mk_cfg(min_pattern_bars=11)
    atr = _atr_const(len(close))
    piv = causal_pivots(close, atr, cfg)
    # 前置：尾部 5 pivot 顺序正确
    assert _last5_pivots(piv) == [1, -1, 1, -1, 1], \
        f"尾部 5 pivot 顺序错误：{_last5_pivots(piv)}"
    # 前置：跨度确实 < min_pattern_bars
    nz = [i for i in range(len(piv)) if piv.iloc[i] != 0]
    span = nz[-1] - nz[-5]
    assert span < cfg.min_pattern_bars, f"跨度 {span} 应 < min {cfg.min_pattern_bars}"

    res = detect(close, piv, high, low, vol, cfg)
    assert res is None or not res.is_valid, \
        f"短跨度三角形应被跨度校验否决（span={span} < {cfg.min_pattern_bars}）"
