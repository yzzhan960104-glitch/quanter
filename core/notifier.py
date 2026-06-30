"""
core/notifier.py
================
异步单例多通道预警通知管理器。

通道解耦：NotificationChannel 抽象 → TelegramChannel / WeComChannel 具体实现。
NotificationManager.notify_risk_event(msg, level) 用 asyncio.gather 并发推送所有通道，
单通道异常软降级（记日志、不阻塞其它通道）——避免一个 IM 故障导致整条预警链失效。

凭证来源：.env / 系统环境变量，**绝不硬编码 token**。
触发场景（由调用方决定，本模块只负责可靠投递）：
  - 熔断器 on_open：API 持续不可用
  - 对账 is_ok=False：持仓敞口偏差
  - 回测/实盘最大回撤触及红线、重大滑点
"""
from __future__ import annotations

import asyncio
import base64       # 钉钉加签：HMAC 摘要 → base64 编码
import hashlib      # 钉钉加签：SHA256 哈希算法
import hmac         # 钉钉加签：HMAC-SHA256 消息认证码
import logging
import os
import threading
import time          # 钉钉加签：毫秒级时间戳
import urllib.parse  # 钉钉加签：base64 串 → URL 安全的 urlencode
from abc import ABC, abstractmethod
from typing import Awaitable, Literal

import httpx

logger = logging.getLogger(__name__)

RiskLevel = Literal["INFO", "WARN", "ERROR", "CRITICAL"]

# 级别 → 前缀（emoji + 标签），便于手机端一眼分级
_LEVEL_PREFIX: dict[RiskLevel, str] = {
    "INFO": "ℹ️ [INFO]",
    "WARN": "⚠️ [WARN]",
    "ERROR": "❌ [ERROR]",
    "CRITICAL": "🚨 [CRITICAL]",
}


class NotificationChannel(ABC):
    """通知通道抽象。子类实现 send（async）。"""

    @abstractmethod
    async def send(self, text: str) -> None:
        """发送一条文本消息。失败应抛异常，由 Manager 统一软降级。"""


class TelegramChannel(NotificationChannel):
    """Telegram Bot 推送。凭证：bot token + chat id。"""

    def __init__(self, bot_token: str, chat_id: str) -> None:
        self._token = bot_token
        self._chat_id = chat_id

    async def _http_post(self, url: str, payload: dict) -> None:
        """真实 HTTP 投递（测试可 monkeypatch 本方法以脱离网络）。"""
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()

    async def send(self, text: str) -> None:
        await self._http_post(
            f"https://api.telegram.org/bot{self._token}/sendMessage",
            {"chat_id": self._chat_id, "text": text, "parse_mode": "Markdown"},
        )


class WeComChannel(NotificationChannel):
    """企业微信群机器人 Webhook。凭证：完整 webhook url。"""

    def __init__(self, webhook_url: str) -> None:
        self._url = webhook_url

    async def _http_post(self, url: str, payload: dict) -> None:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()

    async def send(self, text: str) -> None:
        await self._http_post(
            self._url,
            {"msgtype": "text", "text": {"content": text}},
        )


class NotificationManager:
    """异步单例：并发投递所有通道，单通道失败软降级。"""

    _instance: "NotificationManager | None" = None
    _singleton_lock = threading.Lock()

    def __init__(self) -> None:
        self._channels: list[NotificationChannel] = []
        # 装配标志：build_default_manager 完成装配后置 True，保证幂等。
        # 防止多次调用（lifespan reload / 测试+实盘）把同一通道重复 append，
        # 否则一条预警会被同通道投递 N 遍。
        self._configured: bool = False

    @classmethod
    def get_default(cls) -> "NotificationManager":
        """双重检查锁单例，线程安全。"""
        if cls._instance is None:
            with cls._singleton_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def add_channel(self, channel: NotificationChannel) -> None:
        self._channels.append(channel)

    def clear_channels(self) -> None:
        """测试用：清空通道，避免跨用例污染单例。同时复位装配标志，
        否则后续 build_default_manager 会因幂等短路而漏装通道。"""
        self._channels.clear()
        self._configured = False

    async def notify_risk_event(self, msg: str, level: RiskLevel = "INFO") -> list:
        """并发推送所有通道；单通道异常被捕获记日志，不向外抛。"""
        prefix = _LEVEL_PREFIX.get(level, "")
        text = f"{prefix} {msg}" if prefix else msg
        if not self._channels:
            logger.debug("NotificationManager 无可用通道，跳过：%s", text)
            return []
        # return_exceptions=True → 单通道失败不中断其它
        results = await asyncio.gather(
            *(ch.send(text) for ch in self._channels), return_exceptions=True
        )
        for ch, res in zip(self._channels, results):
            if isinstance(res, Exception):
                logger.error("通知通道 %s 投递失败：%s", type(ch).__name__, res)
        return results


