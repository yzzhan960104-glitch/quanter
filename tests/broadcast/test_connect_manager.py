# -*- coding: utf-8 -*-
"""connect_manager 单测：全程 mock subprocess，不真拉 dev connect / 不真调 tasklist|taskkill。

覆盖：build_cmd 两类命令组装 / PID 读写 / 防重复 / stop 树杀 /T / status 僵尸清理。
"""
from __future__ import annotations

import broadcast.connect_manager as cm

# ── fixture：等价 Task 3 的 CONNECT_BOTS / CONNECT_DEFAULTS（本任务先用字面量）──
CLS_CFG = {"unified_env": "CLI_BOT_UNIFIED_APP_ID", "channel": "claudecode"}
CUSTOM_CFG = {
    "unified_env": "REVIEW_BOT_UNIFIED_APP_ID",
    "channel": "custom",
    "agent_cmd": ".venv310/Scripts/python.exe infra/tools/dingtalk_review_bridge.py",
}
DEFAULTS = {
    "allowed_users_env": "DINGTALK_ALLOWED_STAFF_IDS",
    "workdir_env": "BROADCAST_AGENT_WORKDIR",
    "agent_memory": True,
    "approval_mode": "ask",
}


def test_build_cmd_claudecode(monkeypatch):
    """claudecode 类：全套 agent 参数，approval_mode 写死 ask，带 workdir。"""
    monkeypatch.setenv("CLI_BOT_UNIFIED_APP_ID", "f0b2740f-c029-4b99-943c-58de139c7463")
    monkeypatch.setenv("DINGTALK_ALLOWED_STAFF_IDS", "staff001")
    monkeypatch.setenv("BROADCAST_AGENT_WORKDIR", "F:/quanter")
    cmd = cm.build_cmd("cli", CLS_CFG, DEFAULTS)
    assert cmd[0:4] == ["dws", "dev", "connect", "--unified-app-id"]
    assert "f0b2740f-c029-4b99-943c-58de139c7463" in cmd
    assert "--channel" in cmd and "claudecode" in cmd
    assert "--agent-memory" in cmd                       # agent_memory=True
    assert "--agent-approval-mode" in cmd
    assert cmd[cmd.index("--agent-approval-mode") + 1] == "ask"  # C2 安全底线
    assert "--allowed-users" in cmd and "staff001" in cmd
    assert "--agent-workdir" in cmd and "F:/quanter" in cmd


def test_build_cmd_custom_review(monkeypatch):
    """custom 类（review）：channel=custom + agent-cmd 相对路径（C4），无 workdir/memory。"""
    monkeypatch.setenv("REVIEW_BOT_UNIFIED_APP_ID", "e2695383-6fe9-4617-9439-2a8538af3107")
    monkeypatch.setenv("DINGTALK_ALLOWED_STAFF_IDS", "staff001")
    cmd = cm.build_cmd("review", CUSTOM_CFG, DEFAULTS)
    assert "--channel" in cmd and "custom" in cmd
    assert "--agent-cmd" in cmd
    assert cmd[cmd.index("--agent-cmd") + 1] == CUSTOM_CFG["agent_cmd"]  # 相对路径原样
    assert "--agent-memory" not in cmd      # custom 类不带
    assert "--agent-workdir" not in cmd     # custom 类不带
    assert "--agent-approval-mode" not in cmd  # custom 类不带（agent-cmd 自管审批）


def test_build_cmd_missing_unified_raises(monkeypatch):
    """缺 unified-app-id → RuntimeError（防静默启动一个无身份的 connect）。"""
    monkeypatch.delenv("CLI_BOT_UNIFIED_APP_ID", raising=False)
    try:
        cm.build_cmd("cli", CLS_CFG, DEFAULTS)
        assert False, "应抛 RuntimeError"
    except RuntimeError:
        pass


def test_build_cmd_missing_allowed_users_raises(monkeypatch):
    """缺身份闸 → RuntimeError（C2 延伸：身份闸不可省）。"""
    monkeypatch.setenv("CLI_BOT_UNIFIED_APP_ID", "x")
    monkeypatch.delenv("DINGTALK_ALLOWED_STAFF_IDS", raising=False)
    try:
        cm.build_cmd("cli", CLS_CFG, DEFAULTS)
        assert False, "应抛 RuntimeError"
    except RuntimeError:
        pass


def test_start_writes_pid_and_detaches(monkeypatch, tmp_path):
    """start：Popen 用 DETACHED 标志 + cwd=PROJECT_ROOT，落 PID 文件。"""
    monkeypatch.setenv("CLI_BOT_UNIFIED_APP_ID", "u-cli")
    monkeypatch.setenv("DINGTALK_ALLOWED_STAFF_IDS", "s1")
    monkeypatch.setenv("BROADCAST_AGENT_WORKDIR", "F:/quanter")
    monkeypatch.setattr(cm, "_pid_file", lambda bot: tmp_path / f"{bot}.pid")
    monkeypatch.setattr(cm, "_log_file", lambda bot: tmp_path / f"{bot}.log")

    captured = {}
    class FakeProc:
        pid = 4242
    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["creationflags"] = kwargs.get("creationflags", 0)
        captured["cwd"] = kwargs.get("cwd")
        return FakeProc()
    monkeypatch.setattr(cm.subprocess, "Popen", fake_popen)

    res = cm.start("cli", CLS_CFG, DEFAULTS)
    assert res == "started"
    assert (tmp_path / "cli.pid").read_text() == "4242"
    # C4：cwd 锁项目根
    assert captured["cwd"] == cm.PROJECT_ROOT
    # 后台 detach：必须同时含两个标志
    assert captured["creationflags"] == cm.CREATE_NEW_PROCESS_GROUP | cm.DETACHED_PROCESS


