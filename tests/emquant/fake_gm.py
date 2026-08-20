# -*- coding: utf-8 -*-
"""FakeGm——gm SDK 测试替身（history/current + 常量），口径按 Task 2 权威文档钉死。

物理定位：
    产物单文件里所有 gm 调用都经 §2 数据层的 `_api()` seam（模块级 `_GM` 缓存），
    测试 monkeypatch 产物模块属性 `_GM` 注入本替身即完成「无终端离线对拍」。替身
    的价值不在「能跑通」而在【口径与真 SDK 一致】——签名/默认值/返回列名/常量值
    全部抄自 docs/research/2026-08-21-gm-sdk-api-verified.md（gm 3.0.186 源码核对），
    若真 SDK 行为与本文档分叉，替身必须跟着改，让分叉在测试期暴露而不是 live 期。

钉死的口径要点（引用 = 该文档章节/差异表编号）：
    - history 完整签名：symbol, frequency, start_time, end_time, fields=None,
      skip_suspended=True, fill_missing=None, adjust=None, adjust_end_time='', df=False
      （Q6/D8——fields 与 adjust 之间还有 skip_suspended/fill_missing 两参）；
    - bar 返回 12 列（Q6 三源一致清单，序为 subscribe 白名单序）：
      symbol, frequency, open, high, low, close, volume, amount, position, pre_close,
      bob, eob；fields 指定时仅含指定列；
    - 常量 ADJUST_NONE=0 / ADJUST_PREV=1 / ADJUST_POST=2（gm/enum.py:130-136）；
    - eob=bar 结束时刻（日线折算为北京时间 naive datetime，本替身用 15:00:00）、
      bob=bar 开始时刻（09:30:00）——刻意保留时分秒：数据层必须把 eob normalize 到
      零点才能与本地腿 tushare 零点口径逐字段可比，替身不带时分秒就测不出这层折算；
    - current 快照最新价键名是 price（Q3/D5——last_price 是通识错名）。

后续任务扩：order_volume/order_cancel/get_cash/get_position 等交易族替身（本文件
是唯一定义点，后续任务只增不改既有口径）。
"""
from __future__ import annotations

import pandas as pd

# gm/enum.py:130-136 复权常量（api 经 `from gm.enum import *` 再导出，值同源）
ADJUST_NONE = 0
ADJUST_PREV = 1
ADJUST_POST = 2

# Q6 三源一致的 bar 12 列（subscribe 白名单序：basic.py:147）
BAR_COLUMNS = ["symbol", "frequency", "open", "high", "low", "close", "volume",
               "amount", "position", "pre_close", "bob", "eob"]


def _synthetic_bars(symbol: str, frequency: str, days: pd.DatetimeIndex) -> pd.DataFrame:
    """确定性合成 OHLCV——同参数恒产出同 df（测试期望值可手算，无随机抖动）。

    价格构造：close 在 [10, 12) 区间按 (i*37)%100 锯齿波动（37 与 100 互素 → 周期
    100 根不重复，形态上多空交替）；open=前根 close（无跳空，TR 纯振幅）；high/low
    在 [open, close] 外扩 1%（日线振幅量级）；volume≈百万股级线性缓增。数值本身不
    模拟任何真实形态——本替身只考【传输口径】（列名/签名/参数），识别行为对拍由
    test_kernel_equivalence 的确定性形态构造承担（职责分离）。
    """
    closes = [10.0 + ((i * 37) % 100) / 50.0 for i in range(len(days))]
    cols = {c: [] for c in BAR_COLUMNS}
    prev = closes[0]
    for i, (d, c) in enumerate(zip(days, closes)):
        hi = max(prev, c) * 1.01
        lo = min(prev, c) * 0.99
        vol = 1_000_000.0 + i * 1_000.0
        cols["symbol"].append(symbol)
        cols["frequency"].append(frequency)
        cols["open"].append(prev)
        cols["high"].append(hi)
        cols["low"].append(lo)
        cols["close"].append(c)
        cols["volume"].append(vol)
        cols["amount"].append(c * vol)
        cols["position"].append(0.0)
        cols["pre_close"].append(prev)
        cols["bob"].append(d + pd.Timedelta(hours=9, minutes=30))    # bar 开始时刻
        cols["eob"].append(d + pd.Timedelta(hours=15))               # bar 结束时刻（日线 15:00）
        prev = c
    return pd.DataFrame(cols, columns=BAR_COLUMNS)


