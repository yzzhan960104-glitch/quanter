# -*- coding: utf-8 -*-
"""xtdata 行情封装单测：mock xtdata，覆盖可用/不可用/异常/空数据四路径。"""
import asyncio
import types

import pytest


def test_get_quote_unavailable(monkeypatch):
    """xtdata 不可用 → None（调用方须容忍）。"""
    # Layer2 阶段3：真身迁 broker.qmt_quote；patch 内部全局须指真身模块（垫片副本无效）。
    from broker import qmt_quote as md
    monkeypatch.setattr(md, "_XTDATA_AVAILABLE", False)

    async def run():
        r = await md.get_quote("600000.SH")
        assert r is None
    asyncio.run(run())


def test_get_quote_ok(monkeypatch):
    """xtdata 可用且返数据 → 返回单标的快照 dict。"""
    # Layer2 阶段3：真身迁 broker.qmt_quote；patch 内部全局须指真身模块（垫片副本无效）。
    from broker import qmt_quote as md
    # xtquant 真实契约：get_full_tick 返驼峰字段（lastPrice），涨跌停由 get_instrument_detail 提供。
    md._LIMIT_PRICE_CACHE.clear()
    fake = types.SimpleNamespace(
        get_full_tick=lambda codes: {"600000.SH": {"lastPrice": 10.5, "lastClose": 9.5}},
        get_instrument_detail=lambda code: {"UpStopPrice": 11.5, "DownStopPrice": 9.5},
    )
    monkeypatch.setattr(md, "xtdata", fake)
    monkeypatch.setattr(md, "_XTDATA_AVAILABLE", True)

    async def run():
        r = await md.get_quote("600000.SH")
        assert r is not None
        assert r["last_price"] == 10.5  # 驼峰 lastPrice 归一化 → last_price
        assert r["high_limit"] == 11.5  # instrument_detail 涨跌停注入
        assert r["low_limit"] == 9.5
    asyncio.run(run())


def test_get_quote_exception_returns_none(monkeypatch):
    """xtdata 抛异常 → 捕获返 None（绝不冒泡到调用方）。"""
    # Layer2 阶段3：真身迁 broker.qmt_quote；patch 内部全局须指真身模块（垫片副本无效）。
    from broker import qmt_quote as md

    def boom(codes):
        raise RuntimeError("C++ 内部错误")
    fake = types.SimpleNamespace(get_full_tick=boom)
    monkeypatch.setattr(md, "xtdata", fake)
    monkeypatch.setattr(md, "_XTDATA_AVAILABLE", True)

    async def run():
        r = await md.get_quote("600000.SH")
        assert r is None
    asyncio.run(run())


def test_get_quote_empty_returns_none(monkeypatch):
    """get_full_tick 返空 dict 或缺该标的 → None。"""
    # Layer2 阶段3：真身迁 broker.qmt_quote；patch 内部全局须指真身模块（垫片副本无效）。
    from broker import qmt_quote as md
    fake = types.SimpleNamespace(get_full_tick=lambda codes: {})
    monkeypatch.setattr(md, "xtdata", fake)
    monkeypatch.setattr(md, "_XTDATA_AVAILABLE", True)

    async def run():
        r = await md.get_quote("600000.SH")
        assert r is None
    asyncio.run(run())
