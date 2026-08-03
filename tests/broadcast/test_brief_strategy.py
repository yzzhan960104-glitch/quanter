# -*- coding: utf-8 -*-
"""策略机器人 brief 单测（Task 5 · 严格 TDD · Step 1 失败测试）。"""
from datetime import datetime

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


def test_strategy_brief_run_without_hits_shows_no_trade():
    """n_hits=0 的回测无交易样本 → 显式「无成交」，不渲染无意义的 0.0%。"""
    r = build_strategy_brief(
        "2026-07-31",
        scan_count=1,
        param_iter_state={"best_annual": 1.1977, "iter": 168},
        recent_runs=[{
            "run_id": "20260714-094258-ea5b8f", "n_hits": 0,
            "win_rate": 0.0, "max_drawdown": 0.0, "annualized_return": 0.0,
        }],
    )
    md = r.markdown
    assert "20260714：无成交" in md
    assert "0.0%" not in md


def test_strategy_brief_drawdown_uses_rr_unit():
    """replay max_drawdown 是累计 rr 峰谷（风险倍数）→ 以 rr 为单位渲染，不当百分比。"""
    r = build_strategy_brief(
        "2026-07-31",
        scan_count=1,
        param_iter_state=None,
        recent_runs=[{
            "run_id": "20260712-230158-289322", "n_hits": 1,
            "win_rate": 0.0, "max_drawdown": -1.0, "annualized_return": -0.1087,
        }],
    )
    md = r.markdown
    assert "回撤 -1.00rr" in md
    assert "胜率 0.0%" in md
    assert "年化 -10.9%" in md


def test_strategy_brief_pending_run_marker():
    """回测任务已提交未完成 → 「回测进行中」置顶，不冒充已完成结果。"""
    r = build_strategy_brief(
        "2026-08-03",
        scan_count=1,
        param_iter_state=None,
        recent_runs=[
            {"run_id": "回测中", "pending": True,
             "created_at": "2026-08-03T10:00:00", "n_hits": -1},
        ],
        now=datetime(2026, 8, 3, 12, 0, 0),
    )
    md = r.markdown
    assert "回测进行中" in md
    assert "无成交" not in md


def test_strategy_brief_stale_note_shown_when_old():
    """最近完成回测距今 >7 天 → 显式标注「N 天前」，让滞后可见。"""
    r = build_strategy_brief(
        "2026-08-03",
        scan_count=1,
        param_iter_state=None,
        recent_runs=[{
            "run_id": "20260716-100000-xxxxxx", "created_at": "2026-07-16T10:00:00",
            "n_hits": 0, "win_rate": 0.0, "max_drawdown": 0.0, "annualized_return": 0.0,
        }],
        now=datetime(2026, 8, 3, 12, 0, 0),
    )
    md = r.markdown
    assert "最近回测已是 18 天前（2026-07-16）" in md


def test_strategy_brief_no_stale_note_when_fresh():
    """最近回测在 7 天内 → 不显示过期警告。"""
    r = build_strategy_brief(
        "2026-08-03",
        scan_count=1,
        param_iter_state=None,
        recent_runs=[{
            "run_id": "20260802-100000-xxxxxx", "created_at": "2026-08-02T10:00:00",
            "n_hits": 0, "win_rate": 0.0, "max_drawdown": 0.0, "annualized_return": 0.0,
        }],
        now=datetime(2026, 8, 3, 12, 0, 0),
    )
    assert "最近回测已是" not in r.markdown
