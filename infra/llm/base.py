# -*- coding: utf-8 -*-
"""infra/llm/base.py —— LLM 调用端口（外部依赖抽象层）。

设计哲学（ports & adapters / 策略模式）：
- LLM 是外部依赖（调 z.ai / 未来 Claude Code 等），归 infra/（与 notifier 同层）。
- LLMClient 是「端口」：业务语义接口（给 prompt 出文本），屏蔽供应商细节
  （凭证/端点/HTTP 内化到实现类）。
- 调用失败统一抛异常：LLMConfigError（凭证缺失）或网络异常（urllib 抛），
  由调用方（review_service / training_analyzer）捕获走各自降级——与原
  `_call_glm 异常向上抛、diagnose/analyze_round 捕获降级` 语义一致。
- 可扩展：未来切供应商仅新增 infra/llm/<provider>.py 实现类 + 工厂分支，
  调用方零改动。
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


class LLMConfigError(Exception):
    """LLM 配置缺失（凭证未配等）。调用方捕获后走降级，不应阻断业务。"""


@runtime_checkable
class LLMClient(Protocol):
    """LLM 调用端口：给 prompt，出模型文本。

    实现类（GlmClient / 未来 ClaudeClient）自行从 env 读凭证与端点，
    调用成功返回模型文本字符串，失败抛 LLMConfigError 或网络异常。
    """

    def call(self, prompt: str, *, max_tokens: int = 4096,
             temperature: float = 0.3) -> str: ...
