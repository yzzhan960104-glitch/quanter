# -*- coding: utf-8 -*-
"""
bridge/claude_events.py
=======================
claude CLI stream-json 帧的纯函数解析器。

帧结构（2026-07-12 实测抓帧，非记忆；若 claude 升级改字段，仅改本文件）：
  输入(stdin): {"type":"user","message":{"role":"user",
               "content":[{"type":"text","text":"<消息>"}]}}
  assistant:   {"type":"assistant","message":{"content":[{"type":"text","text":...}]},
                "session_id":"<sid>"}
  result:      {"type":"result","subtype":"success","is_error":false,
                "result":"<最终文本>","session_id":"<sid>","permission_denials":[]}
  system:      init / thinking_tokens(大量噪音,忽略) / hook_* 等

设计：纯函数 + 无状态，最好测；claude_pool 只调本模块，不自己 json.loads。
"""
from __future__ import annotations

import json
from typing import Optional


def make_user_frame(text: str) -> str:
    """构造写入 claude stdin 的单行 user 帧（不含尾部换行，调用方加 \\n）。

    Why 不在帧内换行：stream-json 协议一行一帧，文本内的换行经 JSON 转义为 \\n，
    不会破坏帧边界。调用方负责在帧尾加 \\n 作为帧分隔。
    """
    return json.dumps(
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": text}],
            },
        },
        ensure_ascii=False,
    )


def parse_event_line(line: str) -> Optional[dict]:
    """解析一行 stdout。空行/非 JSON 返回 None（claude 偶发非 JSON 输出不炸）。"""
    line = line.strip()
    if not line:
        return None
    try:
        ev = json.loads(line)
        return ev if isinstance(ev, dict) else None
    except json.JSONDecodeError:
        return None


def is_result(event: dict) -> bool:
    """一轮终止判据：读到 result 帧。"""
    return event.get("type") == "result"


def extract_result_text(event: dict) -> str:
    """result 帧的 result 字段 = claude 给用户的最终文本（权威输出）。

    Why isinstance 防御：is_error turn 的 result 字段存在但为 None，
    dict.get("result","") 取到 None（不触发默认值 ""），str(None) 会回字面量 "None"
    给钉钉用户。错误 turn 恰是我们要优雅处理的场景，必须返空串。
    """
    result = event.get("result")
    return result if isinstance(result, str) else ""


def extract_session_id(event: dict) -> Optional[str]:
    """从任意含 session_id 的帧取（assistant/result/init 顶层都有）。"""
    sid = event.get("session_id")
    return str(sid) if sid else None


def extract_assistant_text(event: dict) -> str:
    """拼 assistant 帧 message.content 里所有 type==text 的项。

    Why 过滤 type==text：content 数组可能混入 tool_use（工具调用）项，
    其 JSON 不应当文本回钉钉。只取真正的文本块。
    """
    content = event.get("message", {}).get("content", [])
    if not isinstance(content, list):
        return ""
    return "".join(
        str(block.get("text", ""))
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    )
