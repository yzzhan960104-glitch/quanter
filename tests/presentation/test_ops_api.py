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
    # W6-A：gateway_service 引用摘除（P3 改制后端点只消费 process_topology）
    from ops import process_topology as ts

    monkeypatch.setattr(ts, "engine_processes",
                        lambda: [{"pid": 1, "name": "python"}])

    result = asyncio.run(ops.processes())
    assert result["engine_processes"][0]["pid"] == 1       # QMT 退役 P3 新契约
    assert result["server_alive"] is True
    assert "queue_size" in result
