# -*- coding: utf-8 -*-
"""B1：trading_supervisor 三合一校验（端口/pid 文件/session 锁）+ 启停 dry-run 红线。"""
from __future__ import annotations

from trading import single_instance
from ops import trading_supervisor as s
import ops.process_topology as pt


class _FakeProc:
    def __init__(self, stdout: str, returncode: int = 0):
        self.stdout = stdout
        self.returncode = returncode


def test_port_holder_parses_netstat(monkeypatch):
    """netstat 行 → LISTENING PID（B1 三合一端口腿）。"""
    monkeypatch.setattr(pt.subprocess, "run", lambda *a, **kw: _FakeProc(
        "  TCP    0.0.0.0:8000    0.0.0.0:0    LISTENING       27592\n"))
    assert s.port_holder_pid() == 27592


def test_port_holder_none_when_free(monkeypatch):
    """无 LISTENING 行 → None（未启动）。"""
    monkeypatch.setattr(pt.subprocess, "run", lambda *a, **kw: _FakeProc(""))
    assert s.port_holder_pid() is None


def test_pid_file_owner_reads(tmp_path):
    """pid 文件首字段 → 引擎持有者 PID。"""
    from trading.single_instance import _pid_path
    p = _pid_path("sess-b1", str(tmp_path))
    p.write_text("4242 2026-08-06T00:00:00\n", encoding="utf-8")
    assert s.pid_file_owner("sess-b1", str(tmp_path)) == 4242


def test_lock_held_true_when_second_process_holds(tmp_path):
    """锁被真持有 → True（排他探测，不抢锁）。"""
    lock = single_instance.acquire("sess-b1", str(tmp_path))
    assert lock is not None
    try:
        assert s.lock_held("sess-b1", str(tmp_path)) is True
    finally:
        lock.release()


def test_lock_held_false_when_free_restores_pid_file(tmp_path):
    """锁空闲 → False，且探测不污染 pid 文件（三合一不失真）。"""
    from trading.single_instance import _pid_path
    p = _pid_path("sess-b1", str(tmp_path))
    p.write_text("7777 2026-08-06T00:00:00\n", encoding="utf-8")
    assert s.lock_held("sess-b1", str(tmp_path)) is False
    # 探测后 pid 文件内容必须原样（不能变成 supervisor 的 pid）
    assert p.read_text(encoding="utf-8").startswith("7777")


def test_consistency_ok_when_all_same(monkeypatch):
    """端口属主 == pid 文件 == 锁持有 → consistent，无 drift。"""
    monkeypatch.setattr(s, "port_holder_pid", lambda port=8000: 123)
    monkeypatch.setattr(s, "pid_file_owner", lambda *a, **kw: 123)
    monkeypatch.setattr(s, "lock_held", lambda *a, **kw: True)
    monkeypatch.setattr(s, "engine_processes", lambda: [{"pid": 123}])
    monkeypatch.setattr(s, "_client_status", lambda: {"running": True, "pid": 9})
    monkeypatch.setattr(s, "_git_rev", lambda: "abc1234")
    monkeypatch.setattr(s, "_read_runtime_session", lambda: None)
    monkeypatch.setattr(s, "_runtime_started_at", lambda: "2026-08-06T00:00:00")
    st = s.status()
    assert st["consistent"] is True
    assert st["drifts"] == []


def test_consistency_drift_port_owner_differs(monkeypatch):
    """端口属主 != pid 文件 → inconsistent + drift（拒绝启动的依据）。"""
    monkeypatch.setattr(s, "port_holder_pid", lambda port=8000: 100)
    monkeypatch.setattr(s, "pid_file_owner", lambda *a, **kw: 200)
    monkeypatch.setattr(s, "lock_held", lambda *a, **kw: True)
    monkeypatch.setattr(s, "engine_processes", lambda: [])
    monkeypatch.setattr(s, "_client_status", lambda: {"running": None, "pid": None})
    monkeypatch.setattr(s, "_git_rev", lambda: None)
    monkeypatch.setattr(s, "_read_runtime_session", lambda: None)
    monkeypatch.setattr(s, "_runtime_started_at", lambda: None)
    st = s.status()
    assert st["consistent"] is False
    assert any("端口属主" in d and "!=" in d for d in st["drifts"])


def test_start_refuses_when_inconsistent(monkeypatch):
    """三合一不一致 → start 拒绝（rc=2），绝不拉起。"""
    monkeypatch.setattr(s, "status", lambda port=8000: {
        "consistent": False, "drifts": ["端口属主 != pid 文件"], "engine_pids": []})
    called = []
    monkeypatch.setattr(s.subprocess, "run", lambda *a, **kw: called.append(a) or _FakeProc(""))
    assert s.start() == 2
    assert called == []


def test_start_uses_schtasks_when_consistent(monkeypatch):
    """一致且无引擎 → schtasks /Run QuanterServer 拉起。"""
    monkeypatch.setattr(s, "status", lambda port=8000: {
        "consistent": True, "drifts": [], "engine_pids": []})
    calls = []
    monkeypatch.setattr(s.subprocess, "run",
                        lambda *a, **kw: calls.append(a[0]) or _FakeProc("", 0))
    assert s.start() == 0
    assert any("/Run" in c and "QuanterServer" in c for c in calls)


def test_stop_dry_run_does_not_kill(monkeypatch):
    """stop 缺省 dry-run：只展示进程，不 taskkill（B1 红线）。"""
    monkeypatch.setattr(s, "engine_processes", lambda: [{"pid": 11}, {"pid": 22}])
    calls = []
    monkeypatch.setattr(s.subprocess, "run", lambda *a, **kw: calls.append(a[0]) or _FakeProc(""))
    assert s.stop() == 0
    assert not any("taskkill" in c for c in calls)


def test_stop_yes_kills_tree(monkeypatch):
    """--yes：对每个引擎 pid taskkill /F /T（树杀）。"""
    monkeypatch.setattr(s, "engine_processes", lambda: [{"pid": 11}, {"pid": 22}])
    calls = []
    monkeypatch.setattr(s.subprocess, "run", lambda *a, **kw: calls.append(a[0]) or _FakeProc(""))
    assert s.stop(yes=True) == 0
    killed = [c for c in calls if "taskkill" in c]
    assert len(killed) == 2
    assert all("/F" in c and "/T" in c for c in killed)


def test_main_defaults_to_status(monkeypatch, capsys):
    """code-review: 无参数 → 默认 --status（plan 契约），输出 JSON 拓扑。"""
    monkeypatch.setattr(s, "status", lambda port=8000, session_id=None: {"consistent": True})
    assert s.main([]) == 0
    out = capsys.readouterr().out
    assert '"consistent": true' in out
