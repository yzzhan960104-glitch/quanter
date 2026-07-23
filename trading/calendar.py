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
      - 原 calendar 自己读 os.getenv(TUSHARE_TOKEN)/os.getenv(TNSKHDATA_TOKEN) 与
        _tushare_compat 的「TNSKHDATA 代理优先 / TUSHARE 直连兜底」口径不一致，
        直连 tushare 切换（2026-07-24）后 calendar 仍按老口径可能漏读/读错 token。
      - 统一走 get_pro 后：代理/直连/token 轮询/未来 provider 切换全在一处，
        calendar 不再关心凭证细节（守 Layer2 §7 单一职责：凭证归 _tushare_compat）。
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


def is_intraday_session(now: datetime) -> bool:
    """是否 A 股盘中（9:30-11:30 / 13:00-15:00）。"""
    t = now.time()
    return (time(9, 30) <= t < time(11, 30)) or (time(13, 0) <= t < time(15, 0))


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
