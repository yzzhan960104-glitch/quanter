# -*- coding: utf-8 -*-
"""research/committee/llm_tools.py —— GlmToolsSession：anthropic 协议 tools 回路。

在 infra.llm.glm 之上补 tools 能力（GlmClient 本体是无状态单轮 prompt→text，
刻意保持薄；本模块是**委员会专用**的有状态会话，不回流 infra——避免给观测层
通用端口引入会话语义）。协议事实（09-06 diag/glm_tools_probe 实证）：
  - anthropic 端点 https://api.z.ai/api/anthropic/v1/messages 支持 tools；
  - 流式 delta 双通道 content/text_delta；tool_use 块经 content_block_start
    （含 name/id）+ input_json_delta（partial_json）到达；
  - IPv4 强制复用 infra.llm.glm._open（api.z.ai AAAA 黑洞）。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Callable

from infra.llm.glm import _ANTHROPIC_URL, _open

_LLM_TIMEOUT = 300          # 相邻 SSE 包间隔上限（思考期容忍，与 GlmClient 同口径）
_MAX_NET_RETRIES = 2


class ToolCallError(Exception):
    """工具执行异常（消息回填给模型，不炸会话）。"""


class GlmToolsSession:
    """单角色多轮会话：system + messages 历史 + 工具注册表。

    run() 驱动 ReAct 回路直到模型给出无 tool_use 的最终文本（或触达轮次上限，
    届时把已收文本与未答工具调用如实返回，由调用方决定降级）。
    """

    def __init__(self, system: str, tools: dict[str, tuple[dict, Callable]],
                 model: str | None = None, max_tool_rounds: int = 6) -> None:
        import os
        self._key = os.getenv("GLM_API_KEY") or os.getenv("ZHIPU_API_KEY")
        if not self._key:
            from infra.llm.base import LLMConfigError
            raise LLMConfigError("GLM_API_KEY / ZHIPU_API_KEY 未配置")
        self._model = model or os.getenv("GLM_MODEL", "glm-5.3")
        self._system = system
        self._tools = tools                      # name -> (schema, fn)
        self._max_rounds = max_tool_rounds
        self.messages: list[dict] = []           # anthropic content-block 消息史
        self.call_count = 0                      # 本会话 LLM 调用次数（配额台账）
        self.tool_log: list[dict] = []           # 工具调用审计（name/input/结果摘要）

    # ─────────────────────── 单次流式调用（含重试） ───────────────────────

    def _invoke(self, max_tokens: int, reasoning_effort: str | None) -> list[dict]:
        """一次 messages 调用 → content blocks（text/tool_use 已拼装）。"""
        body = {
            "model": self._model,
            "max_tokens": max_tokens,
            "system": self._system,
            "messages": self.messages,
            "stream": True,
        }
        if self._tools:
            body["tools"] = [schema for schema, _ in self._tools.values()]
        if reasoning_effort:
            body["reasoning_effort"] = reasoning_effort
        req = urllib.request.Request(
            _ANTHROPIC_URL, data=json.dumps(body).encode("utf-8"), method="POST")
        req.add_header("x-api-key", self._key)
        req.add_header("Authorization", f"Bearer {self._key}")
        req.add_header("Content-Type", "application/json")
        req.add_header("anthropic-version", "2023-06-01")

        last_err: Exception | None = None
        for attempt in range(_MAX_NET_RETRIES + 1):
            try:
                return self._parse_stream(req)
            except RuntimeError as e:            # 额度类错误不重试（glm._open 翻译过）
                if "1113" in str(e) or "额度" in str(e):
                    raise
                last_err = e
            except (urllib.error.URLError, OSError, TimeoutError) as e:
                last_err = e
            if attempt < _MAX_NET_RETRIES:
                time.sleep(3 * (attempt + 1))
        raise RuntimeError(f"LLM 调用重试耗尽：{last_err}") from last_err

    def _parse_stream(self, req) -> list[dict]:
        """SSE → content blocks（text 拼接 / tool_use json 拼装 / thinking 丢弃）。"""
        blocks: dict[int, dict] = {}
        with _open(req, _LLM_TIMEOUT) as resp:
            for raw in resp:
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
                t = evt.get("type", "")
                if t == "content_block_start":
                    cb = evt.get("content_block") or {}
                    blocks[evt.get("index", len(blocks))] = {
                        "type": cb.get("type"), "text": "",
                        "id": cb.get("id"), "name": cb.get("name"), "input_json": ""}
                elif t == "content_block_delta":
                    i = evt.get("index")
                    d = evt.get("delta") or {}
                    b = blocks.get(i)
                    if b is None:
                        continue
                    if d.get("type") == "text_delta":
                        b["text"] += str(d.get("text") or "")
                    elif d.get("type") == "input_json_delta":
                        b["input_json"] += str(d.get("partial_json") or "")
                elif t == "error":
                    raise RuntimeError(f"端点错误事件：{str(evt.get('error'))[:300]}")
        out: list[dict] = []
        for i in sorted(blocks):
            b = blocks[i]
            if b["type"] == "text" and b["text"].strip():
                out.append({"type": "text", "text": b["text"]})
            elif b["type"] == "tool_use":
                try:
                    args = json.loads(b["input_json"] or "{}")
                except ValueError:
                    args = {"_parse_error": b["input_json"][:200]}
                out.append({"type": "tool_use", "id": b["id"],
                            "name": b["name"], "input": args})
        return out

    # ─────────────────────── 工具执行 ───────────────────────

    def _exec_tool(self, name: str, args: dict) -> str:
        if name not in self._tools:
            return f"ERROR: 未知工具 {name!r}（可用：{sorted(self._tools)}）"
        _, fn = self._tools[name]
        try:
            res = str(fn(**args)) if args else str(fn())
        except ToolCallError as e:
            res = f"ERROR: {e}"
        except TypeError as e:
            res = f"ERROR: 参数不匹配：{e}"
        except Exception as e:  # noqa: BLE001 —— 工具层全量兜底，回填模型自纠
            res = f"ERROR: {type(e).__name__}: {e}"[:1200]
        res = res[:2600]                          # LLM 上下文友好截断
        self.tool_log.append({"name": name, "input": args,
                              "result_head": res[:300]})
        return res

    # ─────────────────────── 主回路 ───────────────────────

    def run(self, user_msg: str, *, max_tokens: int = 8192,
            reasoning_effort: str | None = "high",
            final_reasoning_effort: str | None = None) -> str:
        """注入 user 消息并驱动到最终文本。返回最终 text（可能为空=异常形态）。"""
        self.messages.append({"role": "user",
                              "content": [{"type": "text", "text": user_msg}]})
        texts: list[str] = []
        for round_i in range(self._max_rounds + 1):
            self.call_count += 1
            effort = reasoning_effort if round_i < self._max_rounds \
                else (final_reasoning_effort or reasoning_effort)
            blocks = self._invoke(max_tokens=max_tokens, reasoning_effort=effort)
            if not blocks:
                break
            self.messages.append({"role": "assistant",
                                  "content": [dict(b) for b in blocks]})
            tool_uses = [b for b in blocks if b["type"] == "tool_use"]
            texts = [b["text"] for b in blocks if b["type"] == "text"] or texts
            if not tool_uses:
                return "\n".join(texts).strip()
            results = []
            for tu in tool_uses:
                results.append({"type": "tool_result",
                                "tool_use_id": tu["id"],
                                "content": self._exec_tool(tu["name"], tu["input"])})
            self.messages.append({"role": "user", "content": results})
        # 轮次耗尽：如实返回已收文本（调用方拿 tool_log 判断降级）
        return "\n".join(texts).strip()
