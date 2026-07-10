# -*- coding: utf-8 -*-
"""W 底识别（蔡森多头买进讯号 · 打底 ABC 波 + 幅宽张力 + 量价配合）。

物理意图与蔡森原著对齐（docs/caisen-methodology-summary.md §5/§6）：
  W 底（碗底翻）是蔡森核心多头买进讯号。形态由因果 ZigZag pivot 提取的四点构成：
      P1（左底/谷）→ P2（颈线高点/峰）→ P3（右底/谷）→ P4（突破高点/峰）
  价格收盘突破 P2-P4 颈线即形态确认。本模块的判定序列如下（每步均为硬否决，非软偏好）：

  1. 跨度过滤：(P4_idx - P1_idx) ∈ (min_pattern_bars, max_pattern_bars]
     —— 蔡森原著：至少 11 根才具结构意义；超过 120 日视为长趋势失效。
  2. 【Task 1 硬规则·原著直接 §6.1】右脚 ≥ 左脚（right_above_left=True 时）：
     P3 ≥ P1 × (1 - w_price_tolerance)。蔡森原著明确"右脚必须高于左脚"，
     右脚破左脚 = 下跌中继，直接否决。这比 plan 旧版的 |P3-P1|/P1 双向容忍度更严，
     符合蔡森原著对 W 底张力核心的定义（右脚抬高 → 多头底背离 → 张力更强）。
  3. 幅度过滤：颈线高度比 depth ∈ (min_pattern_depth, max_pattern_depth]
     —— depth = (P2 - min(P1,P3)) / min(P1,P3)；<3% 不构成可交易转折。
  4. 幅宽张力：tension = neck_h / span ≥ pattern_tension_ratio
     —— 蔡森原著：幅宽（两底时间间距）越宽，张力越强；扁平结构交易价值低。
  5. 量价配合：右底缩量 + 突破放量
     —— 蔡森核心理念"精準量價"：右底量 < 左底量 × right_vol_shrink（缩量打底），
     突破日量 ≥ 颈线段均量 × breakout_vol_multiplier（带量突破）。
  6. 颈线斜率 ≥ 0：P2→P4 颈线水平或上倾（下倾颈线 = 趋势仍在下行，形态可靠性差）。
  7. 【Task 1 原著直接 §5】26 周均线打底环境过滤（ma26w_filter=True 时）：
     蔡森原著"多头市场打底通常在 26 周平均线之上完成"。要求右底 P3 处的 close
     ≥ close 的 ma26w_window(默认 130 日) 均线。样本不足（len < ma26w_window）时
     保守放行（兜底，避免扼杀新标的）。

  注（ABC 波检测已移除）：原第 8 步「ABC 打底」要求 P3 接近 P1..P4 区间最低
     （p3 ≤ seg_min×1.005）。但右脚垫高规则要求 P3 ≥ P1，而 seg_min ≤ P1（P1 在
     区间内），故 seg_min ≤ P1 ≤ P3 → 标准右脚垫高 W 底必然 p3 > seg_min×1.005 被
     全部否决（自杀式逻辑）。W 底的打底由「右脚≥左脚」+ ZigZag 保证 P1/P3 为局部
     谷底共同覆盖，无需重复 ABC 校验；abc_wave_detect 配置项保留给头肩底（其头底
     P4 本就是区间最低，ABC 检测不自杀）。

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
class WBottom:
    """W 底识别结果。

    字段物理意图：
        p1_idx/p1_price ~ p4_idx/p4_price：四点下标与收盘价（P1=左底, P2=颈线高点,
            P3=右底, P4=突破高点）；
        neckline_price：颈线价（标准 W 底 = 中间峰 P2 的水平线，Bug2 校正）；
        bottom_price：谷底价 = min(P1, P3)（直接由形态给出，供下游 plan 使用，Bug3）；
        depth：颈线高度比 = (P2 - min(P1,P3)) / min(P1,P3)，形态垂直幅度；
        tension：幅宽张力 = neck_h / span，高度/宽度比例；
        is_valid：综合判定是否有效；
        reason：判定原因（中文，便于审计与日志）。
    """
    p1_idx: int
    p2_idx: int
    p3_idx: int
    p4_idx: int
    p1_price: float
    p2_price: float
    p3_price: float
    p4_price: float
    neckline_price: float
    bottom_price: float
    depth: float
    tension: float
    is_valid: bool
    reason: str = ""


def _ma26w_passes(close: pd.Series, p3_idx: int, cfg: StrategyConfig) -> bool:
    """26 周均线打底环境过滤（蔡森原著 §5）。

    物理意图：蔡森原著"多头市场打底通常在 26 周平均线之上完成"。
    要求右底 P3 处的 close ≥ close 的 ma26w_window(默认130日) 均线。

    兜底规则：样本不足（len(close) < ma26w_window）时无法可靠计算 26 周线，
    保守放行（不阻断识别），避免新上市标的被过度过滤。

    返回 True = 通过（在 26 周线之上或样本不足兜底放行）；False = 否决。
    """
    n = len(close)
    if n < cfg.ma26w_window:
        # 样本不足兜底：无法计算 26 周线，保守放行（蔡森原著无明确兜底规则）
        return True
    ma26w = close.rolling(cfg.ma26w_window, min_periods=cfg.ma26w_window).mean()
    ma26w_at_p3 = ma26w.iloc[p3_idx]
    close_at_p3 = close.iloc[p3_idx]
    # NaN 防御：rolling 在窗口不满时返回 NaN，理论上 n>=window 时 p3_idx 处应为有效值，
    # 但 p3_idx < ma26w_window-1 时（右底落在序列前段）也会 NaN → 兜底放行
    if pd.isna(ma26w_at_p3):
        return True
    return close_at_p3 >= ma26w_at_p3


def detect(
    close: pd.Series,
    pivots: pd.Series,
    high: pd.Series,
    low: pd.Series,
    volume: pd.Series,
    cfg: StrategyConfig,
) -> Optional[WBottom]:
    """从因果 pivot 序列尾部找最近的 [P1,P2,P3,P4] W 底结构。

    参数：
        close:   收盘价序列（含 26 周线过滤所需的历史长度，样本不足时兜底放行）；
        pivots:  causal_pivots 返回的因果 pivot 标记（1=峰, -1=谷, 0=非）；
        high/low: 高低价序列（保留接口以备未来 K 线实体突破判定扩展）；
        volume:  成交量序列（量价配合判定）；
        cfg:     StrategyConfig 全参数模型。

    返回：
        WBottom（is_valid=True 为有效 W 底）或 None（未识别/被否决）。

    判定流程（每步硬否决，任一失败返回 None）：
        1. 尾部取最后 4 个 pivot，必须构成 谷-峰-谷-峰 顺序；
        2. 跨度过滤；
        3. 右脚≥左脚（right_above_left=True 时）；
        4. 幅度过滤；
        5. 幅宽张力；
        6. 量价：右底缩量 + 突破放量；
        7. 颈线斜率 ≥ 0；
        8. 26 周均线过滤（ma26w_filter=True 时）。
        （W 底不做 ABC 打底校验——见模块 docstring 注，原 ABC 步骤为自杀式逻辑已移除。）
    """
    # 提取所有 pivot 下标（值非 0 的位置）
    idxs = [i for i in range(len(pivots)) if pivots.iloc[i] != 0]
    if len(idxs) < 4:
        return None   # pivot 不足，无法构成 W 底四点

    # 从尾部取最后 4 个 pivot，要求顺序为 谷(-1)-峰(1)-谷(-1)-峰(1)
    p4_i, p3_i, p2_i, p1_i = idxs[-1], idxs[-2], idxs[-3], idxs[-4]
    if not (
        pivots.iloc[p4_i] == 1
        and pivots.iloc[p3_i] == -1
        and pivots.iloc[p2_i] == 1
        and pivots.iloc[p1_i] == -1
    ):
        return None   # 末尾四点不构成 谷-峰-谷-峰，非 W 底结构

    p1 = float(close.iloc[p1_i])
    p2 = float(close.iloc[p2_i])
    p3 = float(close.iloc[p3_i])
    p4 = float(close.iloc[p4_i])
    span = p4_i - p1_i

    # —— 1. 跨度过滤：蔡森原著至少 11 根才具结构意义 ——
    if not (cfg.min_pattern_bars < span <= cfg.max_pattern_bars):
        return None

    # —— 2. 右脚 ≥ 左脚（Task 1 硬规则，原著 §6.1）——
    # right_above_left=True 时强制 P3 ≥ P1×(1-tolerance)；右脚破左脚 = 下跌中继。
    # 关闭时退化为 |P3-P1|/P1 ≤ tolerance 的双向容忍度（plan 原版语义）。
    if cfg.right_above_left:
        # 右脚下限：不能明显破左脚；右脚抬高（P3>P1）更好，张力更强
        if p3 < p1 * (1 - cfg.w_price_tolerance):
            return None
    else:
        # 关闭硬规则时用双向容忍度（兼容更宽松的方法学）
        if abs(p3 - p1) / p1 > cfg.w_price_tolerance:
            return None

    # —— 3. 幅度过滤：颈线高度比 ∈ (min_depth, max_depth] ——
    bottom = min(p1, p3)
    neck_h = p2 - bottom
    if neck_h <= 0:
        return None   # 颈线高点低于两底，结构异常
    depth = neck_h / bottom
    if not (cfg.min_pattern_depth < depth <= cfg.max_pattern_depth):
        return None

    # —— 4. 幅宽张力：高度/宽度 ≥ tension_ratio ——
    tension = neck_h / span if span > 0 else 0.0
    if tension < cfg.pattern_tension_ratio:
        return None

    # —— 5. 量价配合：右底缩量 + 突破放量 ——
    # 右底缩量：右底 P3 处成交量 ≤ 左底 P1 处成交量 × right_vol_shrink（缩量打底完成）
    vol_p1 = float(volume.iloc[p1_i]) if p1_i < len(volume) else 0.0
    vol_p3 = float(volume.iloc[p3_i]) if p3_i < len(volume) else 0.0
    if vol_p1 > 0 and vol_p3 > vol_p1 * cfg.right_vol_shrink:
        return None   # 右底未缩量，打底未完成
    # 突破放量：P3→P4 上涨段【最高单日成交量】≥ 颈线段均量 × breakout_vol_multiplier
    # 【Bug5】旧实现只校验 P4 单根量，但实盘价到 P4 见顶时常已缩量，真正放量发生在
    # 突破 P2 的那天（P3→P4 段内）。改用段内最大量——突破段某日放量即认可。
    breakout_baseline = (
        float(volume.iloc[p2_i:p3_i].mean())
        if (p3_i - p2_i) > 0
        else float(volume.iloc[p2_i])
    )
    breakout_max_vol = (
        float(volume.iloc[p3_i : p4_i + 1].max()) if p4_i >= p3_i else 0.0
    )
    if breakout_baseline > 0 and breakout_max_vol < breakout_baseline * cfg.breakout_vol_multiplier:
        return None   # 突破段全段未放量，形态可靠性差

    # —— 6. 颈线斜率 ≥ 0：水平或上倾 ——
    # P2→P4 颈线斜率 < 0 表示颈线下倾，趋势仍在下行，形态可靠性低
    if neckline.slope((p2_i, p2), (p4_i, p4)) < 0:
        return None

    # —— 7. 26 周均线打底环境过滤（Task 1 校准，原著 §5）——
    if cfg.ma26w_filter and not _ma26w_passes(close, p3_i, cfg):
        return None   # 右底在 26 周线之下，多头基底环境不成立

    # 颈线价：标准 W 底颈线 = 中间峰 P2 的水平线。
    # 【Bug2】旧实现用两点 (p2,p4) 拟合直线在 p4 处求值，过 (p4_i,p4) 点 → 必 = p4，
    # 使颈线=突破价、阻力线跟着价格跑，H/止盈/盈亏比全部失效。标准 W 底颈线即 P2 水平线。
    neck_at_break = p2

    return WBottom(
        p1_idx=p1_i, p2_idx=p2_i, p3_idx=p3_i, p4_idx=p4_i,
        p1_price=p1, p2_price=p2, p3_price=p3, p4_price=p4,
        neckline_price=neck_at_break,
        bottom_price=bottom,   # 【Bug3】min(p1,p3)，直接传谷底价，废除 plan 逆推
        depth=depth,
        tension=tension,
        is_valid=True,
        reason="W底（右脚≥左脚 + 颈线突破 + 量价配合）",
    )
