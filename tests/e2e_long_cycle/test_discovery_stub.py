# -*- coding: utf-8 -*-
"""V5：discovery cron 注册真 + daemon mock + 补跑两态。"""
from __future__ import annotations


def test_discovery_cron_registered_on_engine_sched(isolated_state, monkeypatch):
    """discovery_stub.attach → engine.sched 加 discovery_daemon cron 02:00 + _run_discovery_subprocess mock。"""
    from tests.e2e_long_cycle.discovery_stub import discovery_stub
    from trading.engine import TradingEngine
    from unittest.mock import MagicMock

    eng = TradingEngine()
    eng.sched.add_job = MagicMock()  # 捕获 add_job
    run_daemon_mock = MagicMock()
    with discovery_stub.attach(eng, run_daemon_mock=run_daemon_mock):
        # cron 注册（spec §3.2 V2 范式）
        add_args = eng.sched.add_job.call_args
        assert add_args is not None
        assert add_args.kwargs.get("id") == "discovery_daemon"
    # _run_discovery_subprocess 被 mock（不真跑 daemon）

