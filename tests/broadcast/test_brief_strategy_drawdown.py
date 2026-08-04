# -*- coding: utf-8 -*-
"""策略播报回撤口径护栏（2026-08-05：legacy 累计 rr 口径被当净值百分比渲染 -41262%）。"""
from broadcast.brief_strategy import _fmt_dd


def test_fmt_dd_out_of_range_shows_dash():
    """legacy 累计 rr 口径（如 -412.62）不得被当净值百分比渲染。"""
    assert _fmt_dd(-412.62) == "—"


def test_fmt_dd_valid_negative():
    """净值百分比口径正常渲染（P1-1 起 max_drawdown ∈ [-1, 0]）。"""
    assert _fmt_dd(-0.1419) == "-14.2%"


def test_fmt_dd_positive_invalid_shows_dash():
    """正值不是回撤，显示「—」而非正百分比。"""
    assert _fmt_dd(0.5) == "—"
