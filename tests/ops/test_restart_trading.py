# -*- coding: utf-8 -*-
"""B2：restart_trading 原子重启（默认 dry-run + 三合一拒绝）。"""
from __future__ import annotations

from ops import restart_trading as r
from ops import trading_supervisor as s


def test_restart_dry_run_does_not_kill_or_start(monkeypatch):
    """缺省 dry-run：stop(yes=False) 只展示，start 不被调。"""
    calls = []
    monkeypatch.setattr(s, "status", lambda port=8000: {
        "consistent": True, "drifts": [], "engine_pids": [11]})
    monkeypatch.setattr(s, "stop",
                        lambda port=8000, yes=False: calls.append(("stop", yes)))
    monkeypatch.setattr(s, "start", lambda port=8000: calls.append(("start",)) or 0)
    assert r.main(["restart"]) == 0
    assert ("stop", False) in calls
    assert ("start",) not in calls


def test_restart_yes_stops_then_starts(monkeypatch):
    """--yes：stop(yes=True) 后 start。"""
    calls = []
    monkeypatch.setattr(s, "status", lambda port=8000: {
        "consistent": True, "drifts": [], "engine_pids": [11]})
    monkeypatch.setattr(s, "stop",
                        lambda port=8000, yes=False: calls.append(("stop", yes)))
    monkeypatch.setattr(s, "start", lambda port=8000: calls.append(("start",)) or 0)
    assert r.main(["restart", "--yes"]) == 0
    assert ("stop", True) in calls
    assert ("start",) in calls


def test_restart_refuses_when_inconsistent(monkeypatch):
    """三合一不一致 → rc=2，stop/start 都不调（绝不顶掉旧链）。"""
    calls = []
    monkeypatch.setattr(s, "status", lambda port=8000: {
        "consistent": False, "drifts": ["端口属主 != pid 文件"], "engine_pids": []})
    monkeypatch.setattr(s, "stop", lambda **kw: calls.append("stop"))
    monkeypatch.setattr(s, "start", lambda **kw: calls.append("start"))
    assert r.main(["restart", "--yes"]) == 2
    assert calls == []


def test_status_subcommand_prints_json(monkeypatch, capsys):
    """status：打印 supervisor.status JSON。"""
    monkeypatch.setattr(s, "status", lambda port=8000: {"consistent": True})
    assert r.main(["status"]) == 0
    out = capsys.readouterr().out
    assert '"consistent": true' in out
