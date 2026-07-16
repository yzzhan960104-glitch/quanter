# -*- coding: utf-8 -*-
"""push_brief 单测：dry-run / 成功 / 失败(returncode) / 缺凭证 / 超时 / dws 不存在。"""
import subprocess

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
