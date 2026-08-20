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

Task 7 扩全（交易族，本文件是唯一定义点，只增不改既有口径）：
    - order_volume 完整签名（Q4 trade.py:115-127）：symbol, volume, side, order_type,
      position_effect, price=0, trigger_type=0, stop_price=0, order_duration, order_
      qualifier, account=""；返回 List[Dict]（DictLikeOrder 形态：dict 子类属性/下标
      双访问，Q4）；cl_ord_id 自增、限价买默认 OrderStatus_New(1)；
    - order_cancel(wait_cancel_orders) 收 dict 或 list[dict]、键 cl_ord_id+account_id
      （Q4 trade.py:398-417）；空列表抛「撤单信息不能为空」（Q4 GmError(-1) 形态）；
    - get_orders() 无参返日内全部委托（Q4 trade.py:336，状态词汇 = enum.py:23-37 的
      int 0-14）；get_position(account_id=None) 返 list[dict]、get_cash(account_id=None)
      返单 dict（Q5 query.py:1146/1163）；
    - get_history_symbol(symbol, start_date="", end_date="", df=False) 单数单标的
      （Q7 ds_instrument.py:118），字段含 pre_close/upper_limit/lower_limit（double）；
    - fill_order：测试注入成交的辅助面（真 SDK 由柜台撮合推进 filled_volume/status，
      替身必须给测试一个等效推进器——同步更新订单部成/已成与柜台持仓 vwap/volume）。
