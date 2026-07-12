# -*- coding: utf-8 -*-
"""ClaudePool 测试：mock ClaudeProcess，验证会话隔离/串行/回收/reset。"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from bridge.claude_pool import ClaudePool
from bridge.session_store import SessionStore


class FakeProc:
    """假的 ClaudeProcess：记录 ask 调用，可控制返回值与串行。"""
    def __init__(self, answer="ok", session_id="sid-x"):
        self._answer = answer
        self.session_id = session_id
        self.is_alive = True
        self.last_active = 0.0
        self.ask = AsyncMock(return_value=answer)
        self.aclose = AsyncMock()


@pytest.mark.asyncio
async def test_same_conversation_reuses_process(tmp_path):
    """同会话两次 ask 复用同一进程（不重复 spawn）。"""
    store = SessionStore(str(tmp_path / "s.json"))
    pool = ClaudePool(cfg=MagicMock(idle_ttl=900, ask_timeout=10), store=store,
                      proc_factory=lambda cfg, sid: FakeProc())
    a1 = await pool.ask("convA", "q1", "staff")
    a2 = await pool.ask("convA", "q2", "staff")
    assert a1 == a2 == "ok"
    proc = pool._procs["convA"]  # 同一对象
    assert proc.ask.call_count == 2
    await pool.aclose_all()


@pytest.mark.asyncio
async def test_different_conversations_isolated(tmp_path):
    """不同会话用不同进程（跨会话隔离，避免上下文串味）。"""
    store = SessionStore(str(tmp_path / "s.json"))
    pool = ClaudePool(cfg=MagicMock(idle_ttl=900, ask_timeout=10), store=store,
                      proc_factory=lambda cfg, sid: FakeProc())
    await pool.ask("convA", "q", "staff")
    await pool.ask("convB", "q", "staff")
    assert len(pool._procs) == 2
    assert pool._procs["convA"] is not pool._procs["convB"]
    await pool.aclose_all()


@pytest.mark.asyncio
async def test_reset_kills_process_and_clears_mapping(tmp_path):
    """/new (reset) 杀进程 + 清 session_store 映射。"""
    path = tmp_path / "s.json"
    store = SessionStore(str(path))
    store.set("convA", "sid-old")
    pool = ClaudePool(cfg=MagicMock(idle_ttl=900, ask_timeout=10), store=store,
                      proc_factory=lambda cfg, sid: FakeProc())
    await pool.ask("convA", "q", "staff")
    await pool.reset("convA")
    assert "convA" not in pool._procs          # 进程已杀
    assert store.get("convA") is None          # 映射已清
    await pool.aclose_all()


@pytest.mark.asyncio
async def test_session_id_persisted_after_ask(tmp_path):
    """ask 后把捕获的 session_id 落 store（进程死后 --resume 可续）。"""
    path = tmp_path / "s.json"
    store = SessionStore(str(path))
    pool = ClaudePool(cfg=MagicMock(idle_ttl=900, ask_timeout=10), store=store,
                      proc_factory=lambda cfg, sid: FakeProc(session_id="sid-caught"))
    await pool.ask("convA", "q", "staff")
    assert store.get("convA") == "sid-caught"
    await pool.aclose_all()


@pytest.mark.asyncio
async def test_idle_sweeper_reclaims_idle_process(tmp_path):
    """空闲超 idle_ttl 的进程被回收（防进程数随历史会话无限增长）。"""
    store = SessionStore(str(tmp_path / "s.json"))
    # idle_ttl=0 → 立即视为空闲可回收
    pool = ClaudePool(cfg=MagicMock(idle_ttl=0, ask_timeout=10), store=store,
                      proc_factory=lambda cfg, sid: FakeProc())
    await pool.ask("convA", "q", "staff")
    assert "convA" in pool._procs
    await pool._sweep_once()   # 手动触发一次扫描
    assert "convA" not in pool._procs
    await pool.aclose_all()


@pytest.mark.asyncio
async def test_sweep_preserves_session_id(tmp_path):
    """空闲回收只杀进程，不清 store——session_id 保留供下次 --resume 续上下文。

    回归保护：之前 _sweep_once 误用 reset（清 store），导致空闲 15 分钟后丢上下文。
    """
    store = SessionStore(str(tmp_path / "s.json"))
    pool = ClaudePool(cfg=MagicMock(idle_ttl=0, ask_timeout=10), store=store,
                      proc_factory=lambda cfg, sid: FakeProc())  # 默认 session_id="sid-x"
    await pool.ask("convA", "q", "staff")
    assert store.get("convA") == "sid-x"
    await pool._sweep_once()
    assert "convA" not in pool._procs            # 进程已回收
    assert store.get("convA") == "sid-x"         # session_id 保留（修复点）
    await pool.aclose_all()
