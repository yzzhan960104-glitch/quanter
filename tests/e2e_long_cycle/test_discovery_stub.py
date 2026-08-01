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


def test_discovery_catchup_two_states(isolated_state, monkeypatch):
    """_discovery_missed_last_run 两态：错过→补跑 / 未错过→跳过（spec §3.3）。

    plan 强约束（plans/2026-08-01-e2e-long-cycle.md:34）「验 _discovery_missed_last_run 两态」，
    V5 原 brief 笔误只写错过态（名实不符）。此用例补齐未错过态分支保护：
    覆盖 main.py:_discovery_missed_last_run 的 row is not None + last_dt >= yesterday_02 → False
    （offline 容错补跑判定的另一面），V7 全链路集成时若 yesterday_02 比较逻辑有 bug
    （时区/ISO 格式），本用例可即时捕获。
    """
    from tests.e2e_long_cycle.discovery_stub import discovery_stub
    import sqlite3
    from datetime import datetime, timedelta

    tmp_path = isolated_state  # conftest.isolated_state 透传 tmp_path（隔离 DB）

    # 错过态：空 DB（DB 文件不存在 → sqlite3.connect 建空库但 search_run 表缺失
    # → OperationalError → except 分支返 True 补跑，语义对齐「无记录即补跑」）。
    monkeypatch.setenv("DISCOVERY_DB", str(tmp_path / "no_such_e2e.db"))
    assert discovery_stub.missed_last_run() is True

    # 未错过态：DB 有近期 started_at（≥ 昨日 02:00）→ False（不补跑）。
    # 物理意图：昨晚 02:00 的 cron 已正常跑过 discovery（started_at=今天凌晨），
    # lifespan startup 不补跑（避免与今晚 02:00 双跑——补跑子进程有 ~4h budget 开销）。
    # 参考 tests/presentation/test_lifespan_consolidation.py:_seed_search_run 范式
    # （C-7 V3 已有完整两态测试，此处照搬语义，仅 started_at 列即可——
    # _discovery_missed_last_run 只读这一列，不依赖 snapshot_hash）。
    db_recent = str(tmp_path / "discovery_recent.db")
    conn = sqlite3.connect(db_recent)
    conn.execute("CREATE TABLE search_run (started_at TEXT)")  # 幂等建表
    recent = (datetime.now() - timedelta(hours=2)).isoformat()  # 今天凌晨，≥ 昨日 02:00
    conn.execute("INSERT INTO search_run (started_at) VALUES (?)", (recent,))
    conn.commit()
    conn.close()
    monkeypatch.setenv("DISCOVERY_DB", db_recent)
    assert discovery_stub.missed_last_run() is False
