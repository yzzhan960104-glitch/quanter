# -*- coding: utf-8 -*-
"""组件7 MinBarFeeder：Tushare stk_mins 分钟行情源（spec §6）。

物理意图：_stoploss 依赖 qmt_market_data.get_quotes（xtdata 当日累积 high/low+last）；
E2E mock QMT 后 xtdata 空 → 用 stk_mins 5min bar 按时点切片累积，注入 get_quotes，
驱动 decide_exit 真实触发止损/止盈/cancel_on（非概率瞎猜）。

Why stk_mins 非 pro_bar：stk_mins 是 tushare 原生分钟接口（doc_id=370），返 5min OHLCV，
已验证 token 权限 + 字段（trade_time/open/high/low/close/vol/amount）。
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, time
from typing import Callable

import pandas as pd

StkMinsLoader = Callable[[str, date], pd.DataFrame]   # (sym, t_date) -> 5min bar df
DailyLoader = Callable[[str, date], dict]              # (sym, t_date) -> {high, low, close}


def _default_stk_mins_loader(sym: str, t_date: date) -> pd.DataFrame:
    """生产 loader：Tushare pro.stk_mins 拉当日 5min bar。

    full_run 集成修复（根因 4 · stk_mins 限频容错）：
        Tushare stk_mins 接口硬限频（实测 1 次/分钟，spec §6 假设"690 次限频内"是误判）。
        23 日 × 多标的必超限 → pro.stk_mins 抛 "频率超限" 异常。原 loader 不捕获 →
        MinBarFeeder._load_bars 抛 → feed 抛 → pre_open 挂单取价 + stoploss 行情注入
        全失败（full_run 7/2 起 pre_open 全 REJECTED + 7/14+ stoploss 失败）。

    修法：try/except 容错——限频/网络异常 → 返空 DataFrame（让 _load_bars 走 degraded
    降级日线分支，spec §6.2 降级设计已就位）。空 df 触发 feed 的日线降级 + degraded=True
    标记（ReportBuilder §5 行情降级清单可审计）。stk_mins 调用仍消耗限频配额（无法绕开
    Tushare 硬限），但异常被吞不阻断回放，限频期间全降级日线近似（保 E2E 可行性）。
    """
    import logging
    import os
    import tushare as ts
    from dotenv import load_dotenv
    load_dotenv()
    ts.set_token(os.getenv("TUSHARE_TOKEN"))
    pro = ts.pro_api()
    d = t_date.isoformat()
    try:
        return pro.stk_mins(ts_code=sym, start_date=f"{d} 09:00:00",
                            end_date=f"{d} 15:00:00", freq="5min")
    except Exception as exc:
        # 限频/网络异常 → 返空 df 降级日线（不阻断回放；degraded 标记由 _load_bars 置）。
        logging.getLogger(__name__).warning(
            "stk_mins 拉取失败 symbol=%s date=%s（限频/网络？降级日线）：%s",
            sym, d, str(exc)[:120])
        return pd.DataFrame()


def _default_daily_loader(sym: str, t_date: date) -> dict:
    """降级 loader：data_lake 日线 high/low/close（T 日已收盘）。"""
    lake = pd.read_parquet("data_lake/a_shares_daily.parquet")
    try:
        row = lake.xs((pd.Timestamp(t_date), sym))
    except KeyError:
        return {"high": None, "low": None, "close": None}
    return {"high": float(row["high"]), "low": float(row["low"]), "close": float(row["close"])}


class MinBarFeeder:
    """stk_mins 分钟行情源：按时点切片累积 high/low/last，注入 get_quotes。

    Args:
        stk_mins_loader: (sym, t_date) -> 5min bar df（测试可注入 mock）。
        daily_loader: 降级用日线 loader。
    """

    def __init__(self, stk_mins_loader: StkMinsLoader | None = None,
                 daily_loader: DailyLoader | None = None) -> None:
        self._stk_mins_loader = stk_mins_loader or _default_stk_mins_loader
        self._daily_loader = daily_loader or _default_daily_loader
        self._cache: dict[tuple[str, date], pd.DataFrame] = {}  # (sym, t_date) -> bar df
        self.degraded: bool = False  # 降级标记（供 ReportBuilder §5）
        # 当前 feed 上下文（patch_get_quotes 用）
        self._ctx_syms: list[str] = []
        self._ctx_t_date: date | None = None
        self._ctx_up_to: time | None = None

    def set_context(self, symbols: list[str], t_date: date, up_to: time) -> None:
        """设置 patch_get_quotes 的当前上下文（ReplayDriver 每盘中时点 freeze 后调）。"""
        self._ctx_syms = symbols
        self._ctx_t_date = t_date
        self._ctx_up_to = up_to

    def _load_bars(self, sym: str, t_date: date) -> pd.DataFrame:
        """加载（带 cache）stk_mins 5min bar；空 → 标 degraded。"""
        key = (sym, t_date)
        if key in self._cache:
            return self._cache[key]
        df = self._stk_mins_loader(sym, t_date)
        if df is None or len(df) == 0:
            self.degraded = True  # 停牌/限频 → 降级标记
            df = pd.DataFrame()  # cache 空 df（feed 走降级分支）
        self._cache[key] = df
        return df

    def feed(self, symbols: list[str], t_date: date, up_to: time) -> dict[str, dict]:
        """按时点切片累积：取 trade_time.time() <= up_to 的 bar，累积 high=max/low=min/last=末根 close。

        stk_mins 空 → 降级日线 high/low/close。
        """
        out: dict[str, dict] = {}
        for sym in symbols:
            # 盘前/盘后（无分钟数据）直接日线降级，不调 stk_mins（根因4 · 避免无意义限频消耗）。
            # 物理意图：pre_open 09:25（<9:30）/ post_close 15:30（>15:00）时点本就无 5min bar，
            # 调 stk_mins 只消耗硬限频配额（1次/分钟）返空。盘中 [9:30, 15:00] 走 stk_mins
            # （15:00 是收盘 bar 仍有效，含边界）。直接日线 high/low/close 降级 + 标 degraded
            # （ReportBuilder §5 可审计），既避开限频又保 E2E 可行（spec §6 降级设计）。
            if up_to < time(9, 30) or up_to > time(15, 0):
                # 盘前/盘后无分钟数据是设计常态（非故障）→ 日线降级【不标 degraded】。
                # degraded 只标盘中 stk_mins 拉取失败/空（_load_bars 内，属异常降级）；
                # 盘前/盘后是物理必然（9:30 前 / 15:00 后无 bar），标 degraded 会污染
                # ReportBuilder §5「行情降级清单」（把每日 pre_open/post_close 都算降级）。
                d = self._daily_loader(sym, t_date)
                out[sym] = {"last_price": d["close"], "high": d["high"], "low": d["low"]}
                continue
            df = self._load_bars(sym, t_date)
            if len(df) == 0:
                # 降级日线（_stoploss bar 用日线 high/low/close）
                d = self._daily_loader(sym, t_date)
                out[sym] = {"last_price": d["close"], "high": d["high"], "low": d["low"]}
                continue
            # trade_time 切片（up_to 含）
            df = df.copy()
            df["t"] = pd.to_datetime(df["trade_time"]).dt.time
            sliced = df[df["t"] <= up_to]
            if len(sliced) == 0:
                # up_to 早于首根（如 9:25 < 9:30）→ 用日线降级
                d = self._daily_loader(sym, t_date)
                out[sym] = {"last_price": d["close"], "high": d["high"], "low": d["low"]}
                continue
            out[sym] = {
                "last_price": float(sliced["close"].iloc[-1]),  # 末根 close
                "high": float(sliced["high"].max()),            # 累积最高
                "low": float(sliced["low"].min()),              # 累积最低
            }
        return out

    @contextmanager
    def patch_get_quotes(self):
        """monkeypatch trading.qmt_market_data.get_quotes 返当前上下文的 feed 结果。

        _stoploss 内 `quotes = await qmt_market_data.get_quotes(syms)` 命中本 patch。
        """
        from unittest.mock import patch
        syms = self._ctx_syms
        t_date = self._ctx_t_date
        up_to = self._ctx_up_to
        quotes = self.feed(syms, t_date, up_to) if t_date else {}
        with patch("trading.qmt_market_data.get_quotes",
                   new=_AsyncReturn(quotes)):
            yield


class _AsyncReturn:
    """可调用 + 可 await 的伪 coroutine：让同步 feed 结果被 await。

    物理意图：_stoploss 内 `quotes = await qmt_market_data.get_quotes(syms)` ——
    生产 get_quotes 是 ``async def``（broker/qmt_quote.py:109），调用返 coroutine、
    await 拿值。mock 时需同一调用形态：``get_quotes(syms)`` 必须可调用（返本对象），
    且返回对象必须可 ``await``（``__await__`` 拿到 quotes）。

    Why ``__call__`` 返 self 而非 _wrap：本对象已实现 ``__await__``，调用后直接
    ``await self`` 即可，避免每次调用新建 wrapper（极简、显式）。
    """
    def __init__(self, value):
        self._value = value

    def __call__(self, *_args, **_kwargs):
        # 呼应 ``get_quotes(syms)`` 调用形态：吞掉 syms 入参（feed 已算好），返可 await 的 self。
        return self

    def __await__(self):
        async def _wrap():
            return self._value
        return _wrap().__await__()
