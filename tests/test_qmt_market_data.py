# -*- coding: utf-8 -*-
"""xtdata 行情封装单测：mock xtdata，覆盖可用/不可用/异常/空数据四路径。"""
import asyncio
import types

import pytest


def test_get_quote_unavailable(monkeypatch):
    """xtdata 不可用 → None（调用方须容忍）。"""
    from trading import qmt_market_data as md
    monkeypatch.setattr(md, "_XTDATA_AVAILABLE", False)

    async def run():
        r = await md.get_quote("600000.SH")
        assert r is None
    asyncio.run(run())


def test_get_quote_ok(monkeypatch):
    """xtdata 可用且返数据 → 返回单标的快照 dict。"""
    from trading import qmt_market_data as md
    fake = types.SimpleNamespace(
        get_full_tick=lambda codes: {"600000.SH": {"last_price": 10.5, "high_limit": 11.5, "low_limit": 9.5}}
    )
    monkeypatch.setattr(md, "xtdata", fake)
    monkeypatch.setattr(md, "_XTDATA_AVAILABLE", True)

    async def run():
        r = await md.get_quote("600000.SH")
        assert r is not None
        assert r["last_price"] == 10.5
    asyncio.run(run())


def test_get_quote_exception_returns_none(monkeypatch):
    """xtdata 抛异常 → 捕获返 None（绝不冒泡到调用方）。"""
    from trading import qmt_market_data as md

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
    from trading import qmt_market_data as md
    fake = types.SimpleNamespace(get_full_tick=lambda codes: {})
    monkeypatch.setattr(md, "xtdata", fake)
    monkeypatch.setattr(md, "_XTDATA_AVAILABLE", True)

    async def run():
        r = await md.get_quote("600000.SH")
        assert r is None
    asyncio.run(run())
