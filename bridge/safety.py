# -*- coding: utf-8 -*-
"""
bridge/safety.py
================
纯逻辑安全闸：对每条入站消息做「身份 + 意图」裁决。

无 IO、无副作用——最好测的单元。stream_client 拿到裁决后决定：
  allow   → 喂给 claude 进程池
  reject  → 静默丢弃 + 审计（不回执，防探测者通过回执确认机器人存活）
  command → 直接执行指令（/new /status /help），不进 claude
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

from bridge.config import BridgeConfig

# 已知指令 → 标准化名（大小写/空白无关）
_COMMANDS: dict[str, str] = {
    "/new": "new",
    "/status": "status",
    "/help": "help",
}


@dataclass(frozen=True)
class Verdict:
    """单条消息的裁决结果。"""
    action: Literal["allow", "reject", "command"]
    reason: str
    # 仅 action=="command" 时非空：取值 "new"/"status"/"help"
    command: Optional[str] = None


def classify(sender_staff_id: str, text: str, cfg: BridgeConfig) -> Verdict:
    """身份闸（白名单）→ 意图闸（指令解析）。"""
    # 身份闸：全放行模式下唯一身份闸，非白名单一律 reject
    if sender_staff_id not in cfg.allowed_staff_ids:
        return Verdict(
            action="reject",
            reason=f"sender {sender_staff_id} 不在白名单",
        )

    # 意图闸：取首词（按空白拆），若为已知指令前缀则走 command 分支
    head = text.strip().split(maxsplit=1)[0].lower() if text.strip() else ""
    if head in _COMMANDS:
        return Verdict(action="command", reason="known command",
                       command=_COMMANDS[head])

    # 普通文本：allow 原样透传（stream_client 负责去掉可能的 @机器人 前缀）
    return Verdict(action="allow", reason="ok")
