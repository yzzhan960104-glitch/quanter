# -*- coding: utf-8 -*-
"""stream_client 派发测试：mock dingtalk-stream 的 ChatbotMessage，不连真钉钉。

验证：白名单 allow → 异步派发 pool + reply；reject → 静默（不 reply）；
command → 执行指令回复；审计 jsonl 落盘。

字段以 Task 8 Step 0 实测 dingtalk-stream 0.24.3 为准：
ChatbotMessage 有 text.content / sender_staff_id / conversation_id / message_id。
"""
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from bridge.config import BridgeConfig
from bridge.stream_client import BridgeHandler


def _cfg(tmp_path):
    """造一份最小可用配置（白名单含 staffOK，审计/会话路径落 tmp）。"""
    return BridgeConfig(
        app_key="k", app_secret="s", allowed_staff_ids=frozenset({"staffOK"}),
        claude_bin="claude", workdir=str(tmp_path), ask_timeout=10,
        idle_ttl=900, rate_limit_per_min=10,
        session_store_path=str(tmp_path / "s.json"),
        audit_log_path=str(tmp_path / "a.jsonl"), log_path=str(tmp_path / "l.log"),
    )


def _make_msg(text: str, staff_id: str = "staffOK", conv_id: str = "convA"):
    """造一个假的 ChatbotMessage（只用到用到的字段）。

    字段名对齐 SDK 0.24.3 ChatbotMessage 实测：
      text.content / sender_staff_id / conversation_id / message_id
    """
    m = MagicMock()
    m.text = MagicMock(content=text)          # 钉钉 SDK: msg.text.content
    m.sender_staff_id = staff_id
    m.conversation_id = conv_id
    m.message_id = "mid-1"                    # SDK 实际字段名（非 msg_id）
    return m


@pytest.mark.asyncio
async def test_allow_dispatches_to_pool_and_replies(tmp_path):
    """白名单 allow：pool.ask 结果经 reply 回钉钉。"""
    cfg = _cfg(tmp_path)
    pool = MagicMock()
    pool.ask = AsyncMock(return_value="claude 的回答")
    reply_fn = AsyncMock()              # 注入 mock reply，断言它被调用且带回答
    handler = BridgeHandler(cfg=cfg, pool=pool, alarmer=MagicMock(),
                            reply_fn=reply_fn)

    msg = _make_msg("解释颈线")
    await handler._dispatch(msg)   # 直接调内部派发（跳过 SDK ACK 细节）

    pool.ask.assert_awaited_once()
    # reply_fn 被调用，第 3 个位置参数(answer)含 claude 回答
    reply_fn.assert_awaited()
    assert "claude 的回答" in reply_fn.call_args.args[2]


@pytest.mark.asyncio
async def test_reject_silent_no_reply_no_pool(tmp_path):
    """非白名单 reject：不调 pool、不 reply（静默丢弃 + 审计）。"""
    cfg = _cfg(tmp_path)
    pool = MagicMock(); pool.ask = AsyncMock()
    reply_fn = AsyncMock()
    handler = BridgeHandler(cfg=cfg, pool=pool, alarmer=MagicMock(),
                            reply_fn=reply_fn)

    msg = _make_msg("hi", staff_id="intruder")
    await handler._dispatch(msg)

    pool.ask.assert_not_awaited()
    reply_fn.assert_not_awaited()        # 静默：不回执


@pytest.mark.asyncio
async def test_command_new_resets_session(tmp_path):
    """/new 指令：调 pool.reset + 回执，不喂 claude。"""
    cfg = _cfg(tmp_path)
    pool = MagicMock(); pool.reset = AsyncMock(); pool.ask = AsyncMock()
    reply_fn = AsyncMock()
    handler = BridgeHandler(cfg=cfg, pool=pool, alarmer=MagicMock(),
                            reply_fn=reply_fn)

    await handler._dispatch(_make_msg("/new"))
    pool.reset.assert_awaited_once_with("convA")
    pool.ask.assert_not_awaited()
    reply_fn.assert_awaited()            # 回执"会话已重置"