"""
from __future__ import annotations

from datetime import datetime

import pandas as pd

# gm/enum.py:130-136 复权常量（api 经 `from gm.enum import *` 再导出，值同源）
ADJUST_NONE = 0
ADJUST_PREV = 1
ADJUST_POST = 2

# gm/enum.py:62-68 / 113-115 交易方向/委托类型/开平仓常量（Q4 限价最小参数集同源）
OrderSide_Buy = 1          # 买入
OrderSide_Sell = 2         # 卖出
OrderType_Limit = 1        # 限价委托
OrderType_Market = 2       # 市价委托
PositionEffect_Open = 1    # 开仓
PositionEffect_Close = 2   # 平仓

# gm/enum.py:23-37 订单状态词汇（Q4 权威表；替身只在订单簿里推进 1→2→3 / 1→5 两族，
# 其余值留给测试直接注入以覆盖消费方的映射容错）
ORDER_STATUS = {
    "Unknown": 0, "New": 1, "PartiallyFilled": 2, "Filled": 3, "DoneForDay": 4,
    "Canceled": 5, "PendingCancel": 6, "Stopped": 7, "Rejected": 8, "Suspended": 9,
    "PendingNew": 10, "Calculated": 11, "Expired": 12, "AcceptedForBidding": 13,
    "PendingReplace": 14,
}

# order_volume 尾参默认值（Q4 签名原文是 OrderDuration_Unknown / OrderQualifier_Unknown；
# 核对文档未列数值，按 pb proto 默认 0 取形——消费方（§5 订单工具）从不传这两个参，
# 替身在此只为签名形状逐字一致）
OrderDuration_Unknown = 0
OrderQualifier_Unknown = 0


class _DictLike(dict):
    """DictLikeOrder 形态替身（Q4：gm/model/__init__.py:843 dict 子类，属性/下标双访问）。

    Why 不直接返裸 dict：消费方真身（§5 place_limit_buy 取 res[0]["cl_ord_id"]）在真 SDK
    上拿到的就是这种双访问对象；替身同构才能保证「属性风格写法的误用」在测试期暴露。
    """

    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError:
            raise AttributeError(key)

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

    # 交易族常量镜像（真 gm.api 是模块、这些是模块属性；§5 订单工具经 a.OrderSide_Buy
    # 等取值——替身必须以同名类属性呈现同构接口面，漏一个就是 AttributeError 假阴性）
    OrderSide_Buy = OrderSide_Buy
    OrderSide_Sell = OrderSide_Sell
    OrderType_Limit = OrderType_Limit
    OrderType_Market = OrderType_Market
    PositionEffect_Open = PositionEffect_Open
    PositionEffect_Close = PositionEffect_Close
    OrderDuration_Unknown = OrderDuration_Unknown
    OrderQualifier_Unknown = OrderQualifier_Unknown

    def __init__(self, raise_on_history: bool = False, empty_symbols=(),
                 symbol_info=None, raise_on_symbol_info: bool = False):
        self.raise_on_history = raise_on_history
        self.empty_symbols = set(empty_symbols)
        self.calls: list[dict] = []
        # ── 交易族状态面（Task 7 扩全）──
        # symbol_info：get_history_symbol 的证券信息注入面 {gm符号: {pre_close/lower_limit/
        #   upper_limit/...}}——注入什么返回什么（含 None 值=缺字段注入，测消费方回退分支）；
        #   未注入的符号按 pre_close=10.0 + 板位档位自算涨跌停（与 limit_down_price 档位同规）。
        self.symbol_info = dict(symbol_info) if symbol_info else {}
        self.raise_on_symbol_info = raise_on_symbol_info
        # 订单簿：{cl_ord_id: order dict}——order_volume 落单、order_cancel 撤单、
        # fill_order 推进成交，get_orders 全量快照（真 SDK 日内全部委托口径，Q4）
        self.orders: dict[str, dict] = {}
        self._cl_seq = 0                       # cl_ord_id 自增计数器（真 SDK 唯一性由柜台保证）
        # 柜台持仓/资金：fill_order 同步推进（简化：资金不随成交演化——CAP 闸测试由调用方
        # 显式注入数值，不依赖替身资金推演；持仓则真实推进，供对账测试消费）
        self.positions: dict[str, dict] = {}
        self.cash = {"account_id": "fake_account", "currency": 1, "nav": 1_000_000.0,
                     "available": 800_000.0, "market_value": 0.0, "balance": 800_000.0,
                     "frozen": 0.0, "order_frozen": 0.0, "pnl": 0.0, "fpnl": 0.0}

    # -------------------------------------------------------------- order_volume
    def order_volume(self, symbol, volume, side, order_type, position_effect,
                     price=0, trigger_type=0, stop_price=0,
                     order_duration=OrderDuration_Unknown,
                     order_qualifier=OrderQualifier_Unknown, account=""):
        """按指定量委托替身（签名 = Q4 trade.py:115-127 逐字，默认值同源）。

        行为口径：限价单落簿即 OrderStatus_New(1)（已报未成交——「限价买默认 PENDING」
        的 gm 侧对应状态），返回 List[Dict] 首元素为 _DictLike 订单（含 cl_ord_id）。
        成交推进走 fill_order（真 SDK 由柜台撮合回报推进，替身不自动成交）。
        """
        self.calls.append(dict(api="order_volume", symbol=symbol, volume=volume, side=side,
                               order_type=order_type, position_effect=position_effect,
                               price=price, trigger_type=trigger_type, stop_price=stop_price,
                               order_duration=order_duration, order_qualifier=order_qualifier,
                               account=account))
        self._cl_seq += 1
        cid = f"fake_cl_{self._cl_seq:06d}"
        now = datetime.now()
        o = _DictLike({
            "strategy_id": "fake_strategy", "account_id": account or "fake_account",
            "account_name": "fake_account", "cl_ord_id": cid, "order_id": f"ex_{cid}",
            "symbol": symbol, "side": side, "position_effect": position_effect,
            "order_type": order_type, "status": ORDER_STATUS["New"],
            "ord_rej_reason": 0, "ord_rej_reason_detail": "",
            "price": float(price), "volume": int(volume), "value": float(price) * int(volume),
            "filled_volume": 0, "filled_vwap": 0.0, "filled_amount": 0.0,
            "created_at": now, "updated_at": now,
        })
        self.orders[cid] = o
        return [o]                              # List[Dict]（Q4/D6：调用侧取 [0]["cl_ord_id"]）

    # -------------------------------------------------------------- order_cancel
    def order_cancel(self, wait_cancel_orders):
        """撤销委托替身（Q4 trade.py:398-417：收 dict 或 list[dict]，键 cl_ord_id+account_id）。

        空列表抛错形态对齐 GmError(-1, "撤单信息不能为空")（__str__ 的 JSON 形态）。
        已成/已撤/已拒（3/5/8）的撤单请求静默跳过——柜台「撤晚已成」语义（不炸、单留
        原终态），消费方以撤后 get_orders 的实况为准。
        """
        items = ([wait_cancel_orders] if isinstance(wait_cancel_orders, dict)
                 else list(wait_cancel_orders or []))
        if not items:
            raise RuntimeError('{"status": -1, "message": "撤单信息不能为空", '
                               '"function": "order_cancel"}')
        self.calls.append(dict(api="order_cancel", count=len(items)))
        now = datetime.now()
        for it in items:
            o = self.orders.get((it or {}).get("cl_ord_id"))
            if o is None or o["status"] in (ORDER_STATUS["Filled"], ORDER_STATUS["Canceled"],
                                            ORDER_STATUS["Rejected"]):
                continue
            o["status"] = ORDER_STATUS["Canceled"]
            o["updated_at"] = now
        return None                              # 真身无返回值（Q4）

    # ------------------------------------------------------- get_orders/持仓/资金
    def get_orders(self):
        """查询日内全部委托替身（Q4 trade.py:336：无参，返回订单 dict 列表浅拷贝）。"""
        self.calls.append(dict(api="get_orders"))
        return [dict(o) for o in self.orders.values()]

    def get_position(self, account_id=None):
        """查询持仓替身（Q5 query.py:1163：get_position(account_id=None) → list[dict]）。"""
        self.calls.append(dict(api="get_position", account_id=account_id))
        return [dict(p) for p in self.positions.values()]

    def get_cash(self, account_id=None):
        """查询资金替身（Q5 query.py:1146：get_cash(account_id=None) → 单个 dict）。"""
        self.calls.append(dict(api="get_cash", account_id=account_id))
        return dict(self.cash)

    # ---------------------------------------------------------- get_history_symbol
    def get_history_symbol(self, symbol, start_date="", end_date="", df=False):
        """证券基本信息替身（Q7 ds_instrument.py:118：单数单标的）。

        注入优先：self.symbol_info 命中即按注入返回（None 值=缺字段，测回退分支）；
        未命中按 pre_close=10.0 + 板位档位（创板科创 20%/主板 10%）自算上下限——
        自算只为让「未注入也有确定返回」，档位真值由消费方 limit_down_price 测。
        """
        self.calls.append(dict(api="get_history_symbol", symbol=symbol,
                               start_date=str(start_date), end_date=str(end_date), df=df))
        if self.raise_on_symbol_info:
            raise RuntimeError('{"status": 1100, "message": "模拟证券信息查询失败（终端未连接）"}')
        info = self.symbol_info.get(symbol)
        if info is None:
            code = symbol.partition(".")[2]
            rate = 0.20 if code.startswith(("300", "301", "688", "689")) else 0.10
            pre = 10.0
            info = {"pre_close": pre, "lower_limit": round(pre * (1 - rate), 2),
                    "upper_limit": round(pre * (1 + rate), 2), "is_st": False}
        day = str(end_date or start_date or "2026-08-21")
        try:
            trade_date = datetime.strptime(day, "%Y-%m-%d")
        except ValueError:
            trade_date = day                      # 非法日期透传（真身是 datetime，此处不强塑）
        row = {"symbol": symbol, "sec_name": "FAKE", "exchange": symbol.partition(".")[0],
               "trade_date": trade_date, "pre_close": info.get("pre_close"),
               "upper_limit": info.get("upper_limit"), "lower_limit": info.get("lower_limit"),
               "is_st": bool(info.get("is_st", False)),
               "is_suspended": False, "is_adjusted": False}
        return pd.DataFrame([row]) if df else [row]

    # ------------------------------------------------------------------ fill_order
    def fill_order(self, cl_ord_id, volume=None, price=None):
        """测试注入成交（替身专用辅助面，真 SDK 无此方法）。

        推进口径：filled_volume/amount/vwap 累加；status 按是否打满 volume 落 2（部成）
        或 3（已成）；柜台持仓同笔推进（买向加仓重算 vwap、卖向减仓）——对账类测试
        （absorb_reality）靠它与 get_position 的自洽闭环构造「柜台实况」。
        """
        o = self.orders[cl_ord_id]
        remaining = o["volume"] - o["filled_volume"]
        add = int(volume if volume is not None else remaining)
        add = max(0, min(add, remaining))         # 防御：不超委托量（柜台不会超成交）
        px = float(price if price is not None else o["price"])
        if add > 0:
            o["filled_volume"] += add
            o["filled_amount"] += add * px
            o["filled_vwap"] = o["filled_amount"] / o["filled_volume"]
            o["status"] = (ORDER_STATUS["Filled"] if o["filled_volume"] >= o["volume"]
                           else ORDER_STATUS["PartiallyFilled"])
            sym = o["symbol"]
            if o["side"] == OrderSide_Buy:
                p = self.positions.setdefault(sym, {
                    "account_id": o["account_id"], "symbol": sym, "side": 1,
                    "volume": 0, "volume_today": 0, "vwap": 0.0, "amount": 0.0,
                    "available": 0, "available_now": 0, "market_value": 0.0})
                new_vol = p["volume"] + add
                p["vwap"] = ((p["vwap"] * p["volume"] + add * px) / new_vol) if new_vol else 0.0
                p["volume"] = new_vol
                p["available"] = new_vol          # 简化：不模拟 T+1 可用冻结
                p["available_now"] = new_vol
                p["volume_today"] = new_vol
                p["amount"] = new_vol * px
                p["market_value"] = new_vol * px
            else:
                p = self.positions.get(sym)
                if p:
                    p["volume"] = max(0, p["volume"] - add)
                    p["available"] = min(p["available"], p["volume"])
                    p["available_now"] = p["available"]
        o["updated_at"] = datetime.now()
        return dict(o)

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
