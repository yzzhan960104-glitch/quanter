# -*- coding: utf-8 -*-
"""B3：QUANTER_TESTING=1 测试隔离——pytest 不 bind 8000、不抢生产 session。"""
from __future__ import annotations

import trading.__main__ as m


def test_assert_single_instance_skips_when_testing(monkeypatch):
    """QUANTER_TESTING=1 且端口被占 → 不抛 SystemExit（测试进程允许并存）。"""
    monkeypatch.setenv("QUANTER_TESTING", "1")
    monkeypatch.setattr(m, "_port_holder_alive", lambda port: 12345)
    m._assert_single_instance()


def test_assert_single_instance_still_blocks_outside_testing(monkeypatch):
    """无 QUANTER_TESTING → 端口被占仍 SystemExit(1)（生产红线不放松）。"""
    import pytest

    monkeypatch.delenv("QUANTER_TESTING", raising=False)
    monkeypatch.setattr(m, "_port_holder_alive", lambda port: 12345)
    monkeypatch.setattr(m, "_alert_critical", lambda msg: None)
    with pytest.raises(SystemExit) as ei:
        m._assert_single_instance()
    assert ei.value.code == 1


def test_run_server_skips_port_assert_in_testing(monkeypatch):
    """QUANTER_TESTING=1 → run_server 不调 _assert_single_instance。"""
    monkeypatch.setenv("QUANTER_TESTING", "1")
    called: list[int] = []
    monkeypatch.setattr(m, "_assert_single_instance",
                        lambda port: called.append(port))
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: None)
    m.run_server()
    assert called == []
