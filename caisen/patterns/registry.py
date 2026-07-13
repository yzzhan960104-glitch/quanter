# -*- coding: utf-8 -*-
"""蔡森形态注册表（方案B·显式注册表）。

物理定位（CLAUDE.md 极简 + 显式至上 + 拒绝黑盒）：
    把 screener 原硬编码的「enable 开关 / depth 覆写 / 额外输出字段」三类形态差异
    收敛为声明式数据（PatternMeta），screener 用统一遍历逻辑处理所有形态。
    新形态扩展（B2：破底翻/破头锅等）只在本文件 PATTERNS 加一行，screener 零改。

为何不用装饰器 + importlib 自动扫描（方案A）：
    自动扫描是「魔法」——形态清单不直观、调试时来源难追，违背「显式至上、拒绝黑盒」。
    显式 list 的成本（加形态改 2 行）本身是合理的显式工程动作，且形态清单一目了然。

PatternMeta 字段物理意图：
    name:                 pattern_type 标识，与 candidate.pattern_type / plan.py 消费一致；
    detect:               detect(close, pivots, high, low, volume, cfg) -> Result | None；
    enable_field:         cfg 开关字段名（None=总启用；如 "enable_triangle_bottom"）；
    depth_override_field: cfg 深度覆写字段名（None=用 cfg.max_pattern_depth；
                          如 "hs_max_pattern_depth"——头部/边长幅度天然深于 W底颈线高度比，
                          需分类型宽阈值，screener model_copy 替换 max_pattern_depth）；
    extra_output:         candidate 额外字段名 -> Result 属性名
                          （如 triangle: {"pattern_height": "edge_height"}，供 plan.py 满足点用）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from caisen.patterns.w_bottom import detect as w_detect
from caisen.patterns.head_shoulder import detect as hs_detect
from caisen.patterns.triangle_bottom import detect as tri_detect


@dataclass(frozen=True)
class PatternMeta:
    """形态注册元信息：声明 screener 如何调用本形态的 detect（不可变值对象）。"""

    name: str
    detect: Callable
    enable_field: Optional[str] = None
    depth_override_field: Optional[str] = None
    extra_output: dict = field(default_factory=dict)


# 显式注册表：现有 3 形态。新形态（B2 破底翻等）在此追加一行即可，screener 零改。
# 未实现的 enable_pot_breakout/enable_bottom_flip/false_breakout_* 开关待对应形态
# 实现后再入此表（本轮只搬现有 3 形态，不含未实现形态）。
PATTERNS: list[PatternMeta] = [
    PatternMeta(name="w_bottom", detect=w_detect),
    PatternMeta(
        name="head_shoulder",
        detect=hs_detect,
        depth_override_field="hs_max_pattern_depth",
    ),
    PatternMeta(
        name="triangle_bottom",
        detect=tri_detect,
        enable_field="enable_triangle_bottom",
        depth_override_field="triangle_max_pattern_depth",
        extra_output={"pattern_height": "edge_height"},
    ),
]