@pytest.mark.asyncio
async def test_audit_log_written(tmp_path):
    """每条消息落审计 jsonl（全放行模式事后追溯底线）。"""
    cfg = _cfg(tmp_path)
    pool = MagicMock(); pool.ask = AsyncMock(return_value="ans")
    handler = BridgeHandler(cfg=cfg, pool=pool, alarmer=MagicMock(),
                            reply_fn=AsyncMock())
    await handler._dispatch(_make_msg("hi"))

    lines = Path(cfg.audit_log_path).read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["sender_staff_id"] == "staffOK"
    assert rec["conversation_id"] == "convA"
    assert rec["text"] == "hi"
    assert rec["action"] == "allow"


@pytest.mark.asyncio
async def test_completion_log_includes_answer_content(tmp_path, caplog):
    """完成日志带上 answer 实际内容（截断），不只打字数。

    回归 2026-07-13 10:15：日志只有"输出 250 字"，无法判断那 250 字是错误文本还是
    真实结论，得反复对照才知是 529。补内容后一眼可辨。
    """
    import logging as _stdlib_logging
    from bridge.stream_client import _COMPLETION_SENTINEL  # "claude 完成"

    cfg = _cfg(tmp_path)
    pool = MagicMock(); pool.ask = AsyncMock(return_value="真实结论：颈线突破确认")
    handler = BridgeHandler(cfg=cfg, pool=pool, alarmer=MagicMock(), reply_fn=AsyncMock())

    with caplog.at_level(_stdlib_logging.INFO, logger="bridge.stream_client"):
        await handler._dispatch(_make_msg("问"))

    done = [r for r in caplog.records if _COMPLETION_SENTINEL in r.getMessage()]
    assert done, "应有一条 claude 完成日志"
    assert "真实结论" in done[0].getMessage()   # answer 内容进了日志


@pytest.mark.asyncio
async def test_upstream_error_replaced_with_honest_failure_status(tmp_path):
    """pool 返回 529 上游错误文本时，不把原文当答案 @ 回钉钉，改发诚实失败状态。

    回归 2026-07-13 10:15：529 经 assistant→result 正常返回，桥把错误原文当 claude
    的回答 @ 给用户（@131****2380 API Error: 529...），用户误以为这就是结论。
    修法：检测 answer 中的"API Error: \\d{3}"，命中即替换为诚实失败提示（不重试）。
    """
    cfg = _cfg(tmp_path)
    raw_err = ("API Error: 529 [1305][The service may be temporarily overloaded, "
               "please try again later][x]. This is a server-side issue.")
    pool = MagicMock(); pool.ask = AsyncMock(return_value=raw_err)
    reply_fn = AsyncMock()
    handler = BridgeHandler(cfg=cfg, pool=pool, alarmer=MagicMock(), reply_fn=reply_fn)

    await handler._dispatch(_make_msg("问"))

    replied = reply_fn.call_args.args[2]
    assert "API Error" not in replied          # 错误原文不当答案发出
    assert "重发" in replied                    # 给出诚实可操作的状态


@pytest.mark.asyncio
async def test_normal_answer_not_mistaken_as_upstream_error(tmp_path):
    """正常回答（即便含 'error' 字样）不被误判为上游错误、原样发出。

    防御：上游错误检测正则须精确（'API Error: \\d{3}'），不能把日常含 error 的
    真实回答（如"该策略的 error rate 低"）误杀成失败状态。
    """
    cfg = _cfg(tmp_path)
    normal = "颈线突破策略的历史 error rate 很低，可放心实盘。"
    pool = MagicMock(); pool.ask = AsyncMock(return_value=normal)
    reply_fn = AsyncMock()
    handler = BridgeHandler(cfg=cfg, pool=pool, alarmer=MagicMock(), reply_fn=reply_fn)

    await handler._dispatch(_make_msg("问"))

    assert reply_fn.call_args.args[2] == normal   # 原样发出，未被替换
