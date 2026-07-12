# -*- coding: utf-8 -*-
"""
bridge/stream_client.py
=======================
dingtalk-stream 装配 + 消息派发 + 审计。

BridgeHandler.process 是钉钉消息入口：
  1. 解析 ChatbotMessage（text / sender_staff_id / conversation_id）
  2. safety.classify 裁决
  3. 立即 ACK（return (AckMessage.STATUS_OK, 'ok')）—— 防钉钉等不到 ACK 重投
  4. 重活（claude）走 asyncio.create_task 异步派发，不阻塞 SDK 主循环

派发分支：
  reject  → 静默（+ 审计，不回执防探测者确认机器人存活）
  command → 执行 /new /status /help（+ 审计 + 回执）
  allow   → pool.ask（挂 alarmer 监听事件）→ reply 分段回复（+ 审计）

频控：单用户 60s 内 > rate_limit_per_min 条 → 回"太快了"，防刷 + 钉钉频控。

SDK 字段以 Task 8 Step 0 实测 dingtalk-stream 0.24.3 为准：
  - process(self, callback: CallbackMessage) → tuple(code, message)
  - callback.data 已是 json.loads 后的 dict，直接喂 ChatbotMessage.from_dict
  - ChatbotMessage 字段：text.content / sender_staff_id / conversation_id / message_id
  - AckMessage.STATUS_OK == 200
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import defaultdict, deque
from typing import Any, Callable, Optional

from bridge.alarmer import Alarmer
from bridge.claude_pool import ClaudePool
from bridge.config import BridgeConfig
from bridge.replier import reply as reply_text_chunks
from bridge.safety import classify

logger = logging.getLogger(__name__)

# dingtalk-stream SDK（Task 8 Step 0 已核对真实接口，0.24.3）
import dingtalk_stream
from dingtalk_stream import AckMessage, ChatbotMessage


class BridgeHandler(dingtalk_stream.ChatbotHandler):
    """钉钉消息 → 安全闸 → 派发。

    继承 ChatbotHandler 获得 reply_text 等 @回复方法（生产用）；
    测试通过注入 reply_fn 替身避免真发钉钉。
    """

    def __init__(
        self,
        cfg: BridgeConfig,
        pool: ClaudePool,
        alarmer: Alarmer,
        reply_fn: Optional[Callable] = None,
    ) -> None:
        # ChatbotHandler.__init__() 无参（Step 0 实测），设置 self.dingtalk_client=None
        # 与 self.logger；dingtalk_client 在注册到 client 时由 SDK 注入。
        super().__init__()
        self._cfg = cfg
        self._pool = pool
        self._alarmer = alarmer
        # reply_fn 可注入（测试）；默认用 replier.reply（分段+清洗+@回复）
        self._reply_fn = reply_fn or reply_text_chunks
        # 频控：sender_staff_id → 最近时间戳队列（monotonic 不受系统时钟跳变影响）
        self._rate: dict[str, deque[float]] = defaultdict(deque)

    # ---- SDK 入口 ----
    async def process(self, callback):  # type: ignore[override]
        """ChatbotMessage 回调。立即 ACK + 异步派发（不阻塞 SDK 主循环）。

        SDK 契约（Step 0 实测 raw_process）：
          - 入参 callback 是 CallbackMessage，其 .data 已 json.loads 为 dict
          - 返回 (code, message)，code 用 AckMessage.STATUS_OK=200 表示成功
        重活必须异步化：claude 一轮可能跑数十秒，若在 process 内同步 await，
        SDK 主循环会被阻塞 → 钉钉侧等不到 ACK → 触发重投 → 重复处理。
        """
        try:
            # callback.data 已是 dict（CallbackMessage.from_dict 里 json.loads 过）
            msg = ChatbotMessage.from_dict(callback.data)
        except Exception:  # noqa: BLE001
            # 解析失败：仍 ACK OK 避免重投（坏消息重投也是坏的），仅记日志
            logger.exception("ChatbotMessage 解析失败，ACK 丢弃")
            return AckMessage.STATUS_OK, "ok"
        # 立即 ACK：把重活丢给后台 task，process 本身秒回
        asyncio.create_task(self._safe_dispatch(msg))
        return AckMessage.STATUS_OK, "ok"

    async def _safe_dispatch(self, msg: Any) -> None:
        """派发包装：任何异常都不外泄（避免 asyncio 吞 traceback 仅打一行）。

        异步 task 里抛的异常默认被 asyncio 收集到 task 的 exception 里，
        若无人 await 该 task，异常会被静默丢弃——这里主动 try/except + 日志兜底。
        """
        try:
            await self._dispatch(msg)
        except Exception:  # noqa: BLE001
            logger.exception("派发异常")

    async def _dispatch(self, msg: Any) -> None:
        """实际派发逻辑（测试直接调本方法，跳过 SDK ACK 细节）。

        步骤：解析文本/发送者 → 安全闸裁决 → 审计 → 按裁决三分支派发。
        """
        # 钉钉 SDK 字段（Step 0 实测）：text.content / sender_staff_id /
        # conversation_id / message_id。sender_staff_id 优先（企业内应用主标识），
        # 退化到 sender_id（部分场景 staff_id 可能为空）。
        text = (getattr(msg.text, "content", "") or "").strip()
        # 去掉 @机器人 前缀：钉钉群里 @机器人 时 content 可能含 "@机器人名 问题"，
        # safety 的指令解析取首词，若首词是 "@xxx" 会误判——剥离后再分类。
        # 简化处理：若文本以 @ 开头，去掉首个空白分隔的 token。
        if text.startswith("@"):
            text = text.split(maxsplit=1)[1] if " " in text else ""
            text = text.strip()
        sender = getattr(msg, "sender_staff_id", "") or getattr(msg, "sender_id", "")
        conv_id = getattr(msg, "conversation_id", "") or "unknown"

        # 安全闸裁决（纯逻辑，无副作用）
        verdict = classify(sender, text, self._cfg)
        # 先落审计（无论哪个分支都有此条记录，事后追溯底线）
        self._audit(msg, text, sender, conv_id, verdict.action)

        if verdict.action == "reject":
            # 静默：不回执（防探测者通过回执确认机器人存活），仅审计已落盘
            logger.info("拒绝非白名单消息：sender=%s", sender)
            return

        if verdict.action == "command":
            await self._handle_command(msg, conv_id, verdict.command)
            return

        # allow：频控 → 派发 claude
        if not self._rate_allow(sender):
            # 频控触发：回执提示用户慢下来（避免钉钉侧频控直接吞消息无反馈）
            await self._reply_fn(self, msg, "太快了，稍候再试 ⏳")
            return
        await self._ask_claude(msg, conv_id, sender, text)

    # ---- 分支实现 ----
    async def _handle_command(self, msg: Any, conv_id: str,
                              command: Optional[str]) -> None:
        """执行已知指令：/new /status /help。不进 claude（轻量本地处理）。"""
        if command == "new":
            # 重置会话：杀进程 + 清映射 + 清 store（ClaudePool.reset 三清）
            await self._pool.reset(conv_id)
            await self._reply_fn(self, msg, "✅ 会话已重置（上下文清空，下次开新会话）")
        elif command == "status":
            # 活跃会话快照（运维观测用）
            stats = self._pool.status()
            if not stats:
                await self._reply_fn(self, msg, "🤖 桥状态：\n（无活跃会话）")
                return
            lines = ["🤖 桥状态："]
            for s in stats:
                # 会话 ID 截断前 12 字符防刷屏；alive/session_id 有无用"有/无"中文化
                lines.append(
                    f"- {s['conversation_id'][:12]}… alive={s['alive']} "
                    f"sid={'有' if s['session_id'] else '无'}"
                )
            await self._reply_fn(self, msg, "\n".join(lines))
        elif command == "help":
            await self._reply_fn(self, msg, _HELP_TEXT)

    async def _ask_claude(self, msg: Any, conv_id: str,
                          sender: str, text: str) -> None:
        """喂给 pool + 挂事件监听(本地可见/钉钉进度/高危告警) → reply。

        复杂问题 claude 可能跑数分钟甚至数十分钟，若只等 result 帧才回复，用户
        盯着空气无法判断卡在哪（实测有 34 分钟无反馈的 case）。故 on_event 做三件事：
          ① 本地 logger.info 实时打印 claude 思考文本/工具调用（定位卡住根因）；
          ② 节流(15s)推钉钉进度（用户手机端看到 claude 在动，不干等）；
          ③ 高危工具调用实时告警（全放行纵深防御③：事中知情）。
        钉钉进度用 asyncio.create_task fire-and-forget，不阻塞 claude 读循环。
        """
        started = time.monotonic()
        logger.info("→ claude 开始处理 (sender=%s): %s", sender,
                    text[:80].replace("\n", " "))
        progress = {"chars": 0, "tools": [], "last_text": "", "last_push": 0.0}

        def on_event(event: dict) -> None:
            if event.get("type") == "assistant":
                content = event.get("message", {}).get("content", [])
                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        btype = block.get("type")
                        if btype == "text" and block.get("text"):
                            snippet = block["text"]
                            progress["chars"] += len(snippet)
                            progress["last_text"] = snippet
                            # ① 本地终端实时打印 claude 思考（定位卡住的关键可见性）
                            logger.info("[claude 思考] %s",
                                        snippet[:300].replace("\n", " "))
                        elif btype == "tool_use":
                            name = block.get("name", "?")
                            inp = json.dumps(block.get("input", {}),
                                             ensure_ascii=False)
                            progress["tools"].append(name)
                            logger.info("[claude 工具] %s | %s", name, inp[:150])
                            # ② 节流推钉钉进度（每 15s 一次，防刷屏 + 钉钉频控）
                            now = time.monotonic()
                            if now - progress["last_push"] > 15:
                                progress["last_push"] = now
                                elapsed = int(now - started)
                                tip = (f"⏳ 思考中 {elapsed}s · 工具 "
                                       f"{len(progress['tools'])} 次(近:{name})"
                                       f" · 已输出 {progress['chars']} 字")
                                asyncio.create_task(self._reply_fn(self, msg, tip))
            # ③ 高危工具调用实时告警（全放行纵深防御③：事中知情）
            self._alarmer.check_event(event, sender_staff_id=sender)

        try:
            answer = await self._pool.ask(conv_id, text, sender, on_event=on_event)
            logger.info("← claude 完成 (%ds, 工具 %d 次, 输出 %d 字)",
                        int(time.monotonic() - started),
                        len(progress["tools"]), progress["chars"])
        except Exception as e:  # noqa: BLE001
            # 失败诊断：运行时长/工具次数/输出字数/最后动作，定位卡住根因
            elapsed = int(time.monotonic() - started)
            last = progress["tools"][-1] if progress["tools"] else (
                progress["last_text"][:60] or "(无)")
            logger.exception(
                "claude 处理失败 (运行 %ds, 工具 %d 次, 输出 %d 字, 最后动作: %s)",
                elapsed, len(progress["tools"]), progress["chars"], last)
            answer = (f"⚠️ claude 处理失败（{elapsed}s 后）：{e}\n"
                      f"已运行：工具 {len(progress['tools'])} 次，"
                      f"输出 {progress['chars']} 字，最后动作：{last}")
        await self._reply_fn(self, msg, answer)

    # ---- 频控 ----
    def _rate_allow(self, sender: str) -> bool:
        """滑窗频控：60s 内不超过 rate_limit_per_min 条。

        Why 滑窗而非固定窗口：固定窗口在边界处会被瞬时打穿（59s 末 + 61s 初
        各打满额度 = 2x 突刺）；滑窗始终看最近 60s，平滑无突刺。
        Why monotonic：系统时钟可能被 NTP 调整/手动改，monotonic 单调递增，
        频控判定更稳。
        """
        now = time.monotonic()
        q = self._rate[sender]
        # 弹出所有超过 60s 的旧时间戳
        while q and now - q[0] > 60.0:
            q.popleft()
        if len(q) >= self._cfg.rate_limit_per_min:
            return False
        q.append(now)
        return True

    # ---- 审计 ----
    def _audit(self, msg: Any, text: str, sender: str,
               conv_id: str, action: str) -> None:
        """追加一行 jsonl 到审计日志（全放行模式事后追溯底线）。

        Why 不缓冲：直接 append，每条消息一行——进程崩溃也不丢已落盘记录。
        Why 截断 text 到 500：防巨量内容（如用户粘贴整份文件）撑爆审计文件。
        落盘失败仅记日志，不影响主流程（审计是纵深防御的"事后"层，不能反拖垮主链）。
        """
        rec = {
            "ts": time.time(),   # epoch 秒（时间戳溯源用，事后按时间检索）
            "msg_id": getattr(msg, "message_id", "") or getattr(msg, "msg_id", ""),
            "sender_staff_id": sender,
            "conversation_id": conv_id,
            "text": text[:500],   # 截断防巨量日志
            "action": action,
        }
        try:
            # 父目录可能不存在（首次运行），makedirs 兜底；exist_ok 容忍并发创建
            os.makedirs(os.path.dirname(self._cfg.audit_log_path) or ".", exist_ok=True)
            with open(self._cfg.audit_log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001
            logger.exception("审计落盘失败")


_HELP_TEXT = (
    "🤖 钉钉→claude 旁路桥\n"
    "- 直接发消息 = 与本机 claude 对话（全放行模式，等同终端）\n"
    "- /new 重置当前会话上下文\n"
    "- /status 查看桥的活跃会话\n"
    "- /help 显示本帮助\n"
    "⚠️ 全放行：claude 可读写文件/跑命令，高危操作会实时告警。"
)


def build_and_run(cfg: BridgeConfig) -> None:
    """装配 Stream 客户端 + Handler 并阻塞运行（入口 __main__ 调用）。

    装配顺序：
      - 同步阶段（无 loop）：SessionStore → ClaudePool → Alarmer →
        dingtalk_stream.Credential → DingTalkStreamClient → 注册 BridgeHandler。
      - 异步阶段（_run 协程内，已有 running loop）：启动空闲回收 sweeper →
        client.start() 阻塞 → finally 取消 sweeper + aclose_all 优雅收尾。

    Why 直接 await client.start() 而非 SDK 的 start_forever()（M5 修复 + SDK 实测）：
      SDK 0.24.3 的 start_forever() 是同步函数，内部自带 `while True: asyncio.run(self.start())`
      的重连循环。若我们 await start_forever() 会立刻抛 RuntimeError: "asyncio.run()
      cannot be called from a running event loop"。即使不 await、直接同步调它，
      也意味着 sweeper / aclose_all 跑在它内部建的 loop 上，进程退出时我们
      无法在同 loop 里收尾（loop 已关）→ aclose_all 跨 loop RuntimeError。
      故改为：绕过 start_forever，直接 await client.start()（SDK 的真正协程，
      内部已含 while True 网络重连），由我们自己的 asyncio.run 拥有 loop，
      sweeper 与 aclose_all 同 loop。

    KeyboardInterrupt（Ctrl-C 退出）会被 asyncio.run 转成 KeyboardInterrupt 上抛，
    本函数不拦——交由 __main__ 让进程整体退出；finally 已保证 sweeper/aclose 在
    KeyboardInterrupt 触发的 loop 收尾阶段执行（asyncio.run 的 finally 会跑 cleanup）。
    """
    from bridge.session_store import SessionStore

    store = SessionStore(cfg.session_store_path)
    pool = ClaudePool(cfg, store)
    alarmer = Alarmer()

    credential = dingtalk_stream.Credential(cfg.app_key, cfg.app_secret)
    client = dingtalk_stream.DingTalkStreamClient(credential)
    handler = BridgeHandler(cfg=cfg, pool=pool, alarmer=alarmer)
    # ChatbotMessage.TOPIC 是 SDK 约定的机器人消息主题字符串
    client.register_callback_handler(ChatbotMessage.TOPIC, handler)

    logger.info("钉钉桥启动，工作目录=%s", cfg.workdir)

    # 单 loop 包裹：主循环 + 收尾共享同一个 asyncio.run 建的 loop。
    async def _run() -> None:
        # 启动后台空闲回收（每 60s 扫一次，超 idle_ttl 的常驻进程被 reset 回收）。
        # 必须在 loop 内建：create_task 需 running loop。
        sweeper = pool.start_idle_sweeper()
        try:
            # client.start() 是 SDK 的阻塞协程（内部 while True 接钉钉 stream，
            # 含网络异常自动重连）。直接 await——不要用 SDK 的 start_forever()。
            await client.start()
        finally:
            # 收尾：无论正常退出还是异常，都取消 sweeper + 关全部常驻 claude 进程，
            # 防僵尸子进程残留。
            sweeper.cancel()
            await pool.aclose_all()

    asyncio.run(_run())