def test_start_skips_when_already_running(monkeypatch, tmp_path):
    """防重复：PID 文件在 + 进程存活 → 跳过，不再 Popen。"""
    monkeypatch.setenv("CLI_BOT_UNIFIED_APP_ID", "u-cli")
    monkeypatch.setenv("DINGTALK_ALLOWED_STAFF_IDS", "s1")
    monkeypatch.setattr(cm, "_pid_file", lambda bot: tmp_path / f"{bot}.pid")
    (tmp_path / "cli.pid").write_text("9999", encoding="utf-8")
    monkeypatch.setattr(cm, "_is_alive", lambda pid: True)  # 存活

    popped = []
    monkeypatch.setattr(cm.subprocess, "Popen", lambda *a, **k: popped.append(1) or type("P", (), {"pid": 1})())

    assert cm.start("cli", CLS_CFG, DEFAULTS) == "already_running"
    assert popped == []  # 没有重复拉起


def test_start_clears_dead_pid_then_starts(monkeypatch, tmp_path):
    """PID 文件在但进程已死 → 清死文件后允许新拉起（防崩溃留死文件卡死）。"""
    monkeypatch.setenv("CLI_BOT_UNIFIED_APP_ID", "u-cli")
    monkeypatch.setenv("DINGTALK_ALLOWED_STAFF_IDS", "s1")
    monkeypatch.setattr(cm, "_pid_file", lambda bot: tmp_path / f"{bot}.pid")
    monkeypatch.setattr(cm, "_log_file", lambda bot: tmp_path / f"{bot}.log")
    (tmp_path / "cli.pid").write_text("8888", encoding="utf-8")
    monkeypatch.setattr(cm, "_is_alive", lambda pid: False)  # 死了

    monkeypatch.setattr(cm.subprocess, "Popen", lambda *a, **k: type("P", (), {"pid": 7777})())
    assert cm.start("cli", CLS_CFG, DEFAULTS) == "started"
    assert (tmp_path / "cli.pid").read_text() == "7777"  # 死 PID 被新 PID 覆盖


def test_stop_uses_tree_kill(monkeypatch, tmp_path):
    """stop：taskkill /F /T /PID —— /T 必须在场（C3 防孤儿 Claude Code）。"""
    monkeypatch.setattr(cm, "_pid_file", lambda bot: tmp_path / f"{bot}.pid")
    (tmp_path / "cli.pid").write_text("4242", encoding="utf-8")
    captured = {}
    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return type("R", (), {"returncode": 0})()
    monkeypatch.setattr(cm.subprocess, "run", fake_run)

    assert cm.stop("cli") == "stopped"
    assert captured["cmd"][0] == "taskkill"
    assert "/F" in captured["cmd"]      # 强制
    assert "/T" in captured["cmd"]      # C3：树杀（连 Claude Code 子进程）
    assert "/PID" in captured["cmd"] and "4242" in captured["cmd"]
    assert not (tmp_path / "cli.pid").exists()  # 停后清 PID 文件


def test_stop_no_pid_returns_not_running(monkeypatch, tmp_path):
    """无 PID 文件 → not_running（幂等，不报错）。"""
    monkeypatch.setattr(cm, "_pid_file", lambda bot: tmp_path / f"{bot}.pid")
    assert cm.stop("cli") == "not_running"


def test_status_running(monkeypatch, tmp_path):
    monkeypatch.setattr(cm, "_pid_file", lambda bot: tmp_path / f"{bot}.pid")
    (tmp_path / "cli.pid").write_text("4242", encoding="utf-8")
    monkeypatch.setattr(cm, "_is_alive", lambda pid: True)
    assert cm.status("cli") == "running"


def test_status_dead_clears_pid(monkeypatch, tmp_path):
    """僵尸清理：PID 文件在但进程已死 → 返 'dead' 且删 PID 文件。"""
    monkeypatch.setattr(cm, "_pid_file", lambda bot: tmp_path / f"{bot}.pid")
    (tmp_path / "cli.pid").write_text("4242", encoding="utf-8")
    monkeypatch.setattr(cm, "_is_alive", lambda pid: False)
    assert cm.status("cli") == "dead"
    assert not (tmp_path / "cli.pid").exists()


def test_status_not_running(monkeypatch, tmp_path):
    monkeypatch.setattr(cm, "_pid_file", lambda bot: tmp_path / f"{bot}.pid")
    assert cm.status("cli") == "not_running"
