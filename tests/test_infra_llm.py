# -*- coding: utf-8 -*-
"""infra/llm 端口与工厂契约：Protocol 形状 + 工厂按 env 选实现 + 凭证缺失抛 LLMConfigError。"""
import os
import pytest
from unittest.mock import patch


def test_llm_client_protocol_call_signature():
    """LLMClient 实现 .call(prompt) -> str（业务语义接口，屏蔽供应商）。"""
    from infra.llm.base import LLMClient

    class _Fake:
        def call(self, prompt: str, *, max_tokens: int = 4096, temperature: float = 0.3) -> str:
            return f"echo:{prompt}"

    assert isinstance(_Fake(), LLMClient)          # runtime_checkable Protocol
    assert _Fake().call("hi") == "echo:hi"


def test_get_llm_client_default_glm(monkeypatch):
    """LLM_PROVIDER 缺省 → 返回 GlmClient 实例。"""
    monkeypatch.setenv("GLM_API_KEY", "fake")
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    from infra.llm import get_llm_client
    from infra.llm.glm import GlmClient
    assert isinstance(get_llm_client(), GlmClient)


def test_glm_client_missing_creds_raises_config_error(monkeypatch):
    """凭证缺失 → call() 抛 LLMConfigError（调用方捕获走降级）。"""
    monkeypatch.delenv("GLM_API_KEY", raising=False)
    monkeypatch.delenv("ZHIPU_API_KEY", raising=False)
    from infra.llm.glm import GlmClient
    from infra.llm.base import LLMConfigError
    with pytest.raises(LLMConfigError):
        GlmClient().call("any prompt")
