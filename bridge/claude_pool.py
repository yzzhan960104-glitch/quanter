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
import time
from typing import Callable, Optional

from bridge import claude_events as ce
from bridge.config import BridgeConfig
from bridge.session_store import SessionStore

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


# ===================== ClaudePool（进程池） =====================
# 每个 conversationId 懒启动一个 ClaudeProcess，同会话串行（由 ClaudeProcess
# 内部锁保证）、跨会话并行（不同进程互不干扰）。空闲超 idle_ttl 的进程主动
# 回收，session_id 仍在 store 落盘，下次同会话再来走 --resume 续上下文。


class ClaudePool:
    """conversationId → ClaudeProcess 的进程池。

    Why 每会话一进程：claude CLI 是有状态的多轮对话（同一进程内 stdin 持续写
    user 帧即可推进上下文）。一个进程同一时刻只能处理一轮（内部锁），但不同
    会话彼此独立——故「每会话一进程」是同会话串行 + 跨会话并行的最小可行解。

    Why session_id 落盘：进程会死（崩溃/超时 kill/空闲回收），但 claude 把完整
    会话历史存本地 ~/.claude/。拿着 session_id 即可 --resume <sid> 续上下文，
    故 ask 后必须把 session_id 落 SessionStore（进程死、映射在，上下文不丢）。
    """

    # proc_factory 仅测试注入用（生产用默认 ClaudeProcess），便于 FakeProc 替身
    def __init__(
        self,
        cfg: BridgeConfig,
        store: SessionStore,
        proc_factory: Optional[Callable] = None,
    ) -> None:
        self._cfg = cfg
        self._store = store
        # 会话 → 常驻进程映射（懒启动：首次 ask 才创建）
        self._procs: dict[str, ClaudeProcess] = {}
        # 会话 → 最近活跃时间（monotonic），空闲回收判定依据
        self._last_active: dict[str, float] = {}
        # 生产默认造真实 ClaudeProcess；测试注入 FakeProc 避免真跑 claude
        self._proc_factory = proc_factory or (lambda c, sid: ClaudeProcess(c, sid))

    def _get_or_create(self, conv_id: str) -> ClaudeProcess:
        """懒启动：首次取用时创建，已知 session_id 则传入（--resume 续上下文）。

        Why 从 store 取 sid：进程可能被空闲回收掉了，但 session_id 在磁盘上。
        把 sid 传给新进程构造函数，ClaudeProcess._spawn 会拼 --resume <sid>，
        于是「进程死、上下文不丢」的崩溃恢复链成立。
        """
        if conv_id not in self._procs:
            sid = self._store.get(conv_id)
            self._procs[conv_id] = self._proc_factory(self._cfg, sid)
        return self._procs[conv_id]

    async def ask(
        self,
        conv_id: str,
        text: str,
        sender_staff_id: str,
        on_event: Optional[Callable[[dict], None]] = None,
    ) -> str:
        """派发一轮问答到对应会话进程；ask 后落盘 session_id。

        - 同会话串行：由 ClaudeProcess 内部 _lock 保证（一轮没出 result，下一轮 await）。
        - 跨会话并行：不同 conv_id 取到不同进程对象，互不阻塞。
        - sender_staff_id 目前透传留口（后续审计/权限分级用），本轮不强校验。
        """
        proc = self._get_or_create(conv_id)
        try:
            answer = await proc.ask(text, on_event=on_event)
        except Exception:
            # 进程挂了：从池中摘除，下次 ask 走重建（已知 sid → --resume 续上下文）
            self._procs.pop(conv_id, None)
            raise
        # 记活跃时间（monotonic 不受系统时钟跳变影响，回收判定更稳）
        self._last_active[conv_id] = time.monotonic()
        # 落盘 session_id：进程死后 --resume 可续（spec §4.1 三级链）
        if proc.session_id:
            self._store.set(conv_id, proc.session_id)
        return answer

    async def reset(self, conv_id: str) -> None:
        """/new：杀该会话进程 + 清映射 → 下次开全新会话。

        Why 三清：进程要杀（释放资源）、_procs 映射要清（下次懒启动造新的）、
        store 映射要清（不传 sid = 不 --resume = 全新会话，不接旧上下文）。
        """
        proc = self._procs.pop(conv_id, None)
        if proc is not None:
            await proc.aclose()
        self._store.clear(conv_id)
        self._last_active.pop(conv_id, None)

    def status(self) -> list[dict]:
        """/status：每会话的 alive / session_id / last_active 快照（运维观测用）。"""
        out = []
        for conv_id, proc in self._procs.items():
            out.append({
                "conversation_id": conv_id,
                "alive": proc.is_alive,
                "session_id": proc.session_id,
                "last_active": self._last_active.get(conv_id),
            })
        return out

    async def _sweep_once(self) -> None:
        """扫一次：空闲超 idle_ttl 的进程回收（start_idle_sweeper 周期调用）。

        Why 用 reset 而非裸 pop：reset 既杀进程又清 _last_active，且保留 store
        里的 session_id（下次同会话来仍可 --resume 续上下文，只是进程当前不在池中）。
        """
        now = time.monotonic()
        # 用 >= 而非 >：idle_ttl=0 表示「立即回收」（brief 测试明意）；TTL 边界
        # 上也算过期，避免进程恰好在 idle_ttl 整数秒时永远不被回收的边界抖动。
        stale = [
            cid for cid, t in self._last_active.items()
            if now - t >= self._cfg.idle_ttl
        ]
        for cid in stale:
            logger.info("会话 %s 空闲超 %ss，回收进程", cid, self._cfg.idle_ttl)
            await self.reset(cid)

    def start_idle_sweeper(self) -> "asyncio.Task":
        """启动后台空闲回收任务（每 60s 扫一次）。

        Why 单独任务：常驻进程数会随历史会话无限堆积，必须有 reaper。60s 粒度
        足够（idle_ttl 量级是分钟/小时），过频浪费 CPU 且频繁扫锁无意义。
        异常吞掉仅记日志——sweeper 挂了不影响主流程，下一轮自动续命。
        """
        async def _loop():
            while True:
                await asyncio.sleep(60)
                try:
                    await self._sweep_once()
                except Exception:  # noqa: BLE001
                    logger.exception("idle sweeper 异常")
        return asyncio.create_task(_loop())

    async def aclose_all(self) -> None:
        """桥退出时优雅关闭全部进程（SIGTERM/KeyboardInterrupt 收尾用）。"""
        for cid in list(self._procs.keys()):
            proc = self._procs.pop(cid, None)
            if proc is not None:
                await proc.aclose()
        self._last_active.clear()
