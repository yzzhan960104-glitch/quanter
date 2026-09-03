# -*- coding: utf-8 -*-
"""infra/llm/glm.py —— GlmClient：z.ai GLM 实现（OpenAI Chat 兼容协议）。

协议根因（2026-09-03 官方文档实锤 + 全天实测复盘）：
- 凭证是 GLM Coding Plan 订阅——官方文档明文「订阅过 Coding Plan（含过期）
  的 key 目前只能经 OpenAI Chat Completion 兼容协议访问模型 API」。
- 此前走 Anthropic 兼容端点：短请求碰巧能过、长请求（1.3k+ 字归因 prompt）
  长时间静默后超时——Anthropic 端点对 Coding Plan key 的支持不完整，
  一直误判为「端点拥塞」，实为协议路径不对。
- 现默认 https://api.z.ai/api/paas/v4/chat/completions（Bearer 鉴权，
  官方对 Coding Plan 的正道）；GLM_PROTOCOL=anthropic 可切回旧路径（保险）。

流式与思考分离（官方 Deep Thinking 文档口径）：
- 流式 delta 双通道：delta.content=正文 / delta.reasoning_content=思考——
  本客户端只收正文（思考段对调用方是黑盒，也避免静默期读超时：正文 delta
  到达即证明连接活着）。
- glm-5.3 思考不可禁用（thinking.type 仅 enabled）；reasoning_effort 三档
  low/high/max（max=默认，复杂任务推荐）。
"""
from __future__ import annotations

import http.client
import json
import os
import socket
import urllib.error
import urllib.request

from infra.llm.base import LLMConfigError

# 端点：默认 OpenAI Chat 兼容（Coding Plan 正道）；anthropic 分支留保险
_OPENAI_URL = "https://api.z.ai/api/paas/v4/chat/completions"
_ANTHROPIC_URL = "https://api.z.ai/api/anthropic/v1/messages"
# 包间隔上限：reasoning 模型思考期可能数分钟不发正文 delta，流式下 timeout
# 是「相邻包间隔」语义——300s 容忍思考段（归因实测 ~1-3 分钟思考+正文）
_LLM_TIMEOUT = 300


class _IPv4HTTPSConnection(http.client.HTTPSConnection):
    """强制 IPv4 的 HTTPS 连接（2026-09-04 实锤根因之二）。

    api.z.ai 的 AAAA 记录在本机 IPv6 出口黑洞：getaddrinfo 返回 v6 在前，
    urllib/httpx 按序连接挂在 v6 的 TLS 握手（SSL handshake timed out），
    而 curl 靠 Happy Eyeballs 竞速秒切 v4——表现为「curl 通 Python 挂」的
    间歇性假拥塞。作用域化修复：只对本 client 的连接强制 AF_INET。
    """

    def connect(self):                            # noqa: D102（覆写父类）
        af, st, proto, _, sa = socket.getaddrinfo(
            self.host, self.port, socket.AF_INET,
            socket.SOCK_STREAM, socket.IPPROTO_TCP)[0]
        s = socket.socket(af, st, proto)
        s.settimeout(self.timeout)
        s.connect(sa)
        self.sock = self._context.wrap_socket(s, server_hostname=self.host)


class _IPv4HTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_IPv4HTTPSConnection, req)


def _open(req, timeout: float):
    """带 IPv4 强制与可读错误翻译的 urlopen。"""
    try:
        return urllib.request.build_opener(_IPv4HTTPSHandler()).open(
            req, timeout=timeout)
    except urllib.error.HTTPError as e:
        if e.code == 429:
            body = e.read().decode("utf-8", "replace")
            if "1113" in body:
                # 智谱/z.ai 余额族错误码（glm.py 历史注释同源：08 月曾因
                # bigmodel 1113 切 z.ai——本次是 Coding Plan 订阅额度耗尽）
                raise RuntimeError(
                    "GLM 额度耗尽（code 1113：余额/资源包不足）——Coding Plan "
                    "订阅额度被耗尽或到期，需充值或等额度周期重置；非代码问题") from e
        raise


