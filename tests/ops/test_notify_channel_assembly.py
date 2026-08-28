# -*- coding: utf-8 -*-
"""W0（2026-08-28 全库评审 P0-2）钉死「零通道静默」回归。

事故形态：ops 三件套 notify 走裸 NotificationManager.get_default()（初始零通道），
且从不调 build_default_manager() → 晨检/EOD/看护/数据熔断告警整体静默（连
LocalFileChannel 的 alerts.log 兜底都没有——它也只在 build_default_manager 内装配）。
本测试钉死两件事：① notify 会装配通道；② 装配后真的把消息发到通道（同步语义）。
"""
from __future__ import annotations

import asyncio

import pytest

from ops import gm_ops_common, run_data_check
from infra.notifier import NotificationManager


@pytest.fixture()
def fresh_manager(monkeypatch, tmp_path):
    """隔离单例：清空通道+复位装配标志，env 注入钉钉凭证与本地落盘路径。"""
    mgr = NotificationManager.get_default()
    mgr.clear_channels()
    monkeypatch.setenv("DINGTALK_WEBHOOK", "https://oapi.dingtalk.com/robot/send?access_token=test")
    monkeypatch.setenv("DINGTALK_SECRET", "test-secret")
    # 其余网络通道凭证置空，保证只装配钉钉+本地文件两条
    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "WECOM_WEBHOOK"):
        monkeypatch.delenv(k, raising=False)
    # LocalFileChannel 无参构造吃模块常量 DEFAULT_ALERTS_LOG——测试绝不写真实
    # logs/alerts.log，装配前重定向到 tmp（构造发生在 notify→build 内，时序正确）。
    import infra.notifier as _notifier_mod
    monkeypatch.setattr(_notifier_mod, "DEFAULT_ALERTS_LOG", tmp_path / "alerts.log")
    yield mgr
    mgr.clear_channels()


def test_gm_ops_notify_assembles_channels_and_sends(fresh_manager, monkeypatch):
    sent = []

    async def fake_send(self, text):
        sent.append(text)

    from infra.notifier import DingTalkChannel
    monkeypatch.setattr(DingTalkChannel, "send", fake_send)

    gm_ops_common.notify("WARN", "通道装配回归测试")

    assert fresh_manager._channels, "notify 后通道必须已装配（P0-2：裸 get_default 零通道=静默根因）"
    assert any("通道装配回归测试" in t for t in sent), "同步语义：notify 返回前消息必须已投递通道"
    assert any("⚠" in t for t in sent), "WARN 级前缀应在正文中"


def test_run_data_check_alert_assembles_channels(fresh_manager, monkeypatch):
    sent = []

    async def fake_send(self, text):
        sent.append(text)

    from infra.notifier import DingTalkChannel
    monkeypatch.setattr(DingTalkChannel, "send", fake_send)

    run_data_check._alert("数据检查点回归测试", "ERROR")

    assert fresh_manager._channels
    assert any("数据检查点回归测试" in t for t in sent)


def test_notify_survives_channel_failure(fresh_manager, monkeypatch, tmp_path):
    """单通道故障不上抛主链（_broadcast return_exceptions=True 内部消化）——
    且 LocalFileChannel 兜底必须仍把消息落盘（钉钉挂了本地有痕，CR-7 语义）。"""
    from infra.notifier import DingTalkChannel

    async def boom(self, text):
        raise RuntimeError("模拟钉钉通道故障")

    monkeypatch.setattr(DingTalkChannel, "send", boom)
    gm_ops_common.notify("INFO", "降级路径测试")   # 不应抛
    log_file = tmp_path / "alerts.log"
    assert log_file.exists(), "网络通道全灭时 LocalFileChannel 必须留痕"
    assert "降级路径测试" in log_file.read_text(encoding="utf-8")


def test_build_default_manager_unconditional_local_file(fresh_manager):
    """LocalFileChannel 无条件装配（CR-7 告警双通道兜底）——W0 修复后 ops 侧同样享有。"""
    from infra.notifier import build_default_manager
    mgr = build_default_manager()
    kinds = {type(ch).__name__ for ch in mgr._channels}
    assert "LocalFileChannel" in kinds
    assert "DingTalkChannel" in kinds
