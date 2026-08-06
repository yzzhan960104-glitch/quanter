# -*- coding: utf-8 -*-
"""A 股交易日历（Tushare trade_cal 缓存 + 盘中时段判定）。

Why 独立模块：engine 四触发点都需判交易日/时段（节假日跳过、午休不监控）；
Tushare pro.trade_cal 每年初拉一次缓存本地 JSON，避免每次调 API。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, time
from pathlib import Path

logger = logging.getLogger(__name__)

_CACHE_DIR = Path("logs")


def _cache_path(year: int) -> Path:
    return _CACHE_DIR / f"trade_cal_{year}.json"


def fetch_trade_cal(year: int) -> list[str]:
    """拉 Tushare 某年交易日历，缓存 logs/trade_cal_<year>.json。失败返空 list（降级）。

    token 读取统一走 ``data._tushare_compat.get_pro``（Phase 1.5 任务5 修复）：
      - 原 calendar 自己读 os.getenv(TUSHARE_TOKEN) 与 _tushare_compat 的凭证读取
        口径不一致，直连 tushare 切换（2026-07-24，代理 tnskhdata 废弃后纯直连）后
        calendar 仍按老口径可能漏读/读错 token。
      - 统一走 get_pro 后：token 读取/未来 provider 切换全在一处，calendar 不再
        关心凭证细节（守 Layer2 §7 单一职责：凭证归 _tushare_compat）。
    weekday 兜底仅在 get_pro 抛异常（无 token / 网络失败 / tushare 缺失）时触发。
    """
    cache = _cache_path(year)
    if cache.exists():
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except Exception:
            pass
    try:
        from data._tushare_compat import get_pro  # 统一凭证入口（与 sync_daily_incremental 同源）
        pro = get_pro()
        df = pro.trade_cal(exchange="SSE", start_date=f"{year}0101", end_date=f"{year}1231",
                           fields="cal_date,is_open")
        days = df[df["is_open"] == 1]["cal_date"].tolist()
        days = [f"{d[:4]}-{d[4:6]}-{d[6:]}" for d in days]
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(days), encoding="utf-8")
        return days
    except Exception as e:
        # weekday fallback：无 token / 网络失败 / tushare 缺失 的最后兜底
        # （仅识周末，不识节假日——是物理边界降级，应触发上层告警排查）
        logger.warning("fetch_trade_cal 失败（%s），用 weekday 兜底（仅识周末不识节假日）", e)
        return _weekday_fallback(year)


def _weekday_fallback(year: int) -> list[str]:
    """无 Tushare 时退化为「全年非周末」（不识节假日，仅兜底）。"""
    from datetime import timedelta
    days, d = [], datetime(year, 1, 1)
    while d.year == year:
        if d.weekday() < 5:
            days.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    return days


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
    from datetime import datetime, timedelta
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
    from datetime import datetime, timedelta
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


# 物理意图：数据实时性检查的期望锚点——盘后查 T 数据是否落湖，盘前查 T-1 是否齐全。
# 决策口径：now >= 15:00 且今天是交易日 → 期望今天（收盘数据清算后应落湖）；
#           否则 → 回溯最近一个交易日（最多 10 自然日，覆盖长假）。
def expected_latest_trade_day(now: datetime) -> str:
    """期望最新交易日（数据湖应含此日完整数据）。

    Args:
        now: 当前时刻。

    Returns:
        YYYY-MM-DD。盘后交易日→今天；否则→上一个交易日；全非交易日兜底 today。
    """
    from datetime import timedelta
    today = now.strftime("%Y-%m-%d")
    # 盘后（15:00 之后）且今天交易日 → 期望今天
    if now.time() >= time(15, 0) and is_trading_day(today):
        return today
    # 否则回溯找上一个交易日（最多 10 自然日，覆盖长假 + 周末）
    for i in range(1, 11):
        prev = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        if is_trading_day(prev):
            return prev
    return today  # 兜底：窗口内无交易日（极端长假），返 today 让检查自然 FAIL 告警
