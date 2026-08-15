# -*- coding: utf-8 -*-
"""A 股交易日历会话语义（交易日判定 + 盘中时段）。

Why 与 data/calendar.py 分离（M1 循环切断 · 2026-08-12）：
  trade_cal 数据拉取 + 缓存已下沉到 data/calendar.py（断 data.integrity→trading.calendar
  真函数级循环）。本模块保留会话语义——依赖 data.calendar.fetch_trade_cal 取交易日列表，
  属 trading→data 的正常单向依赖。

T9 下沉（data→trading 边清零 · 2026-08-15）：expected_latest_trade_day（数据实时性检查
的期望锚点，纯交易日历域函数）亦下沉 data/calendar.py。本模块 re-export 保兼容。

兼容 re-export：``trading.calendar.fetch_trade_cal`` 名字保留（外部调用方如 compute/stop.py、
test_stop_loss 的 patch 目标仍指向 trading.calendar.fetch_trade_cal）；``trading.calendar.
expected_latest_trade_day`` 名字保留（engine.py:724 lazy import、orchestrate/pipeline.py、
broadcast/__main__.py 既有消费者零改动）——均指向 data.calendar 的同一函数对象，行为等价。
"""
from __future__ import annotations

from datetime import datetime, time

# 兼容 re-export：fetch_trade_cal / expected_latest_trade_day 物理实现均已下沉到
# data/calendar.py（M1 循环切断 + T9 data→trading 边清零）。保留 trading.calendar
# 名字空间绑定，外部调用方与 patch 目标不改。
from data.calendar import expected_latest_trade_day, fetch_trade_cal  # noqa: F401


def is_trading_day(date_str: str) -> bool:
    """date_str(YYYY-MM-DD) 是否 A 股交易日。查缓存 trade_cal，缺则 fetch。"""
    year = int(date_str[:4])
    days = fetch_trade_cal(year)
    return date_str in days


def next_trading_day(date_str: str) -> str:
    """``date_str``（YYYY-MM-DD）之后最近的一个 A 股交易日。

    用途（修 date 错位 bug · 2026-07-28）：T 日盘后 ``engine._eod`` 落盘计划的
    生效日 = ``next_trading_day(T)``，与次日 ``_pre_open(today=T+1)`` 的
    ``load_plan(today)`` 对齐。原 ``_eod`` 用 ``datetime.now()``（T 日）落盘，
    pre_open 次日却读 ``plan_(T+1)``，永远差一天 → 计划永远挂不上单（系统连续
    多日有 confirmed 计划但 pre_open 全部 reason=「无计划」跳过的根因）。

    实现：从 ``date_str+1`` 起逐日 ``is_trading_day``，最多向前找 15 自然日
    （覆盖周末 + 春节/国庆等长假）。跨年（12 月底 → 次年 1 月）由
    ``is_trading_day`` 内部按候选日的 year 拉 trade_cal 自动处理，无需特判。
    """
    from datetime import timedelta
    d = datetime.strptime(date_str, "%Y-%m-%d")
    for i in range(1, 16):
        cand = (d + timedelta(days=i)).strftime("%Y-%m-%d")
        if is_trading_day(cand):
            return cand
    # 兜底：15 自然日内无交易日（极端长假叠加）→ 返 date_str+1。该值语义上
    # 「可能非交易日」，交由上层 _pre_open 的 is_trading_day 守卫自然告警拦截
    # （宁可显式失败，不静默落一份永远挂不上的脏计划）。
    return (d + timedelta(days=1)).strftime("%Y-%m-%d")


def previous_trading_day(date_str: str) -> str:
    """``date_str``（YYYY-MM-DD）之前最近的一个 A 股交易日。

    用途（SSoT Phase B 断点-2 · B1 pre_open 超期现算基准日）：
        expired_positions 改 pre_open 现算（删除 post_close 写盘 + pre_open 读盘），
        基准日 = ``previous_trading_day(today)`` = 上一交易日（holding_days 计算的 asof）。
        Why 上一交易日非 today：pre_open 在 T 日开盘前跑，T 日尚未收盘，entry_date 与
        T 日做差会把 T 日算入 holding_days（自然多算一天）→ 超期判定整体提前一日 →
        误平窗口内的持仓（致命）。基准日取上一交易日（= T-1 收盘口径）保证零漂移。

    实现（与 ``next_trading_day`` 对称）：从 ``date_str-1`` 起逐日 ``is_trading_day`` 回溯，
    最多 15 自然日（覆盖周末 + 春节/国庆等长假）。跨年（1 月初 → 上年 12 月底）由
    ``is_trading_day`` 内部按候选日的 year 拉 trade_cal 自动处理，无需特判。
    """
    from datetime import timedelta
    d = datetime.strptime(date_str, "%Y-%m-%d")
    for i in range(1, 16):
        cand = (d - timedelta(days=i)).strftime("%Y-%m-%d")
        if is_trading_day(cand):
            return cand
    # 兜底：15 自然日内无交易日（极端长假叠加，A 股无此场景但 defensive 防 IndexError）
    # → 返 date_str-1。该值可能非交易日，交由上层 _pre_open 的 is_trading_day 守卫
    # 自然告警拦截（宁可显式失败，不静默误算超期）。
    return (d - timedelta(days=1)).strftime("%Y-%m-%d")


def is_intraday_session(now: datetime) -> bool:
    """是否 A 股盘中（09:15-15:00 连续；含集合竞价与午休）。

    D2 修订（2026-08-06 用户选项 1）：与 trading_service._in_a_share_session 对齐——
    午休不拦（柜台可接收排队单），隔夜/周末仍由交易日判定拦。
    """
    t = now.time()
    return time(9, 15) <= t <= time(15, 0)


# expected_latest_trade_day 已下沉 data/calendar.py（T9 · 2026-08-15），本模块顶部
# re-export（见文件头 import）；实现与物理意图注释随迁至 data.calendar。
