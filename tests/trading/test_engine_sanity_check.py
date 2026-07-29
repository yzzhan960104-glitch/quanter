# -*- coding: utf-8 -*-
"""M3 口径自检：_eod 落盘 key(next_trading_day) 与 _pre_open 读 key(today) 对齐。

背景（[[eod-date-offbyone-fix]]）：
    代码口径已修（_eod 落盘用 next_trading_day，_pre_open 读 today），但若进程跑的是
    未重启的旧代码，口径会退回「today 落盘 → 次日 today 读 T+1 永远差一天」的旧 bug，
    导致标的错位 + 永不挂单。TradingEngine 启动时主动校验 next_trading_day(today) != today，
    确认次日计算口径正常，否则视为口径坏（拒进 live，降级 dry_run）。
"""
import pytest
from datetime import datetime
from unittest.mock import patch


def test_sanity_check_passes_when_aligned(monkeypatch):
    """T 日盘后：next_trading_day(today) 算出次日 → 落盘 key 与次日 today 读取口径一致 → 通过。"""
    from trading.engine import TradingEngine
    eng = TradingEngine()
    # 模拟 today=T，next_trading_day 算出 T+1（次日，口径正常）
    monkeypatch.setattr("trading.engine.calendar.next_trading_day", lambda d: "2026-07-30")
    assert eng._sanity_check_date_alignment(today="2026-07-29") is True


def test_sanity_check_detects_offbyone(monkeypatch):
    """next_trading_day 返 today 自身（旧 bug 口径）→ 自检失败（拒进 live）。"""
    from trading.engine import TradingEngine
    eng = TradingEngine()
    # 旧 bug：next_trading_day(d) 返 d 自身，落盘 key=今日 → 次日读 today 永远差一天
    monkeypatch.setattr("trading.engine.calendar.next_trading_day", lambda d: d)
    assert eng._sanity_check_date_alignment(today="2026-07-29") is False
