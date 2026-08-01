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

from trading.calendar import next_trading_day


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
