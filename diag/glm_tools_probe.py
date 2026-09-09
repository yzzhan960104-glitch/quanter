# -*- coding: utf-8 -*-
"""diag/glm_tools_probe.py —— z.ai 双端点 tools 协议探测（2026-09-06）。

背景：infra/llm/glm.py 的 call() 是无状态单轮 prompt→text（body 无 tools 字段），
本账户从未走过函数调用协议。TradingAgents 分析师=ReAct bind_tools，需要端点支持
tool_use（anthropic 协议）/ tool_calls（openai 协议）。本探测各打一枪实证：
  1) anthropic 端点（吃 Coding Plan 配额）：带 tools 的流式调用，tool_choice=auto；
     若无 tool_use 则补一发强制 tool_choice 区分「不会发」vs「不想发」。
  2) openai 端点 paas/v4（按量池）：预期 429 code 1113（空池）；若通=池已充值。
判定只看 SSE 里是否出现 tool_use / tool_calls 结构（简单子串匹配，不解析语义）。
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

from infra.llm.glm import _ANTHROPIC_URL, _OPENAI_URL, _open  # 复用 IPv4 强制 opener

MODEL = os.getenv("GLM_MODEL", "glm-5.3")
KEY = os.getenv("GLM_API_KEY") or os.getenv("ZHIPU_API_KEY")

_TOOL_DESC = "查询某标的最近 N 个交易日的累计涨跌幅（百分比）"

def _anthropic_probe(force: bool) -> dict:
    tool = {
        "name": "query_price_change",
        "description": _TOOL_DESC,
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "标的代码，如 399006.SZ"},
                "days": {"type": "integer", "description": "回看交易日数"},
            },
            "required": ["symbol", "days"],
        },
    }
    body = {
        "model": MODEL,
        "max_tokens": 8192,
        "stream": True,
        "reasoning_effort": "low",  # 探测提速；语义上只看是否发 tool_use
        "messages": [{
            "role": "user",
            "content": "创业板指(399006.SZ)最近 5 个交易日累计涨跌幅是多少？"
                       "必须调用工具查询，不要凭记忆回答。",
        }],
        "tools": [tool],
        "tool_choice": {"type": "tool", "name": "query_price_change"} if force
                       else {"type": "auto"},
    }
    req = urllib.request.Request(_ANTHROPIC_URL,
                                 data=json.dumps(body).encode("utf-8"), method="POST")
    req.add_header("x-api-key", KEY)
    req.add_header("Authorization", f"Bearer {KEY}")
    req.add_header("Content-Type", "application/json")
    req.add_header("anthropic-version", "2023-06-01")
    out = {"saw_tool_use": False, "tool_name": None, "stop_reason": None,
           "text_len": 0, "err": None}
    t0 = time.time()
    try:
        with _open(req, 300) as resp:
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
                    if cb.get("type") == "tool_use":
                        out["saw_tool_use"] = True
                        out["tool_name"] = cb.get("name")
                elif t == "content_block_delta":
                    d = evt.get("delta") or {}
                    if d.get("type") == "text_delta":
                        out["text_len"] += len(str(d.get("text") or ""))
                elif t == "message_delta":
                    out["stop_reason"] = (evt.get("delta") or {}).get("stop_reason")
                elif t == "error":
                    out["err"] = str(evt.get("error"))[:300]
    except urllib.error.HTTPError as e:
        out["err"] = f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}"
    except Exception as e:  # noqa: BLE001 —— 探测脚本全量上报
        out["err"] = f"{type(e).__name__}: {e}"[:300]
    out["elapsed_s"] = round(time.time() - t0, 1)
    return out


def _openai_probe() -> dict:
    body = {
        "model": MODEL,
        "max_tokens": 4096,
        "stream": True,
        "thinking": {"type": "enabled"},
        "reasoning_effort": "low",
        "messages": [{
            "role": "user",
            "content": "上证指数最近 3 个交易日累计涨跌幅？必须调用工具查询。",
        }],
        "tools": [{
            "type": "function",
            "function": {
                "name": "query_price_change",
                "description": _TOOL_DESC,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string"},
                        "days": {"type": "integer"},
                    },
                    "required": ["symbol", "days"],
                },
            },
        }],
        "tool_choice": "auto",
    }
    req = urllib.request.Request(_OPENAI_URL,
                                 data=json.dumps(body).encode("utf-8"), method="POST")
    req.add_header("Authorization", f"Bearer {KEY}")
    req.add_header("Content-Type", "application/json")
    out = {"saw_tool_calls": False, "finish_reason": None, "text_len": 0, "err": None}
    t0 = time.time()
    try:
        with _open(req, 300) as resp:
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
                choices = evt.get("choices") or []
                if choices and isinstance(choices[0], dict):
                    c = choices[0]
                    if c.get("delta", {}).get("tool_calls"):
                        out["saw_tool_calls"] = True
                    if c.get("delta", {}).get("content"):
                        out["text_len"] += len(str(c["delta"]["content"]))
                    if c.get("finish_reason"):
                        out["finish_reason"] = c["finish_reason"]
    except urllib.error.HTTPError as e:
        out["err"] = f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}"
    except Exception as e:  # noqa: BLE001
        out["err"] = f"{type(e).__name__}: {e}"[:300]
    out["elapsed_s"] = round(time.time() - t0, 1)
    return out


def main() -> int:
    if not KEY:
        print("FATAL: GLM_API_KEY / ZHIPU_API_KEY 未配置")
        return 1
    print(f"model={MODEL} key={KEY[:4]}****")
    r1 = _anthropic_probe(force=False)
    print(f"[1] anthropic auto   : tool_use={r1['saw_tool_use']} "
          f"name={r1['tool_name']} stop={r1['stop_reason']} "
          f"text={r1['text_len']}字 {r1['elapsed_s']}s err={r1['err']}")
    r1f = None
    if not r1["saw_tool_use"] and not r1["err"]:
        r1f = _anthropic_probe(force=True)
        print(f"[1f] anthropic forced: tool_use={r1f['saw_tool_use']} "
              f"name={r1f['tool_name']} stop={r1f['stop_reason']} "
              f"err={r1f['err']}")
    r2 = _openai_probe()
    print(f"[2] openai paas/v4   : tool_calls={r2['saw_tool_calls']} "
          f"finish={r2['finish_reason']} {r2['elapsed_s']}s err={r2['err']}")
    ok_a = r1["saw_tool_use"] or (r1f or {}).get("saw_tool_use", False)
    print(f"VERDICT: anthropic端点tools={'PASS' if ok_a else 'FAIL'}; "
          f"openai端点tools={'PASS' if r2['saw_tool_calls'] else 'FAIL/空池'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
