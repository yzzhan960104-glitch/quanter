# -*- coding: utf-8 -*-
"""ClaudeProcess 状态机测试：mock asyncio.subprocess，不真跑 claude。

验证：懒启动、stdin 写 user 帧、stdout 聚合到 result、超时 kill+resume 重试、
session_id 捕获、on_event 回调。
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from bridge.claude_pool import ClaudeProcess

# 复用真实帧结构（合法 JSON；brief 原版 ASSISTANT_REAL 顶层多了一个 }，已修正）
INIT_LINE = '{"type":"system","subtype":"init","session_id":"sid-init"}'
ASSISTANT_REAL = (
    '{"type":"assistant","message":{"content":'
    '[{"type":"text","text":"hello "}]},'
    '"session_id":"sid-real"}'
)
ASSISTANT_REAL_2 = (
    '{"type":"assistant","message":{"content":'
    '[{"type":"text","text":"world"}]}}'
)
RESULT_LINE = (
    '{"type":"result","subtype":"success","is_error":false,'
    '"result":"hello world","session_id":"sid-real","permission_denials":[]}'
)


def _make_proc_mock(lines_for_first_read, lines_for_retry=None):
    """造一个假的 asyncio.subprocess.Process：stdout 按行吐给定 JSON。

    第一次 ask 读 lines_for_first_read；若被 kill 重建，第二次读 lines_for_retry。

    Why 索引 = state["call"]-1：ClaudeProcess.ask 的真实调用顺序是
    `_spawn()`(→fake_create，state["call"] 先 +1) 再 `_read_until_result`(→readline)。
    故首次 spawn 后 state["call"]==1，readline 应读 seq[0]；二次 spawn 后
    state["call"]==2，readline 读 seq[1]。索引 = call-1 对齐「第 n 次 spawn 读第 n 组」。
    """
    state = {"call": 0}

    async def readline():
        seqs = [lines_for_first_read, lines_for_retry or []]
        # 第 state["call"] 次 spawn 对应的行组（call 从 1 起）
        idx_seq = state["call"] - 1
        if idx_seq < 0 or idx_seq >= len(seqs):
            return b""  # 没有对应组 → EOF
        seq = seqs[idx_seq]
        idx = state.get("idx", 0)
        state["idx"] = idx + 1
        if idx < len(seq):
            return (seq[idx] + "\n").encode("utf-8")
        return b""  # EOF

    proc = MagicMock()
    proc.stdout = MagicMock()
    proc.stdout.readline = readline
    proc.stdin = MagicMock()
    proc.stdin.write = MagicMock()
    proc.stdin.drain = AsyncMock()
    proc.returncode = None
    proc.wait = AsyncMock(return_value=0)
    proc.terminate = MagicMock()
    proc.kill = MagicMock()

    async def fake_create(*a, **kw):
        # 每次 spawn（首次 + resume 重试）推进序号 + 重置行内游标
        state["call"] += 1
        state["idx"] = 0
        proc.returncode = None
        return proc

    return proc, fake_create


@pytest.mark.asyncio
async def test_ask_lazy_starts_and_aggregates_result(monkeypatch, tmp_path):
    """首次 ask 触发 spawn；聚合 assistant 增量 + 以 result.result 为权威输出。"""
    from bridge import claude_pool as cp
    from bridge.config import BridgeConfig

    cfg = BridgeConfig(
        app_key="k", app_secret="s", allowed_staff_ids=frozenset({"x"}),
        claude_bin="claude", workdir=str(tmp_path), ask_timeout=10,
        idle_ttl=900, rate_limit_per_min=10,
        session_store_path=str(tmp_path / "s.json"),
        audit_log_path=str(tmp_path / "a.jsonl"), log_path=str(tmp_path / "l.log"),
    )
    proc, fake_create = _make_proc_mock(
        [INIT_LINE, ASSISTANT_REAL, ASSISTANT_REAL_2, RESULT_LINE]
    )
    monkeypatch.setattr(cp.asyncio, "create_subprocess_exec", fake_create)

    cp_obj = ClaudeProcess(cfg, session_id=None)
    answer = await cp_obj.ask("hi")

    assert answer == "hello world"           # result.result 权威
    assert cp_obj.session_id == "sid-real"   # 从帧捕获
    # 确认 stdin 写了 user 帧（make_user_frame 用 json.dumps 默认带空格：'"type": "user"'）
    written = b"".join(c.args[0] for c in proc.stdin.write.call_args_list)
    assert b'"type": "user"' in written
    await cp_obj.aclose()


@pytest.mark.asyncio
async def test_ask_recovers_from_crash_then_resume_retry(monkeypatch, tmp_path):
    """进程中途崩溃(stdout EOF)→ kill + --resume 重建重试一次成功。

    模拟首次 spawn 只吐 assistant 帧就 EOF(进程死);重试 spawn 直接吐 result。
    覆盖 _read_until_result 抛 RuntimeError→重建路径(超时 TimeoutError 走同一分支)。
    """
    from bridge import claude_pool as cp
    from bridge.config import BridgeConfig

    cfg = BridgeConfig(
        app_key="k", app_secret="s", allowed_staff_ids=frozenset({"x"}),
        claude_bin="claude", workdir=str(tmp_path), ask_timeout=1,
        idle_ttl=900, rate_limit_per_min=10,
        session_store_path=str(tmp_path / "s.json"),
        audit_log_path=str(tmp_path / "a.jsonl"), log_path=str(tmp_path / "l.log"),
    )
    # 第一次只吐 assistant 不吐 result（永远等不到 → 超时）；重试时吐完整 result
    proc, fake_create = _make_proc_mock(
        [ASSISTANT_REAL],                       # 第一次：卡住不结束
        [RESULT_LINE],                          # 重试：直接 result
    )
    monkeypatch.setattr(cp.asyncio, "create_subprocess_exec", fake_create)

    cp_obj = ClaudeProcess(cfg, session_id="sid-real")
    answer = await cp_obj.ask("hi")
    assert answer == "hello world"
    # 确认被 kill 过
    assert proc.kill.called or proc.terminate.called
    await cp_obj.aclose()


@pytest.mark.asyncio
async def test_on_event_callback_invoked(monkeypatch, tmp_path):
    """on_event 回调把每个解析出的事件交给调用方（alarmer 监听工具调用用）。"""
    from bridge import claude_pool as cp
    from bridge.config import BridgeConfig

    cfg = BridgeConfig(
        app_key="k", app_secret="s", allowed_staff_ids=frozenset({"x"}),
        claude_bin="claude", workdir=str(tmp_path), ask_timeout=10,
        idle_ttl=900, rate_limit_per_min=10,
        session_store_path=str(tmp_path / "s.json"),
        audit_log_path=str(tmp_path / "a.jsonl"), log_path=str(tmp_path / "l.log"),
    )
    proc, fake_create = _make_proc_mock([ASSISTANT_REAL, RESULT_LINE])
    monkeypatch.setattr(cp.asyncio, "create_subprocess_exec", fake_create)

    seen_types: list[str] = []
    cp_obj = ClaudeProcess(cfg)
    await cp_obj.ask("hi", on_event=lambda ev: seen_types.append(ev["type"]))

    assert "assistant" in seen_types
    assert "result" in seen_types
    await cp_obj.aclose()


@pytest.mark.asyncio
async def test_ask_marks_busy_during_read(monkeypatch, tmp_path):
    """ask 执行期间 is_busy=True、结束后 False（sweeper 据此跳过活进程，修 race）。

    on_event 在 _read_until_result 读循环内触发，此刻 is_busy 必为 True；
    ask 返回后归位 False，确保非跑动状态可被正常空闲回收。
    """
    from bridge import claude_pool as cp
    from bridge.config import BridgeConfig

    cfg = BridgeConfig(
        app_key="k", app_secret="s", allowed_staff_ids=frozenset({"x"}),
        claude_bin="claude", workdir=str(tmp_path), ask_timeout=10,
        idle_ttl=900, rate_limit_per_min=10,
        session_store_path=str(tmp_path / "s.json"),
        audit_log_path=str(tmp_path / "a.jsonl"), log_path=str(tmp_path / "l.log"),
    )
    proc, fake_create = _make_proc_mock([INIT_LINE, ASSISTANT_REAL, RESULT_LINE])
    monkeypatch.setattr(cp.asyncio, "create_subprocess_exec", fake_create)

    busy_seen: list[bool] = []
    cp_obj = ClaudeProcess(cfg)
    await cp_obj.ask("hi", on_event=lambda ev: busy_seen.append(cp_obj.is_busy))

    assert any(busy_seen), f"读期间应 is_busy=True，实测 {busy_seen}"
    assert cp_obj.is_busy is False      # 结束后归位
    await cp_obj.aclose()


@pytest.mark.asyncio
async def test_read_until_result_guard_when_proc_nulled(monkeypatch, tmp_path):
    """读循环中 _proc 被外部置 None（/new reset / 异常回收）→ 抛清晰 RuntimeError 走重建，
    而非裸 AttributeError 崩溃。

    回归保护 2026-07-13 22:23 那条 `AttributeError: 'NoneType' has no attribute 'stdout'`：
    即便 sweeper 不再杀活进程，任何把 _proc 置空的路径都不该以 AttributeError 上抛——
    RuntimeError 会被 ask 的重建重试链兜住，对用户只是"慢一点重试成功"而非"崩溃失败"。
    """
    from bridge import claude_pool as cp
    from bridge.config import BridgeConfig

    cfg = BridgeConfig(
        app_key="k", app_secret="s", allowed_staff_ids=frozenset({"x"}),
        claude_bin="claude", workdir=str(tmp_path), ask_timeout=10,
        idle_ttl=900, rate_limit_per_min=10,
        session_store_path=str(tmp_path / "s.json"),
        audit_log_path=str(tmp_path / "a.jsonl"), log_path=str(tmp_path / "l.log"),
    )
    # 第 1 组：吐 1 行 assistant 后让 readline 把 _proc 置 None（模拟外部杀进程）；
    # 第 2 组（重建后）：直接吐 result → 重试成功。
    seqs = [[ASSISTANT_REAL], [RESULT_LINE]]
    state = {"call": 0, "idx": 0}
    cp_ref: dict = {}

    async def readline():
        idx_seq = max(0, state["call"] - 1)
        seq = seqs[idx_seq] if idx_seq < len(seqs) else []
        idx = state["idx"]
        state["idx"] = idx + 1
        if idx < len(seq):
            # 第 1 组首行读出前，模拟外部把 _proc 置空
            if idx_seq == 0 and idx == 0 and "o" in cp_ref:
                cp_ref["o"]._proc = None
            return (seq[idx] + "\n").encode("utf-8")
        return b""  # EOF

    proc = MagicMock()
    proc.stdout = MagicMock(); proc.stdout.readline = readline
    proc.stdin = MagicMock(); proc.stdin.write = MagicMock(); proc.stdin.drain = AsyncMock()
    proc.returncode = None; proc.wait = AsyncMock(return_value=0)
    proc.kill = MagicMock(); proc.terminate = MagicMock()

    async def fake_create(*a, **kw):
        state["call"] += 1; state["idx"] = 0; proc.returncode = None
        return proc

    monkeypatch.setattr(cp.asyncio, "create_subprocess_exec", fake_create)
    cp_obj = ClaudeProcess(cfg)
    cp_ref["o"] = cp_obj
    answer = await cp_obj.ask("hi")
    assert answer == "hello world"   # 经 guard→RuntimeError→重建重试→result 恢复
    await cp_obj.aclose()
