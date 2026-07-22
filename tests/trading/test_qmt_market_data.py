# -*- coding: utf-8 -*-
"""qmt_market_data 批量行情单测（Task 3 · get_quotes + get_quote 回归）。

测试边界（Karpathy 极简 + TDD）：
- 不真连 miniQMT / xtdata：monkeypatch ``qmt_market_data.xtdata`` 注入假实例 +
  ``_XTDATA_AVAILABLE`` 开关，构造「正常批量 / 缺失标的 / xtdata 不可用 / get_quote 委托」
  四类场景；
- 断言 ``get_full_tick`` 原生 list 透传（批量调用 1 次），缺失标的值 None（调用方按 None 降级）；
- 断言 ``get_quote`` 单只便利签名内部委托 ``get_quotes([symbol])[symbol]``（DRY）。
"""
from __future__ import annotations

import asyncio

from trading import qmt_market_data


# ============================================================================
# 1. 批量取价：原生 list 透传 + 正常多只返 {symbol: tick}
# ============================================================================
def test_get_quotes_batch_returns_dict(monkeypatch):
    """批量取多只：get_full_tick 返多只 dict → get_quotes 返 {symbol: tick}。

    Why 断言透传 list：xtdata.get_full_tick 原生支持 list 入参（xtdata.html 契约），
    若实现错误地拆成多次单只调用，线程池调用数 N→1 优化失效（本 task 核心目标）。
    """
    fake_tick = {
        "600000.SH": {"last_price": 10.5, "high_limit": 11.5, "low_limit": 9.5},
        "000001.SZ": {"last_price": 15.2, "high_limit": 16.7, "low_limit": 13.7},
    }
    monkeypatch.setattr(qmt_market_data, "_XTDATA_AVAILABLE", True)
    captured: dict = {}

    class _FakeXtdata:
        def get_full_tick(self, symbols):
            captured["symbols"] = symbols
            return fake_tick

    monkeypatch.setattr(qmt_market_data, "xtdata", _FakeXtdata())

    result = asyncio.run(qmt_market_data.get_quotes(["600000.SH", "000001.SZ"]))

    # 原生 list 透传（核心优化点：1 次调用而非 N 次）
    assert captured["symbols"] == ["600000.SH", "000001.SZ"]
    assert set(result.keys()) == {"600000.SH", "000001.SZ"}
    assert result["600000.SH"]["last_price"] == 10.5
    assert result["000001.SZ"]["last_price"] == 15.2


# ============================================================================
# 2. 缺失标的：get_full_tick 不含的 symbol 值 None（调用方按 None 降级）
# ============================================================================
def test_get_quotes_missing_symbol_is_none(monkeypatch):
    """get_full_tick 返 dict 不含的标的 → 该 symbol 值 None。

    Why：颈线法 stop_loss_monitor 遇到停牌 / 退市 / 代码错误时，
    xtdata.get_full_tick 返回 dict 不含该 symbol，实现必须填 None（绝不漏键），
    否则下游 ``quotes[sym]`` 抛 KeyError 阻断整个止损监控循环（致命）。
    """
    monkeypatch.setattr(qmt_market_data, "_XTDATA_AVAILABLE", True)

    class _FakeXtdata:
        def get_full_tick(self, symbols):
            return {"600000.SH": {"last_price": 10.5}}  # 缺 000001.SZ

    monkeypatch.setattr(qmt_market_data, "xtdata", _FakeXtdata())

    result = asyncio.run(qmt_market_data.get_quotes(["600000.SH", "000001.SZ"]))

    assert result["600000.SH"]["last_price"] == 10.5
    assert result["000001.SZ"] is None  # 缺失标的显式 None（不漏键）


