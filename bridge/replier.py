# -*- coding: utf-8 -*-
"""
bridge/replier.py
=================
把 claude 的回复文本发回钉钉：清洗 + 分段 + 逐条 reply（@回复原消息）。

Why 清洗：钉钉群机器人 Markdown 仅支持 #/##/###、**粗**、*斜*、>引用、-列表、
[链接](url)、![图](url)；不支持 <font>、表格 |、---分隔线、复杂代码块。
claude 输出常含这些，直接发会被钉钉渲染成乱码或截断。

Why 分段：钉钉单条消息有长度限制（~20KB，但 Markdown 渲染建议远小于此），
按 1800 字分段 + 段落边界切，避免硬切单词/代码块。
"""
from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# 单条默认上限（字符）；留余量给 Markdown 语法开销
_DEFAULT_LIMIT = 1800

# 钉钉不支持的 HTML 标签（剥离标签保留内文）
_FONT_TAG = re.compile(r"<font[^>]*>(.*?)</font>", re.IGNORECASE | re.DOTALL)
# 通用 HTML 标签清理（<br> 转换行，其余剥离）
_OTHER_TAGS = re.compile(r"</?(?!b>|strong>|i>|em>|code>)[a-zA-Z][^>]*>")
# Markdown 表格分隔行（纯 | - : 组成）
_TABLE_SEPARATOR = re.compile(r"^\s*\|?[\s:|-]+\|?\s*$", re.MULTILINE)


def clean_markdown_for_dingtalk(text: str) -> str:
    """剥离钉钉不支持的 Markdown / HTML，保留可渲染部分。"""
    # 1) <font>...</font> → 内文
    text = _FONT_TAG.sub(r"\1", text)
    # 2) <br> → 换行；其它陌生 HTML 标签剥离
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = _OTHER_TAGS.sub("", text)
    # 3) 表格分隔行整行删除（数据行的 | 保留为竖线视觉分隔，钉钉能显示纯文本）
    text = _TABLE_SEPARATOR.sub("", text)
    return text.strip()


def split_long_text(text: str, limit: int = _DEFAULT_LIMIT) -> list[str]:
    """按段落/行边界切，每段 ≤ limit。尽量不硬切单词/代码行。"""
    if len(text) <= limit:
        return [text] if text else []

    chunks: list[str] = []
    # 先按段落（双换行）拆，再按单行拆，累计到 limit 就切
    buf: list[str] = []
    buf_len = 0
    for para in text.split("\n"):
        # 单行本身就超限：硬切兜底（极少见，如超长 base64）
        if len(para) > limit:
            if buf:
                chunks.append("\n".join(buf))
                buf, buf_len = [], 0
            for i in range(0, len(para), limit):
                chunks.append(para[i:i + limit])
            continue
        if buf_len + len(para) + 1 > limit:
            chunks.append("\n".join(buf))
            buf, buf_len = [], 0
        buf.append(para)
        buf_len += len(para) + 1
    if buf:
        chunks.append("\n".join(buf))
    return chunks


async def reply(
    handler: Any,
    incoming_msg: Any,
    text: str,
    limit: int = _DEFAULT_LIMIT,
) -> None:
    """清洗 → 分段 → 逐条 reply（@回复原消息）。投递失败重试 2 次。"""
    cleaned = clean_markdown_for_dingtalk(text)
    chunks = split_long_text(cleaned, limit=limit) or ["(空回复)"]
    for i, chunk in enumerate(chunks):
        # 多段加序号前缀，便于用户看出回答未完
        prefix = f"[{i + 1}/{len(chunks)}] " if len(chunks) > 1 else ""
        payload = prefix + chunk
        for attempt in range(3):
            try:
                # reply_text：dingtalk-stream ChatbotHandler 自带的 @回复方法
                await handler.reply_text(incoming_msg, payload)
                break
            except Exception:  # noqa: BLE001
                if attempt == 2:
                    logger.exception("reply 投递失败（已重试 3 次）：%s", payload[:80])
