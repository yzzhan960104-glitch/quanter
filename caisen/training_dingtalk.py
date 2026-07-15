# -*- coding: utf-8 -*-
"""caisen.training_dingtalk 参数审查机器人（Spec 3 §7）。

================================================================
方案纠偏（2026-07-15，权威，覆盖 brief 原文）
================================================================
brief 原写的 access_token + 「企业机器人发单聊消息」batch send API —— **作废**。
改为 webhook + stream 双通道（用户 2026-07-15 提供独立 webhook + 独立企业内部应用凭证）：

  ① 主动推报告 / 回显：**群自定义机器人 webhook**（urllib 极简，不引 requests/aiohttp 黑盒）。
     - 加签算法复用 core/notifier.py:DingTalkChannel._sign(secret)→(ts,sign)
       （HMAC-SHA256 + base64 + urlencode，物理意图见 notifier.py 注释）。
     - errcode 校验复用 DingTalkChannel._validate_response（HTTP 200 + errcode!=0 才是真失败，
       钉钉群机器人最易静默丢失的真实失败模式）。
     - 文本清洗复用 bridge/replier.clean_markdown_for_dingtalk（钉钉 Markdown 限制多）。

  ② 收审核：**企业内部应用 dingtalk-stream**（独立 REVIEW_APP_KEY/SECRET，与 bridge 物理隔离）。
     - 收到 @此机器人的消息 = 当前活跃 loop 的审核指令。
     - 白名单（REVIEW_ALLOWED_STAFF_IDS）外消息静默丢弃（防他人触发训练消耗算力）。
     - 调 orchestrator.submit_review(active_loop_id, text) 唤醒活跃 loop。

Why 主动推用 webhook 而非 batch send：群自定义机器人是单向推（无需 access_token 换取，
配置极简：webhook + 加签 secret 两值），完全满足「训练后推报告给研究员审核」的单向场景；
batch send 需企业内部应用 + access_token 缓存 + 单聊 userId 列表，复杂度与收益不匹配。
stream 仍用企业内部应用（双向收消息必须用企业内部应用凭证建 stream，群机器人无 stream 能力）。
================================================================

全局红线：全中文注释；极简（urllib，复用现成加签/校验/清洗，不造轮子）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import urllib.request
import urllib.error
from dataclasses import dataclass
from typing import Any, Optional

# 复用现成设施：清洗（bridge）+ 加签/errcode 校验（core/notifier）
from bridge.replier import clean_markdown_for_dingtalk
from core.notifier import DingTalkChannel

logger = logging.getLogger(__name__)

# webhook POST 超时（秒）。10s 足够（钉钉群机器人在国内 <1s 回包），过长会反拖 loop 主流程。
_HTTP_TIMEOUT = 10


# ================================================================
# 1. ReviewBotConfig —— 凭证装配（from_env，软降级）
# ================================================================

@dataclass(frozen=True)
class ReviewBotConfig:
    """参数审查机器人配置（环境变量装配，凭证绝不硬编码）。

    双通道凭证分离：
      - app_key/app_secret：企业内部应用凭证，用于 stream 收审核（双向）。
      - webhook/webhook_secret：群自定义机器人凭证，用于 webhook 推报告（单向）。
        webhook 可空（仅 stream 收审核，不推报告）；webhook_secret 可空（裸发，不加签）。

    软降级门控（from_env）：app_key/app_secret/allowed_staff_ids 三者缺一 → 返 None
    （stream 收审核是核心能力，缺失则机器人整体不装配，但不阻断 uvicorn 启动）。
    webhook/webhook_secret 缺失不影响装配（仅推送降级为 no-op）。
    """
    app_key: str
    app_secret: str
    webhook: str               # 可空 → DingTalkNotifier.push 软降级为 no-op
    webhook_secret: str        # 可空 → 裸发（不加签）
    allowed_staff_ids: tuple   # 白名单 staffId（防他人触发训练消耗算力）

    @classmethod
    def from_env(cls) -> Optional["ReviewBotConfig"]:
        """从 REVIEW_* 环境变量装配。stream 三件套缺一 → 返 None（软降级）。"""
        import os
        app_key = os.getenv("REVIEW_APP_KEY", "").strip()
        app_secret = os.getenv("REVIEW_APP_SECRET", "").strip()
        webhook = os.getenv("REVIEW_WEBHOOK", "").strip()
        webhook_secret = os.getenv("REVIEW_WEBHOOK_SECRET", "").strip()
        raw = os.getenv("REVIEW_ALLOWED_STAFF_IDS", "")
        staff = tuple(s.strip() for s in raw.split(",") if s.strip())

        # stream 收审核必需 app_key/secret/staff；缺 → None 软降级（不阻断 uvicorn）
        if not app_key or not app_secret or not staff:
            logger.info(
                "REVIEW_APP_KEY/SECRET/STAFF_IDS 未完整配置，"
                "参数审查机器人不装配（软降级）"
            )
            return None
        return cls(
            app_key=app_key,
            app_secret=app_secret,
            webhook=webhook,
            webhook_secret=webhook_secret,
            allowed_staff_ids=staff,
        )


# ================================================================
# 2. DingTalkNotifier —— webhook 推报告（实现 TrainingNotifier Protocol）
# ================================================================

class DingTalkNotifier:
    """webhook 推 Markdown（群机器人）。

    实现 TrainingNotifier Protocol 的 push(loop_id, text)。
    加签复用 DingTalkChannel._sign，errcode 校验复用 DingTalkChannel._validate_response。
    webhook 未配 → push 软降级为 no-op（仅 warning 日志），不抛、不阻断 loop 主流程。
    """

    def __init__(self, cfg: ReviewBotConfig) -> None:
        self._cfg = cfg

    def push(self, loop_id: str, text: str) -> None:
        """主动推 Markdown 报告到群机器人。

        物理流程：
          1) webhook 空 → 软降级 no-op（凭证只配了 stream 收审核时走此路）。
          2) clean_markdown_for_dingtalk 清洗（剥 <font>/<br>/表格分隔行等钉钉不支持项）。
          3) title 取首行去掉 # 前缀的前 40 字（钉钉群机器人 Markdown title 必填）。
          4) webhook_secret 非空 → 加签（复用 DingTalkChannel._sign，拼 timestamp=&sign= 到 url）。
          5) urllib POST（不引 requests/aiohttp，极简）。
          6) DingTalkChannel._validate_response 校验 errcode（HTTP 200 + errcode!=0 才是真失败）。

        失败仅记 warning（推送是附属通道，不应反拖垮 loop 主流程）。
        """
        if not self._cfg.webhook:
            logger.warning(
                "REVIEW_WEBHOOK 未配，无法推送 loop=%s（软降级 no-op）", loop_id
            )
            return
        try:
            cleaned = clean_markdown_for_dingtalk(text)
            # title：首行去 # 前缀，截前 40 字；空则兜底「训练报告」
            title = cleaned.split("\n")[0].lstrip("# ").strip()[:40] or "训练报告"
            payload = {
                "msgtype": "markdown",
                "markdown": {"title": title, "text": cleaned},
            }
            url = self._cfg.webhook
            # 加签：secret 非空才加（复用 DingTalkChannel._sign 的 HMAC-SHA256+base64+urlencode）
            if self._cfg.webhook_secret:
                ts, sign = DingTalkChannel._sign(self._cfg.webhook_secret)
                url = f"{url}&timestamp={ts}&sign={sign}"
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(url, data=body, method="POST")
            req.add_header("Content-Type", "application/json")
            with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            # HTTP 200 + errcode!=0 才是真失败（钉钉群机器人最易静默丢失的真实失败模式）
            DingTalkChannel._validate_response(data)
            logger.info("钉钉审查机器人推送成功 loop=%s title=%s", loop_id, title)
        except Exception as exc:  # noqa: BLE001
            # 推送是附属通道：任何失败（网络/加签错/errcode!=0）仅 warning，不反拖垮 loop
            logger.warning("钉钉审查机器人推送失败 loop=%s：%s", loop_id, exc)


# ================================================================
# 3. _NoopNotifier —— 凭证未配时的软降级替身
# ================================================================

class _NoopNotifier:
    """凭证未配（from_env 返 None）时 orchestrator 用的哑通知器。

    push 静默 no-op（logger.debug，不触网、不抛）。
    保证 orchestrator 无条件装配 notifier 时的安全降级。"""

    def push(self, loop_id: str, text: str) -> None:  # noqa: D401
        logger.debug("_NoopNotifier 静默丢弃 push loop=%s（凭证未配）", loop_id)


# ================================================================
# 4. ReviewChatbotHandler —— stream 收审核（仿 BridgeHandler）
# ================================================================

# dingtalk-stream SDK（bridge 已在用 0.24.3，接口实测）
import dingtalk_stream
from dingtalk_stream import AckMessage, ChatbotMessage


class ReviewChatbotHandler(dingtalk_stream.ChatbotHandler):
    """参数审查机器人 stream 入口：所有 @此机器人的消息 = 当前活跃 loop 的审核。

    专门审核（spec §7）：不路由分流（不像 BridgeHandler 那样 /new /status /help 三分支），
    收到的任何白名单内消息都作为审核指令喂给 orchestrator.submit_review。

    ACK 范式（同 BridgeHandler，防钉钉等不到 ACK 重投）：
      process 立即返 (STATUS_OK, 'ok')，重活（白名单校验 + submit_review）丢后台 task。
    """

    def __init__(self, cfg: ReviewBotConfig, orchestrator) -> None:
        # ChatbotHandler.__init__() 无参（bridge 实测），设 self.dingtalk_client=None + self.logger；
        # dingtalk_client 在注册到 client 时由 SDK 注入。
        super().__init__()
        self._cfg = cfg
        self._orch = orchestrator

    async def process(self, callback):  # type: ignore[override]
        """ChatbotMessage 回调：立即 ACK + 异步派发（不阻塞 SDK 主循环，仿 BridgeHandler）。

        SDK 契约（bridge 实测 0.24.3）：
          - callback.data 已 json.loads 为 dict，直接喂 ChatbotMessage.from_dict
          - 返 (code, message)，code=AckMessage.STATUS_OK(200) 表示成功
        重活异步化：submit_review 可能触发回测（数十秒），同步 await 会阻塞 SDK 主循环
        → 钉钉等不到 ACK → 重投 → 重复处理。
        """
        try:
            msg = ChatbotMessage.from_dict(callback.data)
        except Exception:  # noqa: BLE001
            # 解析失败仍 ACK OK 避免重投（坏消息重投也是坏的），仅记日志
            logger.exception("审查机器人消息解析失败，ACK 丢弃")
            return AckMessage.STATUS_OK, "ok"
        # 立即 ACK：重活丢后台 task，process 秒回
        asyncio.create_task(self._safe_dispatch(msg))
        return AckMessage.STATUS_OK, "ok"

    async def _safe_dispatch(self, msg: Any) -> None:
        """派发包装：任何异常都不外泄（asyncio task 无人 await 时异常会被静默吞）。"""
        try:
            self._dispatch(msg)
        except Exception:  # noqa: BLE001
            logger.exception("审查机器人派发异常")

    def _dispatch(self, msg: Any) -> None:
        """白名单 → 唤醒活跃 loop（测试直调本方法，跳过 SDK ACK 细节）。

        步骤（同 BridgeHandler._dispatch 的前半段，去掉 safety 闸与三分支）：
          1) 取 text.content，去 @机器人 前缀（钉钉群里 @机器人 时 content 含 "@机器人名 指令"）。
          2) sender 优先 sender_staff_id（企业内应用主标识），退化 sender_id。
          3) 白名单校验：非白名单 → 静默丢弃（防他人触发训练消耗算力）。
          4) 取 orchestrator.active_loop_id：无活跃 loop → 不 submit（防误触）。
          5) submit_review(loop_id, text) 唤醒 loop。
        """
        text = (getattr(msg.text, "content", "") or "").strip()
        # 去 @机器人 前缀（同 BridgeHandler._dispatch）：若文本以 @ 开头，去掉首个空白分隔的 token
        if text.startswith("@"):
            text = text.split(maxsplit=1)[1] if " " in text else ""
            text = text.strip()
        # sender_staff_id 优先（bridge 实测企业内应用主标识），退化 sender_id
        sender = getattr(msg, "sender_staff_id", "") or getattr(msg, "sender_id", "")
        if sender not in self._cfg.allowed_staff_ids:
            logger.info("审查机器人拒绝非白名单消息：sender=%s", sender)
            return
        loop_id = getattr(self._orch, "active_loop_id", None)
        if not loop_id:
            logger.info("审查机器人收到审核但无活跃 loop，忽略")
            return
        self._orch.submit_review(loop_id, text)


# ================================================================
# 5. start_review_bot —— lifespan 装配入口
# ================================================================

async def _run_stream(cfg: ReviewBotConfig, orchestrator) -> None:
    """阻塞协程：起 dingtalk-stream 连接收审核（独立 app 凭证，与 bridge 物理隔离）。

    直接 await client.start()（SDK 阻塞协程，内置 while True 重连）—— 不用 start_forever()：
    SDK 0.24.3 的 start_forever() 是同步函数，内部自带 `while True: asyncio.run(self.start())`，
    在 running loop 内会抛 RuntimeError。bridge 已踩过坑，这里复用相同解法。
    """
    credential = dingtalk_stream.Credential(cfg.app_key, cfg.app_secret)
    client = dingtalk_stream.DingTalkStreamClient(credential)
    handler = ReviewChatbotHandler(cfg, orchestrator)
    client.register_callback_handler(ChatbotMessage.TOPIC, handler)
    logger.info(
        "参数审查机器人 stream 启动（独立 app_key=%s…，与 bridge 物理隔离）",
        cfg.app_key[:6],
    )
    await client.start()


def start_review_bot(app, orchestrator) -> Any:
    """lifespan 装配入口：凭证齐 → 起 stream 后台 async task；不齐 → 返 None 软降级。

    返回 asyncio.Task（lifespan shutdown 时应 cancel，防悬挂），调用方挂到
    app.state.review_bot_task。凭证缺失（from_env 返 None）→ 直接返 None，
    不阻断 uvicorn 启动（与 bridge 一致的软降级哲学）。
    """
    cfg = ReviewBotConfig.from_env()
    if cfg is None:
        return None
    # 必须有 running event loop（lifespan async 上下文内调）
    task = asyncio.create_task(_run_stream(cfg, orchestrator), name="review-bot-stream")
    logger.info("参数审查机器人后台 task 已起（name=review-bot-stream）")
    return task