# ============================================================================
# 3. xtdata 不可用：所有标的值 None（不抛，降级模式）
# ============================================================================
def test_get_quotes_xtdata_unavailable_returns_all_none(monkeypatch):
    """xtdata 不可用（_XTDATA_AVAILABLE=False）→ 所有标的值 None（不抛）。

    Why：CI / 开发环境无 xtquant 时 _XTDATA_AVAILABLE=False，
    必须返全 None dict（而非抛 ImportError）——risk_shield 据此跳过涨跌停关、
    stop_loss_monitor 据此跳过现价检查（降级不阻断）。
    """
    monkeypatch.setattr(qmt_market_data, "_XTDATA_AVAILABLE", False)

    result = asyncio.run(qmt_market_data.get_quotes(["600000.SH", "000001.SZ"]))

    assert result == {"600000.SH": None, "000001.SZ": None}


# ============================================================================
# 4. 空 list 入参：返空 dict（不出错，不调 xtdata）
# ============================================================================
def test_get_quotes_empty_symbols_returns_empty_dict(monkeypatch):
    """空 list → 返空 dict，不调 xtdata（无持仓即无行情查询）。"""
    xtdata_called = {"n": 0}

    class _FakeXtdata:
        def get_full_tick(self, symbols):
            xtdata_called["n"] += 1
            return {}

    monkeypatch.setattr(qmt_market_data, "_XTDATA_AVAILABLE", True)
    monkeypatch.setattr(qmt_market_data, "xtdata", _FakeXtdata())

    result = asyncio.run(qmt_market_data.get_quotes([]))

    assert result == {}
    assert xtdata_called["n"] == 0  # 空 list 短路，不调 xtdata


# ============================================================================
# 5. 异常：get_full_tick 抛异常 → 所有标的值 None（不阻断主路径）
# ============================================================================
def test_get_quotes_exception_returns_all_none(monkeypatch):
    """get_full_tick 抛异常 → 全 None（C++ 内部错误不阻断下单/查询主路径）。"""
    monkeypatch.setattr(qmt_market_data, "_XTDATA_AVAILABLE", True)

    class _FakeXtdata:
        def get_full_tick(self, symbols):
            raise RuntimeError("xtdata C++ 内部错误")

    monkeypatch.setattr(qmt_market_data, "xtdata", _FakeXtdata())

    result = asyncio.run(qmt_market_data.get_quotes(["600000.SH"]))

    assert result == {"600000.SH": None}  # 异常被吞，全 None


# ============================================================================
# 6. get_quote 委托：单只便利签名 → 内部走 get_quotes([symbol])[symbol]（DRY）
# ============================================================================
def test_get_quote_delegates_to_get_quotes(monkeypatch):
    """get_quote(symbol) → 内部委托 get_quotes([symbol])[symbol]（DRY，复用批量逻辑）。

    Why 委托：risk_shield 第9关涨跌停 / get_positions 市值富化等单只消费者
    无需改签名即可复用批量逻辑，消除两份并行实现（维护成本/一致性风险）。
    """
    monkeypatch.setattr(qmt_market_data, "_XTDATA_AVAILABLE", True)
    captured: dict = {}

    class _FakeXtdata:
        def get_full_tick(self, symbols):
            captured["symbols"] = symbols
            return {"600000.SH": {"last_price": 10.5}}

    monkeypatch.setattr(qmt_market_data, "xtdata", _FakeXtdata())

    result = asyncio.run(qmt_market_data.get_quote("600000.SH"))

    # 委托验证：底层以 list 形式调 get_full_tick（批量路径）
    assert captured["symbols"] == ["600000.SH"]
    assert result == {"last_price": 10.5}


def test_get_quote_missing_returns_none(monkeypatch):
    """get_quote 单只：标的缺失 → None（委托 get_quotes 的缺失语义）。"""
    monkeypatch.setattr(qmt_market_data, "_XTDATA_AVAILABLE", True)

    class _FakeXtdata:
        def get_full_tick(self, symbols):
            return {}  # 不含该标的

    monkeypatch.setattr(qmt_market_data, "xtdata", _FakeXtdata())

    result = asyncio.run(qmt_market_data.get_quote("999999.SH"))

    assert result is None
