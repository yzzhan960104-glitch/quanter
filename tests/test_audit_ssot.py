# -*- coding: utf-8 -*-
"""A6：audit_ssot 进程拓扑三项（引擎数 / 客户端进程 / 端口属主一致性），弃 wmic。"""
from __future__ import annotations

import scripts.audit_ssot as a


class _FakeProc:
    def __init__(self, stdout: str = "", returncode: int = 0):
        self.stdout = stdout
        self.returncode = returncode


def test_engine_process_count_ok_when_single(monkeypatch):
    """恰好 1 个 -m trading 进程 → None（不告警）。"""
    monkeypatch.setattr(a, "_engine_processes", lambda: [{"pid": 1, "cmdline": "-m trading"}])
    assert a.check_engine_process_count() is None


def test_engine_process_count_fails_when_two(monkeypatch):
    """≥2 个引擎进程 → 告警文案（C-5 单例红线）。"""
    monkeypatch.setattr(a, "_engine_processes", lambda: [
        {"pid": 1, "cmdline": "-m trading"}, {"pid": 2, "cmdline": "-m trading"}])
    msg = a.check_engine_process_count()
    assert msg is not None and "引擎进程数 2 > 1" in msg


def test_engine_processes_parses_powershell_json(monkeypatch):
    """_engine_processes 解析 PowerShell JSON，只留 -m trading / uvicorn main。"""
    monkeypatch.setattr(a.subprocess, "run", lambda *args, **kw: _FakeProc(
        '[{"ProcessId": 11, "ExecutablePath": "x", "CommandLine": "python -m trading"},'
        '{"ProcessId": 22, "ExecutablePath": "x", "CommandLine": "python -c print(1)"}]'))
    procs = a._engine_processes()
    assert [p["pid"] for p in procs] == [11]


def test_client_process_ok_when_one(monkeypatch):
    """恰好 1 个 XtMiniQmt → None。"""
    monkeypatch.setattr(a.subprocess, "run", lambda *args, **kw: _FakeProc("44044\n"))
    assert a.check_client_process() is None


def test_client_process_fails_when_missing(monkeypatch):
    """0 个客户端 → 告警（不能假装活）。"""
    monkeypatch.setattr(a.subprocess, "run", lambda *args, **kw: _FakeProc(""))
    msg = a.check_client_process()
    assert msg is not None and "进程数 0 != 1" in msg


def test_port_owner_consistency_ok_when_same(monkeypatch):
    """端口属主 == pid 文件 → None。"""
    monkeypatch.setattr(a, "_port_holder_pid", lambda port=8000: 123)
    monkeypatch.setattr(a, "_pid_file_owner", lambda *x, **kw: 123)
    assert a.check_port_owner_consistency() is None


def test_port_owner_consistency_drift(monkeypatch):
    """端口属主 != pid 文件 → 告警（旧链/非法链）。"""
    monkeypatch.setattr(a, "_port_holder_pid", lambda port=8000: 100)
    monkeypatch.setattr(a, "_pid_file_owner", lambda *x, **kw: 200)
    msg = a.check_port_owner_consistency()
    assert msg is not None and "!= pid 文件" in msg


def test_port_holder_parses_netstat(monkeypatch):
    """netstat 行 → LISTENING PID。"""
    monkeypatch.setattr(a.subprocess, "run", lambda *args, **kw: _FakeProc(
        "  TCP    0.0.0.0:8000    0.0.0.0:0    LISTENING       27592\n"))
    assert a._port_holder_pid() == 27592
