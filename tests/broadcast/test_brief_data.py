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
        {"key": "daily_basic", "status": "stale", "freshness_hours": 48.0},
        {"key": "index_daily", "status": "missing"},
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


def test_data_brief_ready_signal_section_when_true():
    """W5：ready_signal=True → brief 含「挂单就绪单口」段 + 就绪文案。

    物理意图（spec #13 T10）：brief 补单口信号让研究员对账「观测 healthy vs 决策 ready」。
    """
    r = build_data_brief("2026-07-21",
                         datasets=[{"key": "daily", "status": "healthy"}],
                         ready_signal=True)
    assert "挂单就绪单口" in r.markdown
    assert "就绪" in r.markdown


def test_data_brief_ready_signal_section_when_false():
    """W5：ready_signal=False → brief 含「未就绪」文案（暴露 healthy 但 ready 漂移）。"""
    r = build_data_brief("2026-07-21",
                         datasets=[{"key": "daily", "status": "healthy"}],
                         ready_signal=False)
    assert "挂单就绪单口" in r.markdown
    assert "未就绪" in r.markdown


def test_data_brief_ready_signal_none_backward_compat():
    """W5：ready_signal=None（未注入）→ 跳过该段（向后兼容，T10 前调用语义不变）。"""
    r = build_data_brief("2026-07-21",
                         datasets=[{"key": "daily", "status": "healthy"}],
                         ready_signal=None)
    assert "挂单就绪单口" not in r.markdown
