# -*- coding: utf-8 -*-
"""BridgeConfig.from_env 行为测试：环境变量 → 强类型配置，缺凭证/缺白名单时的门控。"""
import pytest

from bridge.config import BridgeConfig


def test_from_env_reads_all_fields(monkeypatch):
    """全凭证 + 白名单齐全时，from_env 正确解析所有字段。"""
    monkeypatch.setenv("DINGTALK_APP_KEY", "ding-test-key")
    monkeypatch.setenv("DINGTALK_APP_SECRET", "secret-test")
    monkeypatch.setenv("DINGTALK_ALLOWED_STAFF_IDS", "staffA,staffB,")
    monkeypatch.setenv("CLAUDE_BIN", "/usr/local/bin/claude")
    monkeypatch.setenv("CLAUDE_WORKDIR", "/tmp/proj")
    monkeypatch.setenv("BRIDGE_ASK_TIMEOUT", "90")
    monkeypatch.setenv("BRIDGE_IDLE_TTL", "600")
    monkeypatch.setenv("BRIDGE_RATE_LIMIT_PER_MIN", "5")

    cfg = BridgeConfig.from_env(project_root="/tmp/proj")

    assert cfg.app_key == "ding-test-key"
    assert cfg.app_secret == "secret-test"
    # 白名单去空白 + 去空串
    assert cfg.allowed_staff_ids == {"staffA", "staffB"}
    assert cfg.claude_bin == "/usr/local/bin/claude"
    assert cfg.ask_timeout == 90
    assert cfg.idle_ttl == 600
    assert cfg.rate_limit_per_min == 5


def test_from_env_uses_defaults_when_unset(monkeypatch):
    """未设可选项时走默认值（claude 走 PATH、超时 120、空闲 900、频控 10）。"""
    monkeypatch.setenv("DINGTALK_APP_KEY", "k")
    monkeypatch.setenv("DINGTALK_APP_SECRET", "s")
    monkeypatch.setenv("DINGTALK_ALLOWED_STAFF_IDS", "x")
    for k in ("CLAUDE_BIN", "CLAUDE_WORKDIR", "BRIDGE_ASK_TIMEOUT",
              "BRIDGE_IDLE_TTL", "BRIDGE_RATE_LIMIT_PER_MIN"):
        monkeypatch.delenv(k, raising=False)

    cfg = BridgeConfig.from_env(project_root="/tmp/proj")

    assert cfg.claude_bin == "claude"
    assert cfg.workdir == "/tmp/proj"
    assert cfg.ask_timeout == 120
    assert cfg.idle_ttl == 900
    assert cfg.rate_limit_per_min == 10


def test_from_env_rejects_missing_credentials(monkeypatch):
    """凭证缺失是致命错（启动即失败，优于静默连不上钉钉）。"""
    monkeypatch.delenv("DINGTALK_APP_KEY", raising=False)
    monkeypatch.delenv("DINGTALK_APP_SECRET", raising=False)
    monkeypatch.setenv("DINGTALK_ALLOWED_STAFF_IDS", "x")
    with pytest.raises(ValueError, match="DINGTALK_APP_KEY"):
        BridgeConfig.from_env(project_root="/tmp/proj")


def test_from_env_rejects_empty_whitelist(monkeypatch):
    """白名单为空 = 无人可用，也是致命错（全放行模式下唯一身份闸，不能空）。"""
    monkeypatch.setenv("DINGTALK_APP_KEY", "k")
    monkeypatch.setenv("DINGTALK_APP_SECRET", "s")
    monkeypatch.delenv("DINGTALK_ALLOWED_STAFF_IDS", raising=False)
    with pytest.raises(ValueError, match="白名单"):
        BridgeConfig.from_env(project_root="/tmp/proj")
