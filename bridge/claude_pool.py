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
import shutil
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
        # 是否正处在一轮 ask 当中（_read_until_result 读循环进行时为 True）。
        # Why 暴露给池：sweeper 据此跳过活进程——ask 只在完成时才更新 last_active，
        # 跑的过程中 last_active 停在旧值，纯按 last_idle 判定会把"距上一条消息
        # >idle_ttl 的长 ask"误判空闲而回收，_kill 置 _proc=None → 读循环撞
        # AttributeError（2026-07-13 22:23 实事故）。is_busy 是"有在飞请求"的硬判据。
        self._busy: bool = False

    # ---- 对外属性 ----
    @property
    def session_id(self) -> Optional[str]:
        return self._session_id

    @property
    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    @property
    def is_busy(self) -> bool:
        """是否正处在一轮 ask 读循环中。sweeper 见 True 必跳过（防误杀活进程）。"""
        return self._busy

    # ---- 进程生命周期 ----
    async def _spawn(self) -> None:
        """拉起 claude（stream-json 双流 + 全放行）。已知 session_id 则 --resume。"""
        # shutil.which 把 claude_bin 解析成完整路径：
        # Windows 上 npm 全局装的 claude 是 .cmd 批处理，asyncio.create_subprocess_exec
        # 非 shell 模式不走 PATHEXT，裸 "claude" 会让 CreateProcess FileNotFoundError（WinError 2）。
        # which 用 PATHEXT 找到 claude.CMD 完整路径，直接 spawn 完整路径可行（已实测 rc=0）。
        claude_path = shutil.which(self._cfg.claude_bin)
        if claude_path is None:
            raise RuntimeError(
                f"找不到 claude 可执行 '{self._cfg.claude_bin}'——检查 PATH 或 .env 的 CLAUDE_BIN"
            )
        cmd = [
            claude_path,
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

        # Why stderr=DEVNULL（防管道死锁，M1 修复）：
        # claude --verbose 在 stderr 上会输出可观的诊断/心跳噪声。若用 PIPE
        # 且不主动读取，OS 管道缓冲（Linux 默认 64KB，Windows 更小）写满后
        # 子进程会阻塞在 stderr write → stdout 不再产新行 → _read_until_result
        # 死等到 ask_timeout。长会话尤其易触发。
        # 这里丢弃 stderr：真正有用的事件（session_id / assistant 增量 / result）
        # 全部走 stdout 的 stream-json 帧，stderr 仅是冗余诊断噪声。
        # 若未来需要 stderr 调试，应起独立 task 持续 readline 排空，而非 PIPE 留空。
        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=self._cfg.workdir,
            # StreamReader 单行缓冲调大到 16MB：claude 复杂问题的单帧 JSON
            #（含大段代码/大文件工具结果）可远超默认 64KB，readline 会抛
            # LimitOverrunError。16MB 足以容纳任何单帧。
            limit=2 ** 24,
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
            # 读循环中被外部把 _proc 置空（/new reset、桥退出 aclose_all 等）的防御：
            # 不加本守卫，下一行 self._proc.stdout.readline() 会抛裸 AttributeError
            # （2026-07-13 22:23 实事故的报错栈即此）。改抛 RuntimeError，落到 ask
            # 的 (RuntimeError, LimitOverrunError) 重建重试链——对用户至多"慢一点
            # 重试成功"，而非以 AttributeError 上抛崩溃。
            if self._proc is None:
                raise RuntimeError(
                    "claude _proc 在读循环中被置空（可能被 reset/回收/退出杀掉）"
                )
            # 不打断 claude：readline 无超时，无限等。
            # Why 无 idle 超时：claude 深度思考/多轮工具/慢模型可能长时间产出
            # thinking_tokens 或在工具间停顿；人为设 idle 上限会误杀正常长任务
            # （用户策略：信任 claude 跑到底，只在它自己崩时才重建）。只在 stdout
            # EOF（进程退出）时抛错走重建。
            # 风险：claude 若真卡死（进程在但 stdout 永久无输出）会挂起同会话——
            # 可 Ctrl+C 重启桥或换会话（不同 conversationId 用不同进程）。
            line_bytes = await self._proc.stdout.readline()
            if not line_bytes:
                # stdout EOF = claude 自己崩/被外部杀。抛出让上层走重建。
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
            self._busy = True  # 标记"有在飞 ask"，sweeper 据此跳过本进程
            try:
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
                        # 读到 result 为止；空闲超时在 _read_until_result 内部逐行判定
                        #（claude 持续输出帧就不算超时，连续 ask_timeout 无输出才判卡死）
                        return await self._read_until_result(on_event)
                    except (RuntimeError, asyncio.LimitOverrunError) as e:
                        # 仅在 claude 自己出问题时重建：进程崩(stdout EOF)或极端单帧超限。
                        # 不打断正在思考/工作的 claude（无 idle 超时、无总时长上限、无限流）。
                        last_err = e
                        logger.warning("claude 第 %d 轮失败 (%s)，kill 后重建重试",
                                       attempt, type(e).__name__)
                        await self._kill()
                        # 循环回到 _spawn：已知 session_id 会自动 --resume 续上下文
                # 重试用尽：抛出，让上层回错误文本给钉钉（不无限重试）
                raise RuntimeError(f"claude 连续 {_MAX_ATTEMPTS} 轮失败：{last_err}")
            finally:
                self._busy = False  # 无论成功失败都归位，允许后续空闲回收


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

        只杀进程 + 清 _procs/_last_active，**不清 store 映射**——保留 session_id，
        下次同会话发消息时 _get_or_create 取 sid → --resume 续上下文。
        （reset 才清 store，那是 /new 用户主动重置专用；回收误用 reset 会丢上下文。）
        """
        now = time.monotonic()
        # 用 >= 而非 >：idle_ttl=0 表示「立即回收」（测试明意）；TTL 边界上也算过期，
        # 避免进程恰好在 idle_ttl 整数秒时永远不被回收的边界抖动。
        stale = []
        for cid, t in self._last_active.items():
            if now - t < self._cfg.idle_ttl:
                continue
            proc = self._procs.get(cid)
            # 关键守卫：正在跑 ask 的进程（is_busy）绝不回收。
            # Why：ask 只在完成时更新 last_active，跑的过程中 last_active 停在旧值，
            # 若仅按 idle 判定，"距上一条消息 >idle_ttl 的长 ask"会被误判空闲而回收——
            # _kill 置 _proc=None，_read_until_result 下轮 readline 即 AttributeError
            # （2026-07-13 22:23 实事故：99s 的 ask 被 sweeper 当空闲杀）。
            # is_busy 是"有在飞请求"的硬判据，优先于 idle_ttl。
            if proc is not None and getattr(proc, "is_busy", False):
                logger.info("会话 %s 虽空闲超 %ss，但 ask 进行中，跳过回收（防误杀活进程）",
                            cid, self._cfg.idle_ttl)
                continue
            stale.append(cid)
        for cid in stale:
            logger.info("会话 %s 空闲超 %ss，回收进程（保留 session_id，下次 --resume 续）",
                        cid, self._cfg.idle_ttl)
            proc = self._procs.pop(cid, None)
            if proc is not None:
                await proc.aclose()
            self._last_active.pop(cid, None)
            # 故意不清 store：session_id 是「进程死、上下文不丢」的关键，回收只省内存

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
