# -*- coding: utf-8 -*-
"""
bridge/claude_pool.py
=====================
常驻 claude 子进程的状态机封装（ClaudeProcess）+ 进程池（ClaudePool，Task 5 补）。

核心思想：每个钉钉会话(conversationId)对应一个常驻 claude 进程，stream-json 双流：
  - stdin  持续写 user 帧（进程不 EOF 即常驻多轮）
  - stdout 逐行读事件，聚合 assistant 增量，读到 result 帧 = 一轮结束

崩溃恢复：进程会死，但 claude 把会话历史存本地 ~/.claude/。拿着 session_id
即可 --resume <sid> 续上下文。故 ask 超时/崩溃时 kill 后用 session_id 重建，
重试 1 次，仍失败抛 RuntimeError（不无限重试，防死循环刷钉钉）。

Windows asyncio：create_subprocess_exec 需 ProactorEventLoop（3.8+ Windows 默认）。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable, Optional

from bridge import claude_events as ce
from bridge.config import BridgeConfig

logger = logging.getLogger(__name__)

# 单轮失败后最多重试次数（含首次 = 总尝试 2 次）
_MAX_ATTEMPTS = 2


class ClaudeProcess:
    """单个钉钉会话对应的常驻 claude 子进程。"""

    def __init__(self, cfg: BridgeConfig, session_id: Optional[str] = None) -> None:
        self._cfg = cfg
        self._session_id: Optional[str] = session_id  # 已知则 --resume 续上下文
        self._proc: Optional[asyncio.subprocess.Process] = None
        # 同会话串行锁：claude 进程一次只能处理一轮，第二条 ask 必须等第一条 result
        self._lock = asyncio.Lock()

    # ---- 对外属性 ----
    @property
    def session_id(self) -> Optional[str]:
        return self._session_id

    @property
    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    # ---- 进程生命周期 ----
    async def _spawn(self) -> None:
        """拉起 claude（stream-json 双流 + 全放行）。已知 session_id 则 --resume。"""
        cmd = [
            self._cfg.claude_bin,
            "--print",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--verbose",
            # 全放行：等同终端里每个确认都按 y（见 spec §7/§8 安全契约）
            "--permission-mode", "bypassPermissions",
        ]
        if self._session_id:
            # 续上下文：进程死后用 session_id 重建，历史不丢
            cmd += ["--resume", self._session_id]

        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._cfg.workdir,
        )
        logger.info("claude 子进程已启动 (pid=%s, resume=%s)",
                    self._proc.pid, self._session_id or "(new)")

    async def _kill(self) -> None:
        """强制结束当前进程（超时/崩溃重建用）。"""
        if self._proc is None or self._proc.returncode is not None:
            self._proc = None
            return
        try:
            self._proc.kill()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            pass
        self._proc = None

    async def aclose(self) -> None:
        """优雅关闭（空闲回收 / 桥退出用）。"""
        await self._kill()

    # ---- 核心：一轮问答 ----
    async def _read_until_result(
        self,
        on_event: Optional[Callable[[dict], None]],
    ) -> str:
        """从 stdout 逐行读，聚合 assistant 文本，直到 result 帧。

        返回 result.result（权威最终文本）。同时把每个解析出的事件交给 on_event。
        """
        accumulated: list[str] = []
        assert self._proc is not None and self._proc.stdout is not None
        while True:
            line_bytes = await self._proc.stdout.readline()
            if not line_bytes:
                # stdout EOF = 进程已退出（崩溃/被 kill）。抛出让上层走重建。
                raise RuntimeError("claude stdout EOF（进程意外退出）")
            ev = ce.parse_event_line(line_bytes.decode("utf-8", errors="replace"))
            if ev is None:
                continue  # 非 JSON / 空行 / 噪音
            if on_event is not None:
                on_event(ev)
            # 捕获 session_id（init/assistant/result 帧都有）
            sid = ce.extract_session_id(ev)
            if sid:
                self._session_id = sid
            # 累加 assistant 增量文本
            if ev.get("type") == "assistant":
                accumulated.append(ce.extract_assistant_text(ev))
            elif ce.is_result(ev):
                # 一轮终止：以 result.result 为权威（优先于累加，防增量遗漏/重复）
                return ce.extract_result_text(ev) or "".join(accumulated)

    async def ask(
        self,
        text: str,
        on_event: Optional[Callable[[dict], None]] = None,
    ) -> str:
        """发一轮问答。超时/崩溃 → kill → --resume 重建重试 1 次。"""
        async with self._lock:  # 同会话串行
            last_err: Optional[Exception] = None
            for attempt in range(1, _MAX_ATTEMPTS + 1):
                # 懒启动 or 进程已死则（重新）拉起
                if not self.is_alive:
                    await self._spawn()
                try:
                    # 写 user 帧（stream-json 一行一帧，尾加 \n）
                    frame = ce.make_user_frame(text) + "\n"
                    assert self._proc is not None and self._proc.stdin is not None
                    self._proc.stdin.write(frame.encode("utf-8"))
                    await self._proc.stdin.drain()
                    # 读到 result 为止，带单轮超时
                    return await asyncio.wait_for(
                        self._read_until_result(on_event),
                        timeout=self._cfg.ask_timeout,
                    )
                except (asyncio.TimeoutError, RuntimeError) as e:
                    last_err = e
                    logger.warning("claude 第 %d 轮失败 (%s)，kill 后重建重试",
                                   attempt, type(e).__name__)
                    await self._kill()
                    # 循环回到 _spawn：已知 session_id 会自动 --resume 续上下文
            # 重试用尽：抛出，让上层回错误文本给钉钉（不无限重试）
            raise RuntimeError(f"claude 连续 {_MAX_ATTEMPTS} 轮失败：{last_err}")


# ClaudePool 在 Task 5 追加到本文件
