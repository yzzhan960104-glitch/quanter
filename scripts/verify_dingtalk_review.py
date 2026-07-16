# -*- coding: utf-8 -*-
"""Spec3 钉钉双通道验证（临时脚本，验证 webhook 推 + stream 收，不进生产）。

用法：
  python scripts/verify_dingtalk_review.py webhook   # 只测推送（立即反馈 errcode）
  python scripts/verify_dingtalk_review.py stream    # 只测收消息（起 stream 等你 @机器人）
  python scripts/verify_dingtalk_review.py           # all（先推后收）

前置：.env 已配 REVIEW_APP_KEY/SECRET/WEBHOOK/WEBHOOK_SECRET/ALLOWED_STAFF_IDS。
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# Windows 控制台默认 GBK，print emoji 会 UnicodeEncodeError；强制 stdout/stderr utf-8
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
from dotenv import load_dotenv
load_dotenv()

import dingtalk_stream
from dingtalk_stream import AckMessage, ChatbotMessage

from caisen.training_dingtalk import ReviewBotConfig, DingTalkNotifier


def test_webhook(cfg: ReviewBotConfig) -> None:
    """webhook 推一条测试 markdown。看群是否收到 + 日志 errcode。"""
    print("=" * 56)
    print("1️⃣  webhook 推送测试")
    print("=" * 56)
    notifier = DingTalkNotifier(cfg)
    notifier.push(
        "verify-test",
        "## 🔍 Spec3 钉钉 webhook 推送测试\n\n"
        "群内看到此条 = **推送通道 OK**。\n\n"
        "- errcode 见上方日志（0=成功 / 310000=加签不对 / 300001=关键词不对）",
    )
    print("→ 推送已尝试。看群消息 + 上方 DingTalkNotifier 日志的 errcode。\n")


class VerifyHandler(dingtalk_stream.ChatbotHandler):
    """最小收消息 handler：打印每条 @消息（不依赖 orchestrator）。"""

    async def process(self, callback):  # type: ignore[override]
        try:
            msg = ChatbotMessage.from_dict(callback.data)
            sender = getattr(msg, "sender_staff_id", "") or getattr(msg, "sender_id", "?")
            text = getattr(msg.text, "content", "") or ""
            print(f"\n✅✅✅ 收到 @消息！sender={sender}  text={text!r}")
            print("→ stream 收消息通道 OK（spec3 ReviewChatbotHandler 可据此唤醒 loop）")
        except Exception as exc:
            print(f"消息解析异常：{type(exc).__name__}: {exc}")
        return AckMessage.STATUS_OK, "ok"


async def test_stream(cfg: ReviewBotConfig, timeout: int = 600) -> None:
    """起 stream 连接，timeout 秒内等你 @机器人。连上 = 应用已开 Stream 能力。"""
    print("=" * 56)
    print(f"2️⃣  stream 收消息测试（app_key={cfg.app_key[:12]}…）")
    print("=" * 56)
    credential = dingtalk_stream.Credential(cfg.app_key, cfg.app_secret)
    client = dingtalk_stream.DingTalkStreamClient(credential)
    client.register_callback_handler(ChatbotMessage.TOPIC, VerifyHandler())
    print("→ 连接 stream 中…（连上即说明应用已开「机器人能力 + Stream 模式」）")
    print(f"→ 请在钉钉群 @此机器人 发任意消息，{timeout}s 内验证能否收到。\n")
    try:
        await asyncio.wait_for(client.start(), timeout=timeout)
    except asyncio.TimeoutError:
        print(f"\n⏱ {timeout}s 超时退出。期间若未收到 @消息 → 检查：")
        print("  1) 应用是否在钉钉开放平台开启「机器人」能力")
        print("  2) 消息接收模式是否选「Stream」（非 HTTP）")
        print("  3) 机器人是否已加入到你 @的群")
    except Exception as exc:
        print(f"\n❌ stream 连接失败：{type(exc).__name__}: {exc}")
        print("→ 多半应用未开 Stream，或 app_key/secret 不对。")


def main() -> None:
    cfg = ReviewBotConfig.from_env()
    if cfg is None:
        print("❌ REVIEW_* 凭证未完整配置（检查 .env 的 REVIEW_APP_KEY/SECRET/STAFF_IDS）")
        sys.exit(1)
    print(f"配置：webhook={'有' if cfg.webhook else '无'}  "
          f"webhook_secret={'有' if cfg.webhook_secret else '无(裸发)'}  "
          f"staff_ids={cfg.allowed_staff_ids}\n")
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    if mode in ("webhook", "all"):
        test_webhook(cfg)
    if mode in ("stream", "all"):
        asyncio.run(test_stream(cfg))


if __name__ == "__main__":
    main()
