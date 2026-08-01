# -*- coding: utf-8 -*-
"""组件4 辅助 DingTalkLog：patch fire_and_forget 真推 + 落日志（spec §4 真推钉钉）。

物理意图：notify_*/push_* 真调钉钉 API 推测试群（验证推送链路），同时收集推送日志供
ReportBuilder §4（每条：时点+机器人+内容摘要+成功/失败）。enabled=False 时 fallback 全 mock。
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from unittest.mock import patch

from trading import clock


@dataclass
class DingTalkLog:
    """钉钉推送日志收集器（真推 + 落表）。

    Args:
        enabled: True=真推（patch fire_and_forget 透传 + 记录）；False=mock 不真推。
    """
    enabled: bool = True
    records: list[dict] = field(default_factory=list)
    _records: list[dict] = field(default_factory=list)  # 别名（测试用）

    def __post_init__(self):
        self._records = self.records  # 同一引用

    @contextmanager
    def collect(self):
        """patch infra.notifier.fire_and_forget：透传真推（enabled）+ 落记录。

        Why patch fire_and_forget 而非 notify_*：fire_and_forget 是推送统一异步入口（防阻塞），
        patch 它可在不阻断回放的前提下记录 + 控制真推/ mock。
        """
        original_faf = None
        try:
            from infra import notifier
            original_faf = notifier.fire_and_forget
        except Exception:
            pass

        def _wrapped(coro):
            # enabled=True 透传真推（包 _runner 落审计记录）；False 直接弃 coro（mock）。
            # Why 单参 coro（V5 review CV-1 收紧）：infra.notifier.fire_and_forget 源码签名
            # 是 fire_and_forget(coro: Awaitable) -> None（单参），*args/**kwargs 是过度设计——
            # 一旦调用方传额外参数且 enabled=True，original_faf(coro, *args) 会 TypeError
            # （fire_and_forget() takes 1 positional argument but N were given）。
            if not (self.enabled and original_faf is not None):
                coro.close()  # 关掉未 await 的 coro（避免 warning）
                return
            # kind 必须在包装【前】从原始协程捕获（cr_code.co_qualname，如 notify_trade_event），
            # 否则拿到的是 _runner 自身限定名（full_run 实测显示 "coroutine"）。
            # 实测本仓库编译环境 cr_code.co_qualname 为 None，但协程对象 __qualname__
            # 属性有真实限定名（如 NotificationManager.notify_trade_event）→ 用它。
            kind = getattr(coro, "__qualname__", type(coro).__name__)

            async def _runner():
                """await 原始推送 + 落 records（time/kind/success/error，design §4.5）。"""
                try:
                    await coro
                    self.records.append({"time": clock.now().isoformat(), "kind": kind,
                                         "success": True})
                except Exception as exc:
                    self.records.append({"time": clock.now().isoformat(), "kind": kind,
                                         "success": False, "error": str(exc)})
                    raise

            return original_faf(_runner())

        with patch("infra.notifier.fire_and_forget", _wrapped):
            yield self
