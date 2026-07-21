# -*- coding: utf-8 -*-
"""数据机器人 brief_data 单测（Task 4 · 一期观测运营层）。

覆盖两用例：
- test_data_brief_health_summary：混合状态（healthy/stale/missing）→ 健康分计数 + 异常清单
- test_data_brief_empty：空 datasets 列表 → 降级文案（「0」或「无数据集」语义）
"""
from broadcast.brief_data import build_data_brief


def test_data_brief_health_summary():
    """混合健康度样本：3 healthy / 1 stale / 1 missing，验证健康分计数 + 异常清单。"""
    r = build_data_brief("2026-07-21", datasets=[
        {"key": "daily", "status": "healthy", "freshness_hours": 2.0},
        {"key": "minute", "status": "stale", "freshness_hours": 48.0},
        {"key": "dragon_list", "status": "missing"},
        {"key": "ths_daily", "status": "healthy", "freshness_hours": 1.0},
    ])
    md = r.markdown
    # 健康分计数：样本含 2 healthy，文案应同时体现 healthy 字样与 healthy 数值
    assert "healthy" in md and "2" in md
    # 异常状态需在文案中如实展示（数据观测层的诚实底线：不掩饰坏数据集）
    assert "stale" in md and "missing" in md


def test_data_brief_empty():
    """空 datasets 列表：降级文案（健康分 0 或「无数据集」语义）。"""
    r = build_data_brief("2026-07-21", datasets=[])
    assert "无数据集" in r.markdown or "0" in r.markdown
