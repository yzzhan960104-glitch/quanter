# -*- coding: utf-8 -*-
"""策略机器人 brief 单测（Task 5 · 严格 TDD · Step 1 失败测试）。"""
from broadcast.brief_strategy import build_strategy_brief


def test_strategy_brief_basic():
    """有信号 + 有参数迭代 + 有回测 → 含「信号数 + 最优年化」关键字。"""
    r = build_strategy_brief(
        "2026-07-21",
        scan_count=3,
        param_iter_state={"best_annual": 0.997, "iter": 179},
        recent_runs=[{"run_id": "r1", "win_rate": 0.55, "max_drawdown": -0.12, "annualized_return": 0.30}],
    )
    md = r.markdown
    assert "3" in md and "99.7%" in md  # 信号数 + 最优年化


def test_strategy_brief_empty():
    """零信号 + 无参数迭代 + 无回测 → 中性降级，不抛。"""
    r = build_strategy_brief("2026-07-21", scan_count=0, param_iter_state=None, recent_runs=[])
    assert "0" in (md := r.markdown) or "无信号" in md
