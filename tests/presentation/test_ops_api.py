# -*- coding: utf-8 -*-
"""B2-3：/api/v1/ops/processes 端点（三合一拓扑 + 队列 + 网关态一屏）。"""
from __future__ import annotations

import asyncio
import os

from presentation.server.api.v1 import ops


def test_queue_size_counts_down_queue_bytes(tmp_path, monkeypatch):
    monkeypatch.setenv("QMT_USERDATA_PATH", str(tmp_path))
    (tmp_path / "down_queue_win_123459").write_bytes(b"x" * 10)
    assert ops._queue_size() == 10


def test_processes_endpoint_assembles_one_screen(monkeypatch):
    from trading import gateway_service as trading_service
    from ops import trading_supervisor as ts

    monkeypatch.setattr(ts, "status", lambda port=8000, session_id=None: {
        "port": 8000, "port_holder_pid": 1, "pid_file_pid": 1, "lock_held": True,
        "engine_pids": [1], "client": {"running": True, "pid": 9},
        "consistent": True, "drifts": []})
    monkeypatch.setattr(trading_service, "get_status",
                        lambda: {"connected": True, "locked": False, "mode": "live"})
    monkeypatch.setenv("QMT_USERDATA_PATH", "")

    result = asyncio.run(ops.processes())
    assert result["port_holder_pid"] == 1
    assert result["consistent"] is True
    assert result["queue_size"] == 0
    assert result["gateway_mode"]["mode"] == "live"
