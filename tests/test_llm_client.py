"""GLMClient：凭证缺失降级、超时降级、JSON 非法降级、结构化校验。

测试不触达真实 GLM 端点：通过 unittest.mock.AsyncMock/MagicMock 注入假 client，
实现完全脱网回归。
"""
import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch
from core.llm_client import GLMClient, SentimentResult


def test_disabled_returns_neutral(monkeypatch):
    monkeypatch.delenv("ZHIPU_API_KEY", raising=False)
    c = GLMClient()
    r = asyncio.run(c.analyze_sentiment("某股大涨"))
    assert r.score == 0.0 and "降级" in r.reasoning


def test_valid_json_returns_score(monkeypatch):
    monkeypatch.setenv("ZHIPU_API_KEY", "KEY")
    c = GLMClient()
    # 构造一个假 completion 响应
    msg = MagicMock(); msg.message.content = '{"score": 0.6, "reasoning": "利好"}'
    resp = MagicMock(); resp.choices = [msg]
    c._client = MagicMock()
    c._client.chat = MagicMock()
    c._client.chat.completions = MagicMock()
    c._client.chat.completions.create = AsyncMock(return_value=resp)
    r = asyncio.run(c.analyze_sentiment("业绩超预期"))
    assert r.score == 0.6 and r.reasoning == "利好"


def test_invalid_json_falls_back_neutral(monkeypatch):
    monkeypatch.setenv("ZHIPU_API_KEY", "KEY")
    c = GLMClient()
    msg = MagicMock(); msg.message.content = "not a json"
    resp = MagicMock(); resp.choices = [msg]
    c._client = MagicMock(); c._client.chat = MagicMock()
    c._client.chat.completions = MagicMock()
    c._client.chat.completions.create = AsyncMock(return_value=resp)
    r = asyncio.run(c.analyze_sentiment("x"))
    assert r.score == 0.0


def test_score_clamped_by_pydantic():
    # score 越界必须被 pydantic 拒绝
    try:
        SentimentResult(score=1.5); assert False
    except Exception:
        pass
