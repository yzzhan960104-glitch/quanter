# -*- coding: utf-8 -*-
"""infra/llm —— LLM 外部依赖适配子包（ports & adapters）。

工厂 get_llm_client() 按 LLM_PROVIDER env 选实现（默认 glm）。
未来切 Claude Code：仅新增 infra/llm/claude.py + 下方加一分支，调用方零改动。
"""
import os

from infra.llm.base import LLMClient, LLMConfigError  # noqa: F401  re-export 端口


def get_llm_client() -> LLMClient:
    """按 LLM_PROVIDER env 选 LLM 实现（默认 glm）。

    切换供应商：新增 infra/llm/<provider>.py 实现类 + 此处加分支。
    """
    provider = os.getenv("LLM_PROVIDER", "glm").lower()
    if provider == "glm":
        from infra.llm.glm import GlmClient
        return GlmClient()
    raise ValueError(f"未知 LLM_PROVIDER={provider!r}（当前仅支持 'glm'）")


__all__ = ["LLMClient", "LLMConfigError", "get_llm_client"]