class GlmClient:
    """GLM（z.ai）LLM 实现。凭证/模型/协议在构造时从 env 读入并持有。"""

    def __init__(self) -> None:
        # 凭证双 fallback（GLM_API_KEY 优先，兼容历史 ZHIPU_API_KEY 命名）
        self._api_key = os.getenv("GLM_API_KEY") or os.getenv("ZHIPU_API_KEY")
        self._model = os.getenv("GLM_MODEL", "glm-4")
        self._protocol = os.getenv("GLM_PROTOCOL", "openai").lower()

    def with_model(self, model: str) -> "GlmClient":
        """同凭证同配置、换模型名的变体（降级兜底用，如 5.3→5.3-flash）。"""
        clone = GlmClient.__new__(GlmClient)
        clone._api_key = self._api_key
        clone._model = model
        clone._protocol = self._protocol
        return clone

    def call(self, prompt: str, *, max_tokens: int = 4096,
             temperature: float = 0.3,
             thinking_budget: int | None = None,
             reasoning_effort: str | None = None) -> str:
        """调 GLM 返回模型正文文本。凭证缺失抛 LLMConfigError，网络异常向上抛。

        thinking_budget：Anthropic 协议 thinking.budget_tokens（计入 max_tokens）
        ——GLM-5.x 上不约束思考墙钟时长，仅 anthropic 分支消费。
        reasoning_effort：GLM-5.3+ 思考深度档（low/high/max，默认 max）。
        流式为两协议共用：思考期静默的读超时问题靠正文 delta 活性消解。
        """
        if not self._api_key:
            raise LLMConfigError("GLM_API_KEY / ZHIPU_API_KEY 未配置")
        if self._protocol == "anthropic":
            return self._call_anthropic(prompt, max_tokens=max_tokens,
                                        temperature=temperature,
                                        thinking_budget=thinking_budget,
                                        reasoning_effort=reasoning_effort)
        body_d: dict = {
            "model": self._model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "stream": True,
        }
        if reasoning_effort is not None:
            body_d["thinking"] = {"type": "enabled"}
            body_d["reasoning_effort"] = reasoning_effort
        req = urllib.request.Request(_OPENAI_URL,
                                     data=json.dumps(body_d).encode("utf-8"),
                                     method="POST")
        req.add_header("Authorization", f"Bearer {self._api_key}")
        req.add_header("Content-Type", "application/json")
        chunks: list[str] = []
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
                # OpenAI 流式：choices[0].delta.content=正文（思考在
                # delta.reasoning_content，弃——429/错误事件也走 choices 结构）
                choices = evt.get("choices") or []
                if choices and isinstance(choices[0], dict):
                    d = choices[0].get("delta") or {}
                    if d.get("content"):
                        chunks.append(str(d["content"]))
        if not chunks:
            raise RuntimeError("GLM 流式响应无正文 delta（思考耗尽或异常终止）")
        return "".join(chunks)

    # ─────────────── Anthropic 兼容分支（GLM_PROTOCOL=anthropic 保险路径） ───────────────

    def _call_anthropic(self, prompt: str, *, max_tokens: int,
                        temperature: float, thinking_budget: int | None,
                        reasoning_effort: str | None) -> str:
        body_d: dict = {
            "model": self._model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "stream": True,
        }
        if thinking_budget is not None:
            body_d["thinking"] = {"type": "enabled",
                                  "budget_tokens": thinking_budget}
        if reasoning_effort is not None:
            body_d["reasoning_effort"] = reasoning_effort
        req = urllib.request.Request(_ANTHROPIC_URL,
                                     data=json.dumps(body_d).encode("utf-8"),
                                     method="POST")
        req.add_header("x-api-key", self._api_key)
        req.add_header("Authorization", f"Bearer {self._api_key}")
        req.add_header("Content-Type", "application/json")
        req.add_header("anthropic-version", "2023-06-01")
        chunks: list[str] = []
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
                delta = evt.get("delta") or {}
                if evt.get("type") == "content_block_delta" \
                        and delta.get("type") == "text_delta":
                    chunks.append(str(delta.get("text") or ""))
        if not chunks:
            raise RuntimeError("GLM 流式响应无 text delta（思考耗尽或异常终止）")
        return "".join(chunks)
