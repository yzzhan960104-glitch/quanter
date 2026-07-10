# -*- coding: utf-8 -*-
"""StrategyConfig 参数模型测试：默认值与边界校验。

覆盖 Task 1 精读校准后的参数清单：
- 时间跨度类（min_pattern_bars > 10 硬约束）
- 风控类（min_rr_ratio / max_position_pct / liquidity_min_amount）
- 蔡森方法学专用（neckline_height_multiple=2 等额累加满足级数 / right_above_left / ma26w_filter）
"""
import pytest
from pydantic import ValidationError

from caisen.config import StrategyConfig


def test_default_config_loads():
    """默认参数可构造，且关键风控阈值/方法学开关符合 spec。"""
    cfg = StrategyConfig()
    # —— 时间跨度 / 风控基准 ——
    assert cfg.min_pattern_bars == 11            # >10 硬约束（蔡森实战篇：形态跨度至少 11 根）
    assert cfg.min_rr_ratio == 1.5               # 兜底定标值（2026-07-11，待replay去重+全市场复算；旧3.0全拦标准W底）
    assert cfg.max_position_pct == 0.05          # 单标的占总资金 5% 上限
    # —— Task 1 精读校准：等额累加满足级数 + 右脚>左脚 + 26 周线打底 ——
    assert cfg.neckline_height_multiple == 2     # 默认看第一+第二波满足（颈线 + n×H 等额累加）
    assert cfg.right_above_left is True          # 右脚价 > 左脚价 硬规则（破左脚=否决）
    assert cfg.ma26w_filter is True              # 26 周均线打底环境过滤


def test_min_pattern_bars_below_11_rejected():
    """形态跨度 < 11 必须拒绝（spec 硬约束 >10 交易日）。"""
    with pytest.raises(ValidationError):
        StrategyConfig(min_pattern_bars=10)


def test_negative_threshold_rejected():
    """负阈值非法（回踩幅度不可能为负）。"""
    with pytest.raises(ValidationError):
        StrategyConfig(pullback_max_pct=-0.01)


def test_neckline_height_multiple_range():
    """颈线满足级数倍数仅允许 1..4（Task 1 校准：等额累加级数 n）。"""
    # 上界越界：>4 拒绝
    with pytest.raises(ValidationError):
        StrategyConfig(neckline_height_multiple=5)
    # 下界越界：<1 拒绝
    with pytest.raises(ValidationError):
        StrategyConfig(neckline_height_multiple=0)
