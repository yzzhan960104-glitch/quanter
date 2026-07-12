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
    """表格 | 钉钉不渲染，把整行表格转成普通文本（去 | 留空格）。"""
    out = clean_markdown_for_dingtalk("| a | b |\n|---|---|\n| 1 | 2 |")
    # 表格分隔行（纯 | 和 -）整行删除；数据行 | 替换为空格
    assert "---" not in out


@pytest.mark.asyncio
async def test_reply_splits_into_multiple_sends():
    """超长回复分多条 reply（防钉钉单条 ~20KB 限 + Markdown 渲染截断）。"""
    handler = MagicMock()
    handler.reply_text = AsyncMock()
    incoming = MagicMock()
    text = "x" * 4000
    await reply(handler, incoming, text, limit=1800)
    assert handler.reply_text.call_count >= 3
