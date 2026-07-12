# -*- coding: utf-8 -*-
"""safety.classify 裁决测试：白名单闸 + 指令解析。纯逻辑，无 IO。"""
import pytest

from bridge.config import BridgeConfig
from bridge.safety import classify


@pytest.fixture
def cfg():
    """最小可用配置（只需 allowed_staff_ids 字段即可测 classify）。"""
    return BridgeConfig(
        app_key="k", app_secret="s",
        allowed_staff_ids=frozenset({"staffOK"}),
        claude_bin="claude", workdir="/tmp", ask_timeout=120,
        idle_ttl=900, rate_limit_per_min=10,
        session_store_path="/tmp/s.json", audit_log_path="/tmp/a.jsonl",
        log_path="/tmp/l.log",
    )


def test_non_whitelist_rejected(cfg):
    """非白名单用户：reject（静默丢弃 + 审计，不回执防探测）。"""
    v = classify(sender_staff_id="intruder", text="hi", cfg=cfg)
    assert v.action == "reject"
    assert "白名单" in v.reason


def test_whitelist_allowed(cfg):
    """白名单用户 + 普通文本：allow，文本原样透传。"""
    v = classify(sender_staff_id="staffOK", text="解释一下颈线拟合", cfg=cfg)
    assert v.action == "allow"
    assert v.command is None


@pytest.mark.parametrize("raw,cmd", [
    ("/new", "new"),
    ("/status", "status"),
    ("/help", "help"),
    ("  /new  ", "new"),         # 容忍前后空白
    ("/NEW", "new"),             # 大小写不敏感
])
def test_command_parsed(cfg, raw, cmd):
    """指令前缀正确解析为 command 动作。"""
    v = classify(sender_staff_id="staffOK", text=raw, cfg=cfg)
    assert v.action == "command"
    assert v.command == cmd


def test_non_command_slash_allowed(cfg):
    """以 / 开头但非已知指令（如文件路径）→ 当普通文本 allow，不误判。"""
    v = classify(sender_staff_id="staffOK", text="/etc/hosts 是什么", cfg=cfg)
    assert v.action == "allow"
