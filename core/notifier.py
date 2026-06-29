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
import logging
import os
import threading
from abc import ABC, abstractmethod
from typing import Literal

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
    # 装配完成标记，使后续调用幂等。
    mgr._configured = True
    return mgr
