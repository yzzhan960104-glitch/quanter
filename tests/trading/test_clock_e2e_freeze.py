# -*- coding: utf-8 -*-
"""C-6 V4：e2e clock freeze——eod 落盘 key 与 pre_open 读 key 对齐（单一口子冻结）。

物理意图（spec §4 · [[eod-date-offbyone-fix]] 回归）：
    monkeypatch trading.clock.now 返固定时间（T 日盘后），eod 落 plan_T+1，
    pre_open（仍冻结同一时间或 T+1）读 plan_T+1，key 对齐。
    C-6 前：patch 多处 datetime（position_book/pipeline/engine）才能冻结；
    C-6 后：patch trading.clock.now 单一口子即冻结全包。
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import trading.clock as clock


def test_clock_freeze_single_source():
    """patch trading.clock.now 单一口子 → today/trading_day 一致派生。"""
    fixed = datetime(2026, 7, 28, 19, 0, 0)  # 周二盘后
    with patch("trading.clock.now", lambda: fixed):
        assert clock.today() == "2026-07-28"
        assert clock.trading_day() != "2026-07-28"  # 次交易日（周三）
        # 同轮多次调用结果一致（无跨午夜漂移）
        assert clock.today() == clock.today()


def test_eod_pre_open_key_alignment_under_frozen_clock():
    """eod 落盘 key（trading_day）= pre_open 读 key（today）当 pre_open 在 T+1。

    构造：冻结 T 日盘后 → eod trading_day() = T+1 → 冻结 T+1 盘前 → pre_open today() = T+1。
    两 key 相等 = 对齐（[[eod-date-offbyone-fix]] 病灶回归）。
    """
    # T 日盘后（周二 2026-07-28 19:00）
    t_eod = datetime(2026, 7, 28, 19, 0, 0)
    with patch("trading.clock.now", lambda: t_eod):
        eod_key = clock.trading_day()  # eod 落盘 key
    # T+1 盘前（周三 2026-07-29 09:22）
    t_pre_open = datetime(2026, 7, 29, 9, 22, 0)
    with patch("trading.clock.now", lambda: t_pre_open):
        pre_open_key = clock.today()  # pre_open 读 key
    assert eod_key == pre_open_key, (
        f"eod 落盘 key ({eod_key}) 必须 = pre_open 读 key ({pre_open_key})，"
        "否则 confirmed 计划存在但 pre_open 全部 reason=无计划（[[eod-date-offbyone-fix]] 病灶）")
