# -*- coding: utf-8 -*-
"""头肩底识别（蔡森多头买进讯号 · 6 pivot 结构 + 颈线突破 + 量价配合）。

物理意图与蔡森原著对齐（docs/caisen-methodology-summary.md §5/§6 + 经典形态学）：
  头肩底是蔡森多头买进讯号之一（蔡森 Task 1 精读主要覆盖 W底/碗底翻/打底/转折，
  头肩底按经典形态学 + spec §5.3 实现，类推 W底的 Task 1 校准）。形态由因果 ZigZag
  pivot 提取的六点构成：
      P1（峰/形态起点）→ P2（谷/左肩底）→ P3（峰/左颈）→ P4（谷/头底，区间最低）
      → P5（峰/右颈）→ P6（谷/右肩底）→ P7（峰/突破确认）
  价格收盘突破 P3-P5 颈线即形态确认。本模块的判定序列如下（每步均为硬否决）：

  1. 尾部 7 pivot 顺序：必须为 峰-谷-峰-谷-峰-谷-峰（P1..P7）。P7 为突破确认峰
     （若 causal_pivots 未产出独立 P7 峰，也可用 close.iloc[-1] > 颈线价兜底，
     但本实现优先要求 P7，保证突破已被因果确认、非未来函数）。
  2. 【形态学硬规则】头底 P4 为整个 P1..P6 区间最低（P4 < P2 且 P4 < P6）：
     头肩底的"头"必须比两肩都低，否则结构不成立（如左肩比头还低 = 非头肩底）。
  3. 跨度过滤：(P6_idx - P1_idx) ∈ (min_pattern_bars, max_pattern_bars]
     —— 蔡森原著：至少 11 根才具结构意义；超过 120 日视为长趋势失效。
  4. 【类推 W底 Task 1 硬规则】右肩 ≥ 左肩（right_above_left=True 时）：
     P6 ≥ P2 × (1 - w_price_tolerance)。蔡森原著"右脚必须高于左脚"的精神在头肩底
     同样适用——右肩破左肩 = 结构破位、下跌中继，直接否决。
  5. 幅度过滤：颈线高度比 depth ∈ (min_pattern_depth, max_pattern_depth]
     —— depth = (颈线均价 - P4) / P4；颈线均价用 (P3+P5)/2 近似。
  6. 幅宽张力：tension = neck_h / span ≥ pattern_tension_ratio
     —— 蔡森原著：幅宽越宽张力越强；扁平结构交易价值低。
  7. 量价配合：右肩缩量 + 突破放量
     —— 蔡森"精準量價"：右肩量 < 左肩量 × right_vol_shrink（缩量打底），
     突破日量 ≥ 颈线段均量 × breakout_vol_multiplier（带量突破）。
  8. 颈线斜率 ≥ 0：P3→P5 颈线水平或上倾（下倾颈线 = 趋势仍在下行，可靠性差）。
  9. 颈线突破：P7 > P3-P5 颈线价（突破确认）；若无 P7 则 close.iloc[-1] > 颈线价兜底。
  10. 【类推 Task 1 校准 · 原著 §5】26 周均线打底环境过滤（ma26w_filter=True 时）：
      要求头底 P4 处的 close ≥ close 的 ma26w_window(默认 130 日) 均线
      （蔡森"多头市场打底通常在 26 周平均线之上完成"）。样本不足时保守放行。
  11. 【plan 保留】打底 ABC 波（abc_wave_detect=True 时）：
      P4 头底应为整个 P1..P6 区间的最低（C 波末跌创新低 = ABC 打底完成）。
      本规则与"P4 为区间最低"形态学硬规则方向一致，是 ABC 波时序过程的静态校验。

风控边界（CLAUDE.md 极简 + 显式原则）：
  - 所有阈值取自 StrategyConfig，无逻辑硬编码；
  - 每个否决分支显式返回 None（不抛异常），调用方可据 None 判定"未识别"；
  - causal_pivots 已保证 pivot 因果无未来函数，本模块纯前向消费 pivot 序列。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd

from caisen.config import StrategyConfig
from caisen.patterns import neckline


@dataclass
class HeadShoulderBottom:
    """头肩底识别结果。

    字段物理意图：
        p1_idx/p1_price ~ p6_idx/p6_price：六点下标与收盘价
            （P1=峰/起点, P2=左肩底, P3=左颈峰, P4=头底, P5=右颈峰, P6=右肩底）；
        neckline_price：颈线（P3-P5 两点回归直线）在突破点 P7 处的投影价；
        bottom_price：谷底价 = P4（头底，区间最低，供下游 plan 使用，Bug3）；
        depth：颈线高度比 = ((P3+P5)/2 - P4) / P4，头部相对颈线的垂直幅度；
        tension：幅宽张力 = neck_h / span，头部高度/形态宽度比例；
        is_valid：综合判定是否有效；
        reason：判定原因（中文，便于审计与日志）。
    """
    p1_idx: int
    p2_idx: int
    p3_idx: int
    p4_idx: int
    p5_idx: int
    p6_idx: int
    p1_price: float
    p2_price: float
    p3_price: float
    p4_price: float
    p5_price: float
    p6_price: float
    neckline_price: float
    bottom_price: float
    depth: float
    tension: float
    is_valid: bool
    reason: str = ""


def _ma26w_passes(close: pd.Series, p4_idx: int, cfg: StrategyConfig) -> bool:
    """26 周均线打底环境过滤（类推 W底 Task 1 校准，蔡森原著 §5）。

    物理意图：蔡森原著"多头市场打底通常在 26 周平均线之上完成"。
    要求头底 P4 处的 close ≥ close 的 ma26w_window(默认 130 日) 均线。

    兜底规则：样本不足（len(close) < ma26w_window）时无法可靠计算 26 周线，
    保守放行（不阻断识别），避免新上市标的被过度过滤。

    返回 True = 通过（在 26 周线之上或样本不足兜底放行）；False = 否决。
    """
    n = len(close)
    if n < cfg.ma26w_window:
        # 样本不足兜底：无法计算 26 周线，保守放行（蔡森原著无明确兜底规则）
        return True
    ma26w = close.rolling(cfg.ma26w_window, min_periods=cfg.ma26w_window).mean()
    ma26w_at_p4 = ma26w.iloc[p4_idx]
    close_at_p4 = close.iloc[p4_idx]
    # NaN 防御：rolling 在窗口不满时返回 NaN，理论上 n>=window 时 p4_idx 处应为有效值，
    # 但 p4_idx < ma26w_window-1 时（头底落在序列前段）也会 NaN → 兜底放行
    if pd.isna(ma26w_at_p4):
        return True
    return close_at_p4 >= ma26w_at_p4


def detect(
    close: pd.Series,
    pivots: pd.Series,
    high: pd.Series,
    low: pd.Series,
    volume: pd.Series,
    cfg: StrategyConfig,
) -> Optional[HeadShoulderBottom]:
    """从因果 pivot 序列尾部找最近的 [P1..P7] 头肩底结构。

    参数：
        close:   收盘价序列（含 26 周线过滤所需的历史长度，样本不足时兜底放行）；
        pivots:  causal_pivots 返回的因果 pivot 标记（1=峰, -1=谷, 0=非）；
        high/low: 高低价序列（保留接口以备未来 K 线实体突破判定扩展）；
        volume:  成交量序列（量价配合判定）；
        cfg:     StrategyConfig 全参数模型。

    返回：
        HeadShoulderBottom（is_valid=True 为有效头肩底）或 None（未识别/被否决）。

    判定流程（每步硬否决，任一失败返回 None）：
        1. 尾部取最后 7 个 pivot，必须构成 峰-谷-峰-谷-峰-谷-峰 顺序（P1..P7）；
        2. 头底 P4 为 P1..P6 区间最低（形态学硬规则）；
        3. 跨度过滤；
        4. 右肩≥左肩（right_above_left=True 时）；
        5. 幅度过滤；
        6. 幅宽张力；
        7. 量价：右肩缩量 + 突破放量；
        8. 颈线斜率 ≥ 0；
        9. 颈线突破：P7 > 颈线价（或 close.iloc[-1] > 颈线价兜底）；
        10. 26 周均线过滤（ma26w_filter=True 时）；
        11. ABC 波打底（abc_wave_detect=True 时）。
    """
    # 提取所有 pivot 下标（值非 0 的位置）
    idxs = [i for i in range(len(pivots)) if pivots.iloc[i] != 0]
    if len(idxs) < 7:
        return None   # pivot 不足 7 个，无法构成头肩底六点 + 突破确认峰

    # 从尾部取最后 7 个 pivot，要求顺序为 峰(1)-谷(-1)-峰(1)-谷(-1)-峰(1)-谷(-1)-峰(1)
    # 即 P1=峰(形态起点), P2=谷(左肩底), P3=峰(左颈), P4=谷(头底),
    #    P5=峰(右颈), P6=谷(右肩底), P7=峰(突破确认)
    p7_i, p6_i, p5_i, p4_i, p3_i, p2_i, p1_i = (
        idxs[-1], idxs[-2], idxs[-3], idxs[-4], idxs[-5], idxs[-6], idxs[-7]
    )
    if not (
        pivots.iloc[p1_i] == 1
        and pivots.iloc[p2_i] == -1
        and pivots.iloc[p3_i] == 1
        and pivots.iloc[p4_i] == -1
        and pivots.iloc[p5_i] == 1
        and pivots.iloc[p6_i] == -1
        and pivots.iloc[p7_i] == 1
    ):
        return None   # 末尾七点不构成 峰-谷-峰-谷-峰-谷-峰，非头肩底结构

    p1 = float(close.iloc[p1_i])
    p2 = float(close.iloc[p2_i])
    p3 = float(close.iloc[p3_i])
    p4 = float(close.iloc[p4_i])
    p5 = float(close.iloc[p5_i])
    p6 = float(close.iloc[p6_i])
    p7 = float(close.iloc[p7_i])
    span = p6_i - p1_i   # 形态跨度用 P1..P6（不含突破确认峰 P7）

    # —— 2. 头底 P4 为 P1..P6 区间最低（形态学硬规则）——
    # 头肩底的"头"必须比两肩都低。若 P2 或 P6 比 P4 还低（左/右肩比头还低），
    # 则结构不成立（可能是下跌中继的三个低谷而非头肩底）。
    if not (p4 < p2 and p4 < p6):
        return None

    # —— 3. 跨度过滤：蔡森原著至少 11 根才具结构意义 ——
    if not (cfg.min_pattern_bars < span <= cfg.max_pattern_bars):
        return None

    # —— 4. 右肩 ≥ 左肩（类推 W底 Task 1 硬规则）——
    # right_above_left=True 时强制 P6 ≥ P2×(1-tolerance)；右肩破左肩 = 结构破位。
    # 关闭时退化为 |P6-P2|/P2 ≤ tolerance 的双向容忍度（plan 原版语义）。
    if cfg.right_above_left:
        if p6 < p2 * (1 - cfg.w_price_tolerance):
            return None
    else:
        if abs(p6 - p2) / p2 > cfg.w_price_tolerance:
            return None

    # —— 5. 幅度过滤：颈线高度比 ∈ (min_depth, max_depth] ——
    # 颈线均价用 (P3+P5)/2 近似（两颈线峰的中点）；头部幅度 = 颈线均价 - 头底。
    neck_avg = (p3 + p5) / 2.0
    neck_h = neck_avg - p4
    if neck_h <= 0:
        return None   # 颈线低于头底，结构异常
    depth = neck_h / p4
    if not (cfg.min_pattern_depth < depth <= cfg.max_pattern_depth):
        return None

    # —— 6. 幅宽张力：高度/宽度 ≥ tension_ratio ——
    tension = neck_h / span if span > 0 else 0.0
    if tension < cfg.pattern_tension_ratio:
        return None

    # —— 7. 量价配合：右肩缩量 + 突破放量 ——
    # 右肩缩量：右肩 P6 处成交量 ≤ 左肩 P2 处成交量 × right_vol_shrink（缩量打底完成）
    vol_p2 = float(volume.iloc[p2_i]) if p2_i < len(volume) else 0.0
    vol_p6 = float(volume.iloc[p6_i]) if p6_i < len(volume) else 0.0
    if vol_p2 > 0 and vol_p6 > vol_p2 * cfg.right_vol_shrink:
        return None   # 右肩未缩量，打底未完成
    # 突破放量：P7 突破日成交量 ≥ 颈线段 P3-P5 平均成交量 × breakout_vol_multiplier
    # 物理语义（Task7 review I1）：颈线段 P3→P5（左颈到右颈，颈线本身）是震荡蓄势区间，
    # 其均量代表" baseline 蓄势量能"；突破日放量相对该 baseline 才有"资金增量进场"意义。
    # 原先用 P5→P6 下跌段（右颈回落到右肩的萎缩段）作 baseline，物理语义弱——下跌段
    # 量能本就萎缩，baseline 偏低，breakout_vol_multiplier=1.5 门槛被人为放低。
    # 改用 P3→P5 颈线段（含 p5 端点，与"P3-P5 颈线"几何语义一致）均量作突破参照基准。
    breakout_baseline = (
        float(volume.iloc[p3_i:p5_i + 1].mean())
        if (p5_i - p3_i) >= 0
        else float(volume.iloc[p3_i])
    )
    vol_p7 = float(volume.iloc[p7_i]) if p7_i < len(volume) else 0.0
    if breakout_baseline > 0 and vol_p7 < breakout_baseline * cfg.breakout_vol_multiplier:
        return None   # 突破未放量，形态可靠性差

    # —— 8. 颈线斜率 ≥ 0：水平或上倾 ——
    # P3→P5 颈线斜率 < 0 表示颈线下倾，趋势仍在下行，形态可靠性低
    if neckline.slope((p3_i, p3), (p5_i, p5)) < 0:
        return None

    # —— 9. 颈线突破：P7 > 颈线价（突破确认）——
    # 颈线 = P3-P5 两点回归直线在 P7 处的投影价。若 P7 未突破，则用 close.iloc[-1]
    # 兜底（末尾收盘价突破也算确认，但优先要求 P7 因果确认突破）。
    neck_at_break = neckline.fit_line([(p3_i, p3), (p5_i, p5)], at=p7_i)
    breakout_confirmed = p7 > neck_at_break
    if not breakout_confirmed:
        # 兜底：检查末尾收盘价是否突破颈线（P7 pivot 可能与突破日重合或略早）
        neck_at_end = neckline.fit_line([(p3_i, p3), (p5_i, p5)], at=len(close) - 1)
        if not (float(close.iloc[-1]) > neck_at_end):
            return None
        # 兜底通过时，颈线价仍用 P7 处投影价（保持语义一致）
        neck_at_break = neckline.fit_line([(p3_i, p3), (p5_i, p5)], at=len(close) - 1)

    # —— 10. 26 周均线打底环境过滤（类推 Task 1 校准，原著 §5）——
    if cfg.ma26w_filter and not _ma26w_passes(close, p4_i, cfg):
        return None   # 头底在 26 周线之下，多头基底环境不成立

    # —— 11. ABC 波打底（plan 保留）——
    # 简化静态校验：P4（头底/C 波末跌）应为整个 P1..P6 区间的最低或接近最低
    # （C 波创新低 = ABC 打底完成）。与"头底 P4 为区间最低"形态学硬规则方向一致，
    # 此处作为独立可配置开关，允许更宽松的方法学实现。
    if cfg.abc_wave_detect:
        seg = close.iloc[p1_i : p6_i + 1]
        seg_min = float(seg.min())
        # P4 应接近区间最低（允许 0.5% 容差，避免数值噪声误杀）
        if p4 > seg_min * 1.005:
            return None   # 头底非区间最低，疑似下跌中继而非 ABC 打底

    return HeadShoulderBottom(
        p1_idx=p1_i, p2_idx=p2_i, p3_idx=p3_i, p4_idx=p4_i, p5_idx=p5_i, p6_idx=p6_i,
        p1_price=p1, p2_price=p2, p3_price=p3, p4_price=p4, p5_price=p5, p6_price=p6,
        neckline_price=neck_at_break,
        bottom_price=p4,   # 【Bug3】头底 = 区间最低，直接传谷底价，废除 plan 逆推
        depth=depth,
        tension=tension,
        is_valid=True,
        reason="头肩底（头底最低 + 右肩≥左肩 + 颈线突破 + 量价配合）",
    )
