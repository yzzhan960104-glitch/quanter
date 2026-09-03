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
# 流式下 timeout=相邻 SSE 包的间隔上限。z.ai glm-5.3 对长 prompt 的思考期
# **完全静默**（thinking delta 也不发，实测 2k 字归因 prompt 静默 150s+，
# budget 参数压不住思考时长）——间隔容忍必须 > 最长思考段；300s 下单次
# 归因 ~3 分钟、10 只串行 ~30 分钟，16:15 cron 在 18:00 管道前收口
_LLM_TIMEOUT = 300


class GlmClient:
    """GLM（z.ai）LLM 实现。凭证/模型在构造时从 env 读入并持有。"""

    def __init__(self) -> None:
        # 凭证双 fallback（GLM_API_KEY 优先，兼容历史 ZHIPU_API_KEY 命名）
        self._api_key = os.getenv("GLM_API_KEY") or os.getenv("ZHIPU_API_KEY")
        self._model = os.getenv("GLM_MODEL", "glm-4")

    def with_model(self, model: str) -> "GlmClient":
        """同凭证同配置、换模型名的变体（降级兜底用，如 5.3→5.3-flash）。"""
        clone = GlmClient.__new__(GlmClient)
        clone._api_key = self._api_key
        clone._model = model
        return clone

    def call(self, prompt: str, *, max_tokens: int = 4096,
             temperature: float = 0.3,
             thinking_budget: int | None = None,
             reasoning_effort: str | None = None) -> str:
        """调 GLM 返回模型文本。凭证缺失抛 LLMConfigError，网络异常向上抛。

        thinking_budget：Anthropic 协议 thinking.budget_tokens（计入
        max_tokens）——GLM-5.x 上仅约束思考 token 产出，不约束思考墙钟时长。
        reasoning_effort：GLM-5.3+ 的思考深度档（low/high/max，max=默认），
        顶层字段（z.ai Anthropic 兼容端点实测透传支持）；深度分析用 max。
        两者独立，可单用；GLM-5.3 思考不可禁用（无 disabled）。
        """
        if not self._api_key:
            raise LLMConfigError("GLM_API_KEY / ZHIPU_API_KEY 未配置")
        body_d: dict = {
            "model": self._model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            # 流式（SSE）：reasoning 模型长思考期间服务端数分钟不发一个字节，
            # 非流式下客户端读超时必炸；流式逐 delta 发包，timeout 退化为
            # 「包间隔」语义，思考再长也扛住
            "stream": True,
        }
        if thinking_budget is not None:
            body_d["thinking"] = {"type": "enabled",
                                  "budget_tokens": thinking_budget}
        if reasoning_effort is not None:
            body_d["reasoning_effort"] = reasoning_effort
        body = json.dumps(body_d).encode("utf-8")
        req = urllib.request.Request(GLM_URL, data=body, method="POST")
        # 双投鉴权：z.ai AUTH_TOKEN 认 Bearer、标准 Anthropic 认 x-api-key
        req.add_header("x-api-key", self._api_key)
        req.add_header("Authorization", f"Bearer {self._api_key}")
        req.add_header("Content-Type", "application/json")
        req.add_header("anthropic-version", "2023-06-01")
        chunks: list[str] = []
        with urllib.request.urlopen(req, timeout=_LLM_TIMEOUT) as resp:
            for raw in resp:                          # SSE 行流
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload or payload == "[DONE]":
                    continue
                try:
                    evt = json.loads(payload)
                except ValueError:
                    continue
                delta = evt.get("delta") or {}
                if evt.get("type") == "content_block_delta" \
                        and delta.get("type") == "text_delta":
                    chunks.append(str(delta.get("text") or ""))
        if not chunks:
            raise RuntimeError("GLM 流式响应无 text delta（思考耗尽或异常终止）")
        return "".join(chunks)
