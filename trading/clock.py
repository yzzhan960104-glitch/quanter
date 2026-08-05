# -*- coding: utf-8 -*-
"""C-6 单一时间源口子。trading 包内所有 datetime.now() 替换为本模块函数。

物理意图（[[eod-date-offbyone-fix]] 教训）：
    时间源散落（engine.py 18 处 + position_book/order_state/pipeline）→ 同轮跨午夜
    漂移 + 测试冻结需 patch 多处 datetime + 未来 eod/pre_open 类似 key 漂移无防线。
    本模块提供单一口子：测试 monkeypatch trading.clock 即冻结全包时间。

三函数命名区分读/写口径（避免 eod/pre_open 混淆，spec §3.1）：
    - today()       = 今日（pre_open 读 plan key 口径）
    - trading_day() = 次交易日（eod 落盘 plan key 口径 = next_trading_day(today)）
    - now()         = 当前 datetime（事件时间戳 submitted_at/order_id/written_at）

不凝固时间（clock 无状态，每次调 datetime.now()）：防同轮跨午夜漂移靠触发点入口缓存
（engine._eod/_pre_open/_stoploss/_post_close 入口算一次 _today/_td 传下游），不靠
clock 内部缓存——进程级缓存会让长跑服务时间凝固不真实。
"""
from __future__ import annotations

from datetime import datetime

from trading.calendar import next_trading_day, previous_trading_day


def now() -> datetime:
    """当前 datetime（单一时间源口子，事件时间戳用）。

    测试 monkeypatch trading.clock.now 冻结全包时间（替代 patch 各模块 datetime）。
    """
    return datetime.now()


def today() -> str:
    """今日 YYYY-MM-DD（pre_open 读 plan key 口径）。

    用途：load_plan/save_plan 读 key、is_trading_day 守卫、holding_days 计算。
    禁止 eod 落盘用本函数（eod 必用 trading_day，避免 key 错位）。
    """
    return now().strftime("%Y-%m-%d")


def trading_day() -> str:
    """次交易日（eod 落盘 plan key 口径 = next_trading_day(today)）。

    物理意图：eod（T 日盘后）落 plan_T+1，pre_open（T+1 开盘前）读 plan_T+1。
    today() 与 trading_day() 命名区分读/写口径——避免 eod/pre_open key 错位
    （[[eod-date-offbyone-fix]] 病灶：原 eod 用 today 落盘，pre_open 读 today 永远差一天）。
    """
    return next_trading_day(today())


def pretrade_date(date: str) -> str:
    """``date`` 的上一交易日（薄转发 calendar.previous_trading_day）。

    物理意图（SSoT Phase B 断点-2 · B1 pre_open 超期现算基准日）：
        pre_open 在 T 日开盘前算超期持仓，基准日必须取 T-1（上一交易日收盘口径）而非
        today（T 日未收盘会把 T 日算入 holding_days → 超期整体提前一日 → 误平窗口内持仓）。
        集中口子（与 today/trading_day 同源）便于测试 monkeypatch trading.clock.pretrade_date
        冻结基准日，避免散落调用 previous_trading_day 难以 patch。
    """
    return previous_trading_day(date)
