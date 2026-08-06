# -*- coding: utf-8 -*-
"""B2-1/B2-2：miniQMT guard 探测/拉起/陈旧 WARN/队列兜底。"""
from __future__ import annotations

import os

from ops import miniqmt_guard as g
from ops import process_topology as pt


def _fake_client(status: str, pid: int | None = 9):
    return {"running": status in ("healthy", "stale"), "pid": pid,
            "count": 1 if pid else 0}


def test_check_client_healthy_when_fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(pt, "client_status", lambda: _fake_client("healthy"))
    monkeypatch.setenv("QMT_USERDATA_PATH", str(tmp_path))
    (tmp_path / "down_queue_win_123459").write_text("x")
    st = g.check_client()
    assert st["status"] == "healthy"


def test_check_client_stale_when_files_old(tmp_path, monkeypatch):
    import time as _time
    monkeypatch.setattr(pt, "client_status", lambda: _fake_client("stale"))
    monkeypatch.setattr(pt, "port_holder_pid", lambda port=8000: None)  # 无引擎
    monkeypatch.setenv("QMT_USERDATA_PATH", str(tmp_path))
    f = tmp_path / "down_queue_win_123459"
    f.write_text("x")
    old = _time.time() - 600
    os.utime(f, (old, old))
    st = g.check_client()
    assert st["status"] == "stale" and "陈旧" in st["detail"]


def test_check_client_healthy_when_engine_connected(tmp_path, monkeypatch):
    """引擎已连接（端口有属主）→ 文件陈旧不判假活（connect 返回码才是权威）。"""
    import time as _time
    monkeypatch.setattr(pt, "client_status", lambda: _fake_client("stale"))
    monkeypatch.setattr(pt, "port_holder_pid", lambda port=8000: 79788)
    monkeypatch.setenv("QMT_USERDATA_PATH", str(tmp_path))
    f = tmp_path / "down_queue_win_123459"
    f.write_text("x")
    old = _time.time() - 600
    os.utime(f, (old, old))
    st = g.check_client()
    assert st["status"] == "healthy" and "引擎已连接" in st["detail"]


def test_check_client_missing_when_no_process(monkeypatch):
    monkeypatch.setattr(pt, "client_status", lambda: _fake_client("missing", None))
    st = g.check_client()
    assert st["status"] == "missing"


def test_ensure_client_refuses_without_exe(monkeypatch):
    monkeypatch.setattr(pt, "client_status", lambda: _fake_client("missing", None))
    monkeypatch.delenv("QMT_CLIENT_EXE", raising=False)
    msg = g.ensure_client()
    assert "未配置" in msg


def test_ensure_client_launches_when_exe_set(monkeypatch):
    monkeypatch.setattr(pt, "client_status", lambda: _fake_client("missing", None))
    monkeypatch.setenv("QMT_CLIENT_EXE", r"C:\qmt\XtMiniQmt.exe")
    calls = []
    monkeypatch.setattr(g.subprocess, "Popen",
                        lambda *a, **kw: calls.append(a) or object())
    msg = g.ensure_client()
    assert "已拉起" in msg and calls


def test_ensure_client_dry_run_does_not_launch(monkeypatch):
    monkeypatch.setattr(pt, "client_status", lambda: _fake_client("missing", None))
    monkeypatch.setenv("QMT_CLIENT_EXE", r"C:\qmt\XtMiniQmt.exe")
    calls = []
    monkeypatch.setattr(g.subprocess, "Popen", lambda *a, **kw: calls.append(a))
    msg = g.ensure_client(dry_run=True)
    assert "dry-run" in msg and calls == []


def test_run_once_cleans_stale_queues(tmp_path, monkeypatch):
    import time as _time
    monkeypatch.setattr(pt, "client_status", lambda: _fake_client("healthy"))
    monkeypatch.setenv("QMT_USERDATA_PATH", str(tmp_path))
    monkeypatch.setenv("QMT_SESSION_ID", "123459")
    stale = tmp_path / "down_queue_win_111111"
    stale.write_text("x")
    old = _time.time() - 7200
    os.utime(stale, (old, old))
    res = g.run_once(dry_run=False, alert=False)
    assert any("111111" in name for name in res["cleaned"])