def build_default_manager() -> NotificationManager:
    """
    按 .env / 环境变量装配默认通道。缺凭证则跳过该通道（不报错）。
    环境变量：TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID / WECOM_WEBHOOK
    """
    mgr = NotificationManager.get_default()
    # 幂等守卫：已装配过则直接返回，避免单例通道被重复 append（重复告警）。
    # 测试可通过 clear_channels() 复位本标志以重新装配。
    if mgr._configured:
        return mgr
    tg_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    tg_chat = os.getenv("TELEGRAM_CHAT_ID", "")
    if tg_token and tg_chat:
        mgr.add_channel(TelegramChannel(tg_token, tg_chat))
    wecom = os.getenv("WECOM_WEBHOOK", "")
    if wecom:
        mgr.add_channel(WeComChannel(wecom))
    # 钉钉机器人（Markdown + 加签）：缺一凭证则跳过该通道，不报错。
    # Why 缺一即跳过：钉钉加签必须 webhook + secret 同时有效，单独配 webhook 会
    # 在 send 时算签名失败（HMAC 的 secret 为空）导致每次告警都抛异常，徒增噪音；
    # 故在此做前置门控，凭证不齐直接不装通道。
    dt_webhook = os.getenv("DINGTALK_WEBHOOK", "")
    dt_secret = os.getenv("DINGTALK_SECRET", "")
    if dt_webhook and dt_secret:
        mgr.add_channel(DingTalkChannel(dt_webhook, dt_secret))
    # 装配完成标记，使后续调用幂等。
    mgr._configured = True
    return mgr


def fire_and_forget(coro: Awaitable) -> None:
    """从任意线程（含无事件循环的同步上下文）后台调度一个协程，不阻塞调用方。

    Why 必须用独立 daemon 线程跑 asyncio.run：
      熔断器（CircuitBreaker）的 on_open 回调常发生在数据获取线程池
      （如 starlette.concurrency.run_in_threadpool / requests 同步调用所在线程），
      这类工作线程没有运行中的事件循环。若直接 `asyncio.create_task(coro)` 或
      `asyncio.run_coroutine_threadsafe`（需先有 loop）会抛
      RuntimeError("no running event loop")，导致风控告警被静默吞掉。
      起一个 daemon 线程跑独立的 asyncio.run 是跨线程触发异步告警的最简显式做法——
      daemon 标志确保进程退出时不悬挂。异常仅记日志，避免淹没调用方主流程。
    """
    def _runner() -> None:
        try:
            asyncio.run(coro)  # type: ignore[arg-type]
        except Exception:
            logger.exception("fire_and_forget 后台协程失败")
    threading.Thread(target=_runner, daemon=True).start()


class DingTalkChannel(NotificationChannel):
    """钉钉群机器人 Webhook（Markdown + 加签）。凭证：webhook url + 加签 secret。

    加签算法（钉钉官方，显式实现，无黑盒）：
        sign = urlencode( base64( HMAC-SHA256(secret, f"{timestamp}\n{secret}") ) )
    物理意图：timestamp 防重放（同一签名 1 小时内有效），HMAC-SHA256 防伪造
    （只有持有 secret 的双方能算出同一 sign），base64+urlencode 让二进制摘要
    安全地出现在 URL query string 里（不含 / + = 等会被截断的字符）。
    """

    def __init__(self, webhook: str, secret: str) -> None:
        self._webhook = webhook
        self._secret = secret

    @staticmethod
    def _sign(secret: str) -> "tuple[str, str]":
        """返回 (timestamp 毫秒字符串, sign)。每次调用生成新时间戳防重放。"""
        timestamp = str(round(time.time() * 1000))  # 钉钉要求毫秒级
        string_to_sign = f"{timestamp}\n{secret}"
        digest = hmac.new(secret.encode("utf-8"),
                          string_to_sign.encode("utf-8"), hashlib.sha256).digest()
        # quote_plus 对 base64 串里的 + / = 做 URL 安全转义
        sign = urllib.parse.quote_plus(base64.b64encode(digest))
        return timestamp, sign

    async def _post(self, url: str, payload: dict) -> None:
        """真实 aiohttp 投递（测试 monkeypatch 本方法以脱网）。

        Why 用 aiohttp 而非 httpx：通道间隔离，避免单通道阻塞影响其它通道的
        并发投递语义（NotificationManager 用 asyncio.gather 并发各通道 send）。
        aiohttp 在 spec(T1) 中已锁定。
        """
        import aiohttp
        timeout = aiohttp.ClientTimeout(total=10.0)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload) as resp:
                resp.raise_for_status()

    async def send(self, text: str) -> None:
        timestamp, sign = self._sign(self._secret)
        url = f"{self._webhook}&timestamp={timestamp}&sign={sign}"
        # Markdown + 固定安全词【Quanter】（text 已含 Manager 拼的级别前缀）
        await self._post(url, {
            "msgtype": "markdown",
            "markdown": {"title": "【Quanter】风控告警",
                         "text": f"**【Quanter】**\n\n{text}"},
        })