class FakeGm:
    """gm.api 模块的记录型替身：忠实签名 + 调用快照 + 可注入的失败模式。

    失败注入面（编排层降级路径的测试燃料）：
        raise_on_history=True   → history 恒抛 RuntimeError（模拟 GmError/断连形态）
        empty_symbols={...}     → 指定 symbol 返回空 DataFrame（模拟新上市/退市/查无）
    观测面：self.calls 逐次记录 history 的全部实参（含默认参落定值），供测试把
    「frequency='1d' / adjust=1 / adjust_end_time=end_date / skip_suspended=True /
    df=True」这些取数契约逐项钉死。
    """

    # 常量在类上镜像模块级定义：真 gm.api 是【模块】、ADJUST_* 是模块属性，数据层
    # 经 `a.ADJUST_PREV` 取值——替身必须以同名类属性呈现同构接口面，漏一个就是
    # AttributeError 假阴性（对拍价值反噬）。
    ADJUST_NONE = ADJUST_NONE
    ADJUST_PREV = ADJUST_PREV
    ADJUST_POST = ADJUST_POST

    def __init__(self, raise_on_history: bool = False, empty_symbols=()):
        self.raise_on_history = raise_on_history
        self.empty_symbols = set(empty_symbols)
        self.calls: list[dict] = []

    # ------------------------------------------------------------------ history
    def history(self, symbol, frequency, start_time, end_time, fields=None,
                skip_suspended=True, fill_missing=None, adjust=None,
                adjust_end_time="", df=False):
        """历史行情替身（签名 = Q6 query.py:562-565 逐字，默认值同源）。

        合成规则：pd.bdate_range(start, end) 以营业日近似交易日（周末剔除；法定
        节假日与临时休市的差异不影响被测逻辑——数据层按「返回什么就吃什么」处理，
        历的真值由 build_calendar 用指数日线自行推导，不依赖本替身的日历属性）。
        """
        self.calls.append(dict(symbol=symbol, frequency=frequency, start_time=str(start_time),
                               end_time=str(end_time), fields=fields,
                               skip_suspended=skip_suspended, fill_missing=fill_missing,
                               adjust=adjust, adjust_end_time=adjust_end_time, df=df))
        if self.raise_on_history:
            raise RuntimeError('{"status": 1100, "message": "模拟 gm 取数失败（终端未连接/GmError 形态）"}')
        days = pd.bdate_range(str(start_time), str(end_time))
        if symbol in self.empty_symbols or len(days) == 0:
            frame = pd.DataFrame(columns=BAR_COLUMNS)      # 空 df：列名仍在（gm 空结果口径）
        else:
            frame = _synthetic_bars(symbol, frequency, days)
        if fields:
            # fields 过滤语义（Q6）：逗号串或 list，返回仅含指定列
            keep = [c.strip() for c in fields.split(",")] if isinstance(fields, str) else list(fields)
            frame = frame[keep]
        if df:
            return frame.reset_index(drop=True)
        return frame.to_dict("records")                     # df=False → List[Dict]（Q6）

    # ------------------------------------------------------------------ current
    def current(self, symbols, fields="", include_call_auction=False):
        """tick 快照替身（basic.py:312 签名；最新价键名 price——Q3/D5 通识错名警示）。"""
        self.calls.append(dict(api="current", symbols=symbols, fields=fields,
                               include_call_auction=include_call_auction))
        syms = [symbols] if isinstance(symbols, str) else list(symbols)

        def _px(s: str) -> float:
            # 确定性伪价：ord 累加（hash() 受 PYTHONHASHSEED 污染，禁用于期望值）
            return 10.0 + (sum(ord(ch) for ch in s) % 100) / 10.0

        return [{"symbol": s, "price": _px(s), "open": _px(s) * 0.99, "high": _px(s) * 1.01,
                 "low": _px(s) * 0.98, "cum_volume": 1e6, "cum_amount": 1e7} for s in syms]
