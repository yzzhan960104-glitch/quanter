"""AlphaVantageClient：双装饰器限流+熔断、洗净、safe 兜底空降级。"""
import asyncio
import pandas as pd
import httpx
from data.clients.alpha_vantage_client import (
    AlphaVantageClient, av_breaker, _EMPTY_TY, DataFetchError, CircuitOpenError)


def _fake_response(data):
    class _R:
        def raise_for_status(self): pass
        def json(self): return data
    return _R()


class _FakeCtx:
    """极简 async context manager 替身，避免触网。

    注意：真实 httpx.AsyncClient.get(...) 返回 Coroutine（需 await）。
    brief 草案中 get 为同步 def 直接返回 _resp，会与实现里的 `await client.get(...)`
    类型不符（_R 不可 await，抛 TypeError: object _R can't be used in 'await' expression）。
    故将 get 声明为 async，使其返回值可被 await，精确对齐真实 httpx 语义。
    """
    def __init__(self, resp): self._resp = resp
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def get(self, *a, **k): return self._resp


def test_cleanse_treasury(monkeypatch):
    client = AlphaVantageClient(api_key="KEY")
    payload = {"data": [{"date": "2024-01-03", "value": "4.02"},
                        {"date": "2024-01-02", "value": "4.00"}]}
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _FakeCtx(_fake_response(payload)))
    df = asyncio.run(client.get_treasury_yield("10Y"))
    assert list(df.columns) == ["10Y"]
    assert df["10Y"].iloc[0] == 4.00  # 排序后首行应为较早日期


def test_safe_returns_empty_on_failure(monkeypatch):
    """API 报错时 get_treasury_yield_safe 必须返回空 DF，绝不抛。"""
    client = AlphaVantageClient(api_key="KEY")
    class _BoomCtx:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, *a, **k):
            raise httpx.ConnectError("down")
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _BoomCtx())
    df = asyncio.run(client.get_treasury_yield_safe("10Y"))
    assert df.empty
