# -*- coding: utf-8 -*-
"""A1 回归：pre_open 09:22 集合竞价窗口必须被放行（08-05 废单根因）。"""
from datetime import datetime

from presentation.server.services.trading_service import _in_a_share_session


def test_session_allows_call_auction_0915():
    """09:22（集合竞价 09:15-09:25 内）必须放行——pre_open 调度在此刻挂单。"""
    assert _in_a_share_session(datetime(2026, 8, 5, 9, 22)) is True


def test_session_blocks_before_0915():
    """09:10 仍拦截（隔夜保护不能破）。"""
    assert _in_a_share_session(datetime(2026, 8, 5, 9, 10)) is False


def test_session_allows_lunch_break():
    """D2 修订（选项 1）：午休 12:33 放行（柜台可接收排队单）。"""
    assert _in_a_share_session(datetime(2026, 8, 5, 12, 33)) is True


def test_session_blocks_weekend():
    """周末必须拦截（防周末误单）。"""
    assert _in_a_share_session(datetime(2026, 8, 8, 10, 0)) is False  # 周六
