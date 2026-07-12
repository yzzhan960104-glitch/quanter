# -*- coding: utf-8 -*-
"""replier 测试：分段边界 + Markdown 清洗 + reply 分多条。"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from bridge.replier import clean_markdown_for_dingtalk, reply, split_long_text


def test_short_text_single_chunk():
    assert split_long_text("hello") == ["hello"]


def test_long_text_split_by_paragraph():
    """超长文本按段落边界切，每段 ≤ limit。"""
    para = "a" * 500
    text = "\n\n".join([para] * 6)   # 3000+ 字符，6 段
    chunks = split_long_text(text, limit=1800)
    assert len(chunks) >= 2
    assert all(len(c) <= 1800 for c in chunks)
    # 内容不丢
    joined = "".join(c for c in chunks)
    for p in [para] * 6:
        assert p in joined


def test_clean_strips_font_tags():
    """<font> 钉钉不支持，剥离但保留内部文本。"""
    out = clean_markdown_for_dingtalk("看 <font color='red'>这个</font>")
    assert "<font" not in out
    assert "这个" in out


def test_clean_strips_table_pipes():
    """表格分隔行删除；数据行内容保留且 | 作竖线视觉分隔保留（controller 裁决）。"""
    out = clean_markdown_for_dingtalk("| a | b |\n|---|---|\n| 1 | 2 |")
    # 表格分隔行（|---|---|）整行删除
    assert "---" not in out
    # 数据行内容不丢
    assert "a" in out and "b" in out and "1" in out and "2" in out
    # 数据行 | 保留（controller 裁决：竖线分隔比换空格更可读）
    assert "|" in out


def test_clean_keeps_non_table_separator_lines():
    """纯连字符/冒号行（无 |）不是表格分隔行，不应被误删。"""
    text = "标题\n---\n正文"
    out = clean_markdown_for_dingtalk(text)
    assert "---" in out  # 保留（无 |，非表格）
    assert "标题" in out and "正文" in out


@pytest.mark.asyncio
async def test_reply_splits_into_multiple_sends():
    """超长回复分多条 reply（防钉钉单条 ~20KB 限 + Markdown 渲染截断）。"""
    handler = MagicMock()
    # reply_text 是 dingtalk-stream 同步方法，replier 用 asyncio.to_thread 包它，
    # 故这里用同步 MagicMock（而非 AsyncMock）才能真实复现「同步方法」语义。
    handler.reply_text = MagicMock()
    incoming = MagicMock()
    text = "x" * 4000
    await reply(handler, incoming, text, limit=1800)
    assert handler.reply_text.call_count >= 3
    # 验证参数顺序：(text, incoming_message) —— text 在前、incoming 在后
    first_call = handler.reply_text.call_args_list[0]
    assert first_call.args[0].startswith("[1/")  # text 在前（含 [1/N] 序号前缀）
    assert first_call.args[1] is incoming  # incoming_message 在后
