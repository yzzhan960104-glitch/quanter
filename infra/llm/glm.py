# -*- coding: utf-8 -*-
"""infra/llm/glm.py —— GlmClient：z.ai Anthropic 兼容端点实现。

封装原 server.services.review_service._call_glm 的 urllib 逻辑（逻辑零改动）：
- 端点 GLM_URL = z.ai /api/anthropic/v1/messages（复用「coding plan」订阅额度，
  非智谱按量余额池——后者已 code 1113 耗尽）。
- 双投鉴权 x-api-key + Authorization: Bearer（兼容 Anthropic 与 z.ai 两套约定）。
- anthropic-version 头为协议必填（2023-06-01）。
凭证/模型从 env 读（GLM_API_KEY/ZHIPU_API_KEY/GLM_MODEL），绝不硬编码。
"""
from __future__ import annotations

import json
import os
import urllib.request

from infra.llm.base import LLMConfigError

# z.ai Anthropic Messages 兼容端点（同原 review_service.GLM_URL，逐字搬移）
GLM_URL = "https://api.z.ai/api/anthropic/v1/messages"
_LLM_TIMEOUT = 60


class GlmClient:
    """GLM（z.ai）LLM 实现。凭证/模型在构造时从 env 读入并持有。"""

    def __init__(self) -> None:
        # 凭证双 fallback（GLM_API_KEY 优先，兼容历史 ZHIPU_API_KEY 命名）
        self._api_key = os.getenv("GLM_API_KEY") or os.getenv("ZHIPU_API_KEY")
        self._model = os.getenv("GLM_MODEL", "glm-4")

    def call(self, prompt: str, *, max_tokens: int = 4096,
             temperature: float = 0.3) -> str:
        """调 GLM 返回模型文本。凭证缺失抛 LLMConfigError，网络异常向上抛。"""
        if not self._api_key:
            raise LLMConfigError("GLM_API_KEY / ZHIPU_API_KEY 未配置")
        body = json.dumps({
            "model": self._model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
        }).encode("utf-8")
        req = urllib.request.Request(GLM_URL, data=body, method="POST")
        # 双投鉴权：z.ai AUTH_TOKEN 认 Bearer、标准 Anthropic 认 x-api-key
        req.add_header("x-api-key", self._api_key)
        req.add_header("Authorization", f"Bearer {self._api_key}")
        req.add_header("Content-Type", "application/json")
        req.add_header("anthropic-version", "2023-06-01")
        with urllib.request.urlopen(req, timeout=_LLM_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        # Anthropic Messages 响应：content=[{type:"text", text:"..."}]，取首块文本
        return data["content"][0]["text"]
