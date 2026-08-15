# -*- coding: utf-8 -*-
"""A 股交易日历数据层（Tushare trade_cal 拉取 + 本地缓存 + 期望最新交易日）。

Why 独立于 trading/calendar.py（M1 循环切断 · 2026-08-12）：
  原 fetch_trade_cal 在 trading/calendar.py，被 data/integrity.py 反查形成
  data.integrity → trading.calendar 真函数级循环（与 trading.data_ctx → data.integrity
  互锁）。把「trade_cal 数据拉取 + 缓存」下沉到 data 层，循环断；data.integrity 改
  从 data.calendar 取（data→data，无循环）。trading/calendar.py 保留会话语义
  （is_trading_day / 盘中时段），依赖此处的 fetch_trade_cal。

T9 下沉（data→trading 边清零 · 2026-08-15）：``expected_latest_trade_day``（数据实时性
检查的期望锚点——纯交易日历域函数：只依赖交易日判定 + 时刻比较，无任何交易语义）自
trading/calendar.py 收编至此。trading/calendar.py 改 ``from data.calendar import
expected_latest_trade_day`` re-export，engine.py / orchestrate / broadcast 既有消费者
零改动。

依赖：仅 data._tushare_compat.get_pro（凭证统一入口）+ 标准库。无 trading 依赖。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, time, timedelta
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
        from data._tushare_compat import _call_with_timeout, get_pro  # 统一凭证入口（与 sync_daily_incremental 同源）
        pro = get_pro()
        # _call_with_timeout 包裹 trade_cal（Task G4）：tushare pro_api 底层 requests 无
        # timeout，启动期 TCP 挂起会卡死整个启动流程（calendar 在盘前被调用判交易日）。
        # 包裹后挂起在 30s 后抛 TimeoutError → 被下方 broad except 捕获 → weekday 兜底，
        # 与"无 token / 网络失败"语义同口径（启动不卡死，仅识周末不识节假日）。
        df = _call_with_timeout(
            pro.trade_cal,
            exchange="SSE",
            start_date=f"{year}0101",
            end_date=f"{year}1231",
            fields="cal_date,is_open",
        )
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


def _is_trading_day(date_str: str) -> bool:
    """date_str(YYYY-MM-DD) 是否 A 股交易日（data 层局部判定）。

    Why 不复用 trading.calendar.is_trading_day：那会形成 data→trading 反查（T9 断边
    清的就是这条边）。本函数是其最小依赖镜像——同一句 ``date_str in fetch_trade_cal(year)``
    （trading 侧实现亦如此），仅服务本模块的 expected_latest_trade_day；trading 侧的
    is_trading_day 原样保留（engine 等大量消费者 + 测试 patch 锚不动）。
    """
    year = int(date_str[:4])
    return date_str in fetch_trade_cal(year)


# 物理意图：数据实时性检查的期望锚点——盘后查 T 数据是否落湖，盘前查 T-1 是否齐全。
# 决策口径：now >= 15:00 且今天是交易日 → 期望今天（收盘数据清算后应落湖）；
#           否则 → 回溯最近一个交易日（最多 10 自然日，覆盖长假）。
def expected_latest_trade_day(now: datetime) -> str:
    """期望最新交易日（数据湖应含此日完整数据）。

    T9（2026-08-15）自 trading/calendar.py 下沉至此（纯交易日历域函数归 data 层），
    trading.calendar 保留 re-export（既有消费者零改动）。逻辑逐行等价，唯一差异：
    交易日判定改用本模块私有 ``_is_trading_day``（避免 data→trading 反查）。

    Args:
        now: 当前时刻。

    Returns:
        YYYY-MM-DD。盘后交易日→今天；否则→上一个交易日；全非交易日兜底 today。
    """
    today = now.strftime("%Y-%m-%d")
    # 盘后（15:00 之后）且今天交易日 → 期望今天
    if now.time() >= time(15, 0) and _is_trading_day(today):
        return today
    # 否则回溯找上一个交易日（最多 10 自然日，覆盖长假 + 周末）
    for i in range(1, 11):
        prev = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        if _is_trading_day(prev):
            return prev
    return today  # 兜底：窗口内无交易日（极端长假），返 today 让检查自然 FAIL 告警
