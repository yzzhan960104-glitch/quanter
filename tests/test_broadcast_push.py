# -*- coding: utf-8 -*-
"""push_brief 单测：dry-run / 成功 / 失败(returncode) / 缺凭证 / 超时 / dws 不存在
+ Windows npm .cmd 垫片绕过（多行 --text 截断修复，2026-08-16 实证）。"""
import subprocess
from pathlib import Path

from broadcast import push as push_mod


class _FakeCompleted:
    def __init__(self, returncode=0, stderr=""):
        self.returncode = returncode
        self.stdout = ""
        self.stderr = stderr


def test_push_dry_run_prints_no_subprocess(monkeypatch, capsys):
    called = []
    monkeypatch.setattr(push_mod.subprocess, "run", lambda *a, **k: called.append(1))
    ok = push_mod.push_brief("标题", "正文md", robot_code="rc", group_id="gid", dry_run=True)
    assert ok is True
    assert called == []                      # dry-run 不调 dws
    assert "正文md" in capsys.readouterr().out


def test_push_success(monkeypatch):
    monkeypatch.setattr(push_mod.subprocess, "run", lambda *a, **k: _FakeCompleted(0))
    assert push_mod.push_brief("t", "md", robot_code="rc", group_id="gid") is True


def test_push_returncode_failure_returns_false(monkeypatch):
    monkeypatch.setattr(push_mod.subprocess, "run", lambda *a, **k: _FakeCompleted(1, "boom"))
    assert push_mod.push_brief("t", "md", robot_code="rc", group_id="gid") is False


def test_push_missing_creds_returns_false(monkeypatch):
    called = []
    monkeypatch.setattr(push_mod.subprocess, "run", lambda *a, **k: called.append(1))
    assert push_mod.push_brief("t", "md", robot_code="", group_id="gid") is False
    assert push_mod.push_brief("t", "md", robot_code="rc", group_id="") is False
    assert called == []                      # 缺凭证不调 dws


def test_push_timeout_returns_false(monkeypatch):
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="dws", timeout=1)
    monkeypatch.setattr(push_mod.subprocess, "run", boom)
    assert push_mod.push_brief("t", "md", robot_code="rc", group_id="gid") is False


def test_push_dws_not_found_returns_false(monkeypatch):
    monkeypatch.setattr(push_mod.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()))
    assert push_mod.push_brief("t", "md", robot_code="rc", group_id="gid") is False


# ===========================================================================
# Windows npm .cmd 垫片绕过（多行 --text 截断修复）
#
# 根因（2026-08-16 实证）：dws 经 npm 全局安装为 .CMD 垫片，垫片尾部
# `"%_prog%" "...dws.js" %*` 由 cmd.exe 批处理展开——批处理把参数中的换行当
# 命令分隔符，多行 markdown 经垫片后只剩第一行（rc=0、stderr 空，静默截断），
# 钉钉收到正文仅一行标题 → 群里表现为「消息内容是空的」。
# ===========================================================================

FAKE_NODE = r"C:\Program Files\nodejs\node.exe"


def test_push_bypasses_cmd_shim_direct_node(monkeypatch, tmp_path):
    """垫片 + 真身 js 齐备 → 直调 [node, dws.js]，多行 markdown 必须完整出现在 argv。"""
    # 复刻本机 npm 真实布局：<prefix>\dws.CMD + <prefix>\node_modules\...\dws.js
    js = tmp_path / "node_modules" / "dingtalk-workspace-cli" / "bin" / "dws.js"
    js.parent.mkdir(parents=True)
    js.write_text("// fake dws entry", encoding="utf-8")
    shim = tmp_path / "dws.CMD"
    shim.write_text("@echo off", encoding="utf-8")

    def fake_which(name: str):
        if name == "dws":
            return str(shim)
        if name == "node":
            return FAKE_NODE
        return None

    seen = {}

    def fake_run(cmd, *a, **k):
        seen["cmd"] = cmd
        return _FakeCompleted(0)

    monkeypatch.setattr(push_mod.shutil, "which", fake_which)
    monkeypatch.setattr(push_mod.subprocess, "run", fake_run)
    md = "### 第一行\n> 第二行引用\n- 第三行"
    ok = push_mod.push_brief("标题", md, robot_code="rc", group_id="gid")
    assert ok is True
    cmd = seen["cmd"]
    # 直调 node + dws.js（绝不经过 .CMD 垫片）
    assert cmd[0] == FAKE_NODE
    assert cmd[1] == str(js)
    # 多行正文一字不差落在 --text 参数里（防截断回归）
    i = cmd.index("--text")
    assert cmd[i + 1] == md


def test_push_falls_back_to_shim_when_js_missing(monkeypatch, tmp_path):
    """真身 js 不存在（非 npm 全局布局）→ 回退垫片路径（保留缺 dws 报错语义）。"""
    seen = {}

    def fake_run(cmd, *a, **k):
        seen["cmd"] = cmd
        return _FakeCompleted(0)

    # which 指向 tmp_path（其下无 node_modules/dingtalk-workspace-cli 真身）
    monkeypatch.setattr(push_mod.shutil, "which",
                        lambda n: str(tmp_path / "dws.CMD") if n == "dws" else None)
    monkeypatch.setattr(push_mod.subprocess, "run", fake_run)
    assert push_mod.push_brief("t", "单行", robot_code="rc", group_id="gid") is True
    assert seen["cmd"][0] == str(tmp_path / "dws.CMD")   # 回退原垫片行为


def test_push_non_windows_direct_bin_passthrough(monkeypatch):
    """which 返回非垫片（如 Linux 的 /usr/bin/dws 或 dws.exe）→ 原样直调，零行为变化。"""
    seen = {}

    def fake_run(cmd, *a, **k):
        seen["cmd"] = cmd
        return _FakeCompleted(0)

    monkeypatch.setattr(push_mod.shutil, "which", lambda n: "/usr/bin/dws" if n == "dws" else None)
    monkeypatch.setattr(push_mod.subprocess, "run", fake_run)
    assert push_mod.push_brief("t", "md", robot_code="rc", group_id="gid") is True
    assert seen["cmd"][0] == "/usr/bin/dws"
