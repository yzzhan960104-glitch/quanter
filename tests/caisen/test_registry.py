# -*- coding: utf-8 -*-
"""形态注册表契约测试：PATTERNS 内容 + PatternMeta 字段（方案B 显式注册表）。

物理意图：锁死注册表的声明式契约——screener 据此数据驱动遍历，故 PATTERNS 的
每一项字段（name/detect/enable_field/depth_override_field/extra_output）必须精确
对应 screener 的调用逻辑，任一字段漂移都会让对应形态被误启用/误覆写/漏输出。
"""
import dataclasses
import pytest

from caisen.patterns.registry import PATTERNS, PatternMeta
from caisen.patterns.w_bottom import detect as w_detect
from caisen.patterns.head_shoulder import detect as hs_detect
from caisen.patterns.triangle_bottom import detect as tri_detect


def test_patterns_contains_three_builtins():
    """PATTERNS 含且仅含现有 3 形态（未实现形态不入注册表，待 B2 实现 + 注册）。"""
    names = {m.name for m in PATTERNS}
    assert names == {"w_bottom", "head_shoulder", "triangle_bottom"}


def test_w_bottom_meta_defaults():
    """W 底：基线形态——无 enable 开关、无 depth 覆写、无额外输出。"""
    m = next(m for m in PATTERNS if m.name == "w_bottom")
    assert m.detect is w_detect
    assert m.enable_field is None
    assert m.depth_override_field is None
    assert m.extra_output == {}


def test_head_shoulder_meta_depth_override():
    """头肩底：depth 覆写 hs_max_pattern_depth（头部幅度天然更深，需宽阈值）。"""
    m = next(m for m in PATTERNS if m.name == "head_shoulder")
    assert m.detect is hs_detect
    assert m.enable_field is None              # 总启用（多头基础形态）
    assert m.depth_override_field == "hs_max_pattern_depth"
    assert m.extra_output == {}


def test_triangle_bottom_meta_full():
    """收敛三角形底：enable 开关 + depth 覆写 + 额外 pattern_height=edge_height。"""
    m = next(m for m in PATTERNS if m.name == "triangle_bottom")
    assert m.detect is tri_detect
    assert m.enable_field == "enable_triangle_bottom"
    assert m.depth_override_field == "triangle_max_pattern_depth"
    assert m.extra_output == {"pattern_height": "edge_height"}


def test_pattern_meta_is_frozen():
    """PatternMeta 不可变（防运行时误改注册项导致全市场扫描行为漂移）。"""
    assert dataclasses.is_dataclass(PatternMeta)
    m = PatternMeta(name="x", detect=lambda *a, **k: None)
    with pytest.raises(dataclasses.FrozenInstanceError):
        m.name = "y"
