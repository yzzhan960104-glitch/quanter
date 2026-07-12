# -*- coding: utf-8 -*-
"""收敛三角形底部识别（蔡森招12 · 多头买进讯号 · 白皮书权威）。

物理意图与原著对齐（多空转折策略白皮书 §三·招12）：
  收敛三角形底部 = 上缘（峰）逐次递减 + 下缘（谷）逐次抬高 的收敛结构，
  股价带量向上突破上缘颈线，即多头买进讯号。形态由因果 ZigZag pivot 提取
  的五点构成：
      P1（首峰/上缘左端）→ P2（首谷/下缘左端）→ P3（次峰/上缘右端）
      → P4（次谷/下缘右端）→ P5（突破峰/突破确认）
  收盘突破 P1-P3 上缘颈线即形态确认。

  与 W 底（双底）/ 头肩底（三底）互补：收敛三角形抓的是「波动收敛后的方向
  选择」，本质是多空力量逐步均衡后的蓄势突破，而非底部反转。

白皮书原著硬规则（逐条实现，每步硬否决）：
  1. 收敛结构：P3 < P1（上缘递减）且 P4 > P2（下缘递增）——否则是扩张/平行，
     非收敛三角形，直接否决；
  2. 有效突破位置（★全书唯一带位置硬规则的形态★）：突破点必须落在三角形
     时间跨度的 1/2 ~ 3/4 处。太早（<1/2）=形态未成熟；太晚（>3/4）=接近
     顶点易出现假突破/反向走势（原著明确）。突破进度 = (突破idx−P1_idx) /
     (上下缘交点idx−P1_idx)；
  3. 满足点 = 突破上缘颈线价 + 三角形垂直边长（边长 = 首个完整波段高度
     = P1−P2）。注意：边长 ≠ 颈线−谷底（因突破价 < P1），故 plan.py 经
     pattern_height 字段单独消费边长，与止损（用真实谷底 min(P2,P4)）分离。

防御性过滤（经典形态学，非原著逐字，作为可选风控保留）：
  - 跨度过滤、幅度过滤、幅宽张力、突破放量、（可选）26 周线打底环境。
  - ma26w_filter 默认在 StrategyConfig 为 True，但合成短序列测试经 _mk_cfg
    关闭；生产环境样本足量时开启，过滤「未站上长期均线」的弱势三角形。

风控边界（CLAUDE.md 量化风控拷问）：
  - 上下缘近乎平行时交点跑到无穷远 → progress→0 被否决（合法：未真收敛）；
  - 交点落在突破点之前（下缘已穿上缘）→ 非收敛，否决；
  - 只消费 causal_pivots 的因果 pivot（已隔离末尾 confirm_bars），P5 取尾部
    已确认 pivot，无前视；末根 close.iloc[-1] 仅作突破兜底确认（合法：T 日
    收盘看 T 及之前）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd

from caisen.config import StrategyConfig
from caisen.patterns import neckline


@dataclass
class TriangleBottom:
    """收敛三角形底部识别结果。

    字段物理意图：
        p1_idx/p1_price ~ p5_idx/p5_price：五点下标与收盘价
            （P1=首峰, P2=首谷, P3=次峰, P4=次谷, P5=突破峰）；
        neckline_price：上缘颈线（P1-P3 两点连线）在【突破点】处的投影价
            （突破点=P5_i 若 P5 突破，否则末根 close.iloc[-1] 兜底确认）；
        bottom_price：真实最低谷 = min(P2, P4)，供下游 plan.py 计算止损用
            （与边长 edge_height 分离——止损用真实谷底，满足用边长）；
        edge_height：三角形垂直边长 = P1 − P2（首个完整波段高度），供 plan.py
            经 pattern_height 字段计算满足点（满足_n = neckline + n×edge_height）；
        apex_idx：上下缘交点位置（浮点 idx，仅供审计/可视化，不参与交易决策）；
        breakout_progress：突破进度 = (突破idx−P1_idx)/(apex_idx−P1_idx)，
            原著要求 ∈ [1/2, 3/4]，审计字段；
        depth：边长比 = (P1−P2)/P2，形态垂直幅度（用于多形态择优排序）；
        tension：幅宽张力 = edge_height/span，高度/宽度比例；
        is_valid：综合判定是否有效；
        reason：判定原因（中文，便于审计与日志）。
    """
    p1_idx: int
    p2_idx: int
    p3_idx: int
    p4_idx: int
    p5_idx: int
    p1_price: float
    p2_price: float
    p3_price: float
    p4_price: float
    p5_price: float
    neckline_price: float
    bottom_price: float
    edge_height: float
    apex_idx: float
    breakout_progress: float
    depth: float
    tension: float
    is_valid: bool
    reason: str = ""


def _ma26w_passes(close: pd.Series, p4_idx: int, cfg: StrategyConfig) -> bool:
    """26 周均线打底环境过滤（经典形态学防御性过滤，非原著逐字）。

    物理意图：长期均线之上完成的形态更可靠（底部有长期成本支撑）。要求次谷
    P4（三角形最后一个谷，最贴近当前基底）的 close ≥ close 的 ma26w_window
    (默认 130 日) 均线。

    兜底规则：样本不足（len(close) < ma26w_window）时无法可靠计算 26 周线，
    保守放行（不阻断识别），避免新上市标的被过度过滤。

    返回 True = 通过（在 26 周线之上或样本不足兜底放行）；False = 否决。
    """
    n = len(close)
    if n < cfg.ma26w_window:
        return True   # 样本不足兜底放行
    ma26w = close.rolling(cfg.ma26w_window, min_periods=cfg.ma26w_window).mean()
    ma26w_at = ma26w.iloc[p4_idx]
    if pd.isna(ma26w_at):
        return True   # NaN 防御（p4_idx 落在序列前段窗口未满）→ 兜底放行
    return float(close.iloc[p4_idx]) >= float(ma26w_at)


def detect(
    close: pd.Series,
    pivots: pd.Series,
    high: pd.Series,
    low: pd.Series,
    volume: pd.Series,
    cfg: StrategyConfig,
) -> Optional[TriangleBottom]:
    """从因果 pivot 序列尾部找最近的 [P1..P5] 收敛三角形底部结构。

    参数：
        close:   收盘价序列（含 26 周线过滤所需历史，样本不足时兜底放行）；
        pivots:  causal_pivots 返回的因果 pivot 标记（1=峰, -1=谷, 0=非）；
        high/low: 高低价序列（保留接口以备未来 K 线实体突破判定扩展）；
        volume:  成交量序列（量价配合判定）；
        cfg:     StrategyConfig 全参数模型。

    返回：
        TriangleBottom（is_valid=True 为有效收敛三角形底部）或 None（未识别/被否决）。

    判定流程（每步硬否决，任一失败返回 None）：
        1. 尾部取最后 5 个 pivot，必须构成 峰-谷-峰-谷-峰 顺序（P1..P5）；
        2. 【原著硬规则】收敛结构：P3 < P1（上缘递减）且 P4 > P2（下缘递增）；
        3. 跨度过滤；
        4. 幅度过滤（边长比 depth）；
        5. 幅宽张力；
        6. 量价：P5 突破日量 ≥ 颈线段(P1→P4)均量 × breakout_vol_multiplier；
        7. 上缘突破：P5 > 上缘线投影 或 末根 close 兜底；
        8. 【原著硬规则★】突破位置：进度 ∈ [1/2, 3/4]；
        9. 26 周均线过滤（ma26w_filter=True 时）。
    """
    # 性能优化（回测跑通批次）：pandas .iloc 逐元素是 profile 暴露的瓶颈，改 numpy。
    pv = pivots.values
    cl = close.values
    vl = volume.values
    idxs = (pv != 0).nonzero()[0].tolist()
    if len(idxs) < 5:
        return None   # pivot 不足 5 个，无法构成三角形五点

    # 从尾部取最后 5 个 pivot，要求顺序为 峰(1)-谷(-1)-峰(1)-谷(-1)-峰(1)
    # 即 P1=首峰(上缘左端), P2=首谷(下缘左端), P3=次峰(上缘右端),
    #    P4=次谷(下缘右端), P5=突破峰(突破确认)
    p5_i, p4_i, p3_i, p2_i, p1_i = (
        idxs[-1], idxs[-2], idxs[-3], idxs[-4], idxs[-5]
    )
    if not (pv[p1_i] == 1 and pv[p2_i] == -1 and pv[p3_i] == 1 and pv[p4_i] == -1 and pv[p5_i] == 1):
        return None   # 末尾五点不构成 峰-谷-峰-谷-峰，非收敛三角形结构

    # —— 1b. P1 必须是从前期谷底 P0 反弹的峰（排除下跌起点的"伪三角形"）——
    # 收敛三角形是「下跌到 P0 谷 → 反弹到 P1 峰 → 收敛 P1-P5 → 突破上缘」的结构。
    # 若 P1 前无谷（P1 是下跌起点/全局最高），则 P1-P2 是下跌段而非三角形第一波——
    # 这是 W 底/头肩底的下跌背景，不应判为收敛三角形。故要求至少 6 个 pivot
    # （P0 谷 + P1..P5），且 idxs[-6] 为谷（P0，P1 的反弹起点）。
    # 关键作用：排除 W 底序列（尾部 5 pivot 恰好峰-谷-峰-谷-峰且 P1 为下跌起点）
    # 被误判为三角形，避免与 W 底形态重叠抢命中（两者目标价语义不同）。
    if len(idxs) < 6:
        return None
    p0_i = idxs[-6]
    if pv[p0_i] != -1:
        return None   # P1 前一个 pivot 非谷 → P1 不是反弹峰 → 非收敛三角形

    p1 = float(cl[p1_i])
    p2 = float(cl[p2_i])
    p3 = float(cl[p3_i])
    p4 = float(cl[p4_i])
    p5 = float(cl[p5_i])
    span = p5_i - p1_i   # 形态跨度用 P1..P5（突破峰为形态完成点）

    # —— 2. 【原著硬规则】收敛结构：上缘递减 + 下缘递增 ——
    # 上缘 = P1-P3 连线（峰递减：P3 < P1）；下缘 = P2-P4 连线（谷递增：P4 > P2）。
    # 若 P3≥P1（上缘不降）或 P4≤P2（下缘不升），则为扩张/平行三角形，非收敛结构，
    # 原著收敛三角形底部不成立，直接否决。
    if not (p3 < p1 and p4 > p2):
        return None

    # —— 3. 跨度过滤：蔡森原著至少 11 根才具结构意义 ——
    if not (cfg.min_pattern_bars < span <= cfg.max_pattern_bars):
        return None

    # —— 4. 幅度过滤：边长比 depth ∈ (min_depth, max_depth] ——
    # 边长 = 首个完整波段高度 P1−P2（三角形最大垂直开度）。depth = 边长/首谷价。
    # screener 调用时经 model_copy 把 max_pattern_depth 覆写为 triangle_max_pattern_depth。
    edge_height = p1 - p2
    if edge_height <= 0:
        return None   # 首峰不高于首谷，结构异常
    depth = edge_height / p2
    if not (cfg.min_pattern_depth < depth <= cfg.max_pattern_depth):
        return None

    # —— 5. 幅宽张力：高度/宽度 ≥ tension_ratio ——
    tension = edge_height / span if span > 0 else 0.0
    if tension < cfg.pattern_tension_ratio:
        return None

    # —— 6. 量价配合：P5 突破日放量 ——
    # 突破日（P5）成交量 ≥ 颈线段（P1→P4，整个三角形主体）平均成交量 × 倍数。
    # 物理语义：颈线段均量代表「三角形蓄势期 baseline 量能」，突破日放量相对该
    # baseline 才有「资金增量进场选择方向」的意义（类推头肩底 P3-P5 颈线段 baseline）。
    seg = vl[p1_i : p4_i + 1]
    breakout_baseline = float(seg.mean()) if len(seg) > 0 else 0.0
    vol_p5 = float(vl[p5_i]) if p5_i < len(vl) else 0.0
    if breakout_baseline > 0 and vol_p5 < breakout_baseline * cfg.breakout_vol_multiplier:
        return None   # 突破未放量，方向选择可靠性差

    # —— 7. 上缘突破：P5 > 上缘投影 或 末根 close 兜底 ——
    # 上缘 = P1-P3 两点连线。突破点优先取 P5（已确认的突破峰 pivot）；若 P5 未突破
    # 上缘，用末根 close.iloc[-1] 兜底（当前价正在突破，P5 pivot 可能略早于突破日）。
    neck_at_p5 = neckline.fit_line([(p1_i, p1), (p3_i, p3)], at=p5_i)
    if p5 > neck_at_p5:
        breakout_idx = p5_i
        neck_at_break = neck_at_p5
    else:
        # 末根兜底：当前收盘突破上缘也算确认（合法：T 日收盘看 T 及之前，无前视）
        end_i = len(close) - 1
        neck_at_end = neckline.fit_line([(p1_i, p1), (p3_i, p3)], at=end_i)
        if not (float(close.iloc[-1]) > neck_at_end):
            return None   # P5 与末根均未突破上缘，形态未确认
        breakout_idx = end_i
        neck_at_break = neck_at_end

    # —— 8. 【原著硬规则★】突破位置：进度 ∈ [1/2, 3/4] ——
    # 计算上缘线(P1-P3)与下缘线(P2-P4)的交点 idx_apex（三角形理论收敛顶点）。
    # 上缘斜率 k_up = (p3-p1)/(p3_i-p1_i)（负，因 p3<p1）；
    # 下缘斜率 k_dn = (p4-p2)/(p4_i-p2_i)（正，因 p4>p2）。
    # 两线交点：k_up*(x-p1_i)+p1 = k_dn*(x-p2_i)+p2 → 解出 x = idx_apex。
    k_up = (p3 - p1) / (p3_i - p1_i)
    k_dn = (p4 - p2) / (p4_i - p2_i)
    denom = k_up - k_dn
    if abs(denom) < 1e-12:
        return None   # 上下缘近乎平行 → 无有限交点 → 未真收敛，否决
    idx_apex = (p2 - p1 - k_dn * p2_i + k_up * p1_i) / denom
    if idx_apex <= float(breakout_idx):
        # 交点落在突破点之前 = 下缘已自下穿上缘 = 非收敛（扩张或反向），否决。
        # 正常收敛三角形交点应在右侧未来（idx_apex > 所有 pivot）。
        return None
    breakout_progress = (breakout_idx - p1_i) / (idx_apex - p1_i)
    if not (cfg.triangle_breakout_min <= breakout_progress <= cfg.triangle_breakout_max):
        return None   # 突破太早(<1/2，未成熟)或太晚(>3/4，易假突破)，原著硬规则否决

    # —— 9. 26 周均线打底环境过滤（经典形态学防御性过滤，ma26w_filter=True 时）——
    if cfg.ma26w_filter and not _ma26w_passes(close, p4_i, cfg):
        return None   # 次谷在 26 周线之下，长期基底环境不成立

    return TriangleBottom(
        p1_idx=p1_i, p2_idx=p2_i, p3_idx=p3_i, p4_idx=p4_i, p5_idx=p5_i,
        p1_price=p1, p2_price=p2, p3_price=p3, p4_price=p4, p5_price=p5,
        neckline_price=neck_at_break,
        bottom_price=min(p2, p4),       # 真实最低谷，止损用
        edge_height=edge_height,        # 三角形边长 P1-P2，满足点用
        apex_idx=float(idx_apex),
        breakout_progress=float(breakout_progress),
        depth=depth,
        tension=tension,
        is_valid=True,
        reason=(
            f"收敛三角形底部（上缘降+下缘升+带量突破上缘+进度{breakout_progress:.2f}∈[1/2,3/4]）"
        ),
    )
