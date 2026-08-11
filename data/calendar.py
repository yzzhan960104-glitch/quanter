# -*- coding: utf-8 -*-
"""A 股交易日历数据层（Tushare trade_cal 拉取 + 本地缓存）。

Why 独立于 trading/calendar.py（M1 循环切断 · 2026-08-12）：
  原 fetch_trade_cal 在 trading/calendar.py，被 data/integrity.py 反查形成
  data.integrity → trading.calendar 真函数级循环（与 trading.data_ctx → data.integrity
  互锁）。把「trade_cal 数据拉取 + 缓存」下沉到 data 层，循环断；data.integrity 改
  从 data.calendar 取（data→data，无循环）。trading/calendar.py 保留会话语义
  （is_trading_day / 盘中时段 / 期望最新交易日），依赖此处的 fetch_trade_cal。

依赖：仅 data._tushare_compat.get_pro（凭证统一入口）+ 标准库。无 trading 依赖。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

_CACHE_DIR = Path("logs")


def _cache_path(year: int) -> Path:
    return _CACHE_DIR / f"trade_cal_{year}.json"


def fetch_trade_cal(year: int) -> list[str]:
    """拉 Tushare 某年交易日历，缓存 logs/trade_cal_<year>.json。失败返空 list（降级）。

    token 读取统一走 ``data._tushare_compat.get_pro``（Phase 1.5 任务5 统一凭证入口）：
      - 原 calendar 自己读 os.getenv(TUSHARE_TOKEN) 与 _tushare_compat 口径不一致，
        直连 tushare 切换后可能漏读/读错 token；统一走 get_pro 后凭证读取在一处。
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
    days, d = [], datetime(year, 1, 1)
    while d.year == year:
        if d.weekday() < 5:
            days.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    return days
