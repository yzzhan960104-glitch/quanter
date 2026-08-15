# -*- coding: utf-8 -*-
"""T2 检查点就地落库 data_ready（C-2 D4）。

物理意图：T2 步骤算完结构化结果 {ok, melted, details} 后，run_check 额外把结构化状态
落 data_ready 表（同日重采覆盖），让 eod/pre_open gate 读 readiness 而非退化为进程退出码。
本测试覆盖两条关键路径：t2 PASS 落 ok=1；t2 超时熔断落 ok=0。
"""
import tempfile
from pathlib import Path
from unittest.mock import patch
from trading import state_store


def test_t2_writes_data_ready_on_pass(monkeypatch, tmp_path):
    db = str(tmp_path / "t.db")
    monkeypatch.setattr(state_store, "_DEFAULT_DB", db)
    state_store.init_store()
    # run_check("t2") 成功后应落 data_ready
    with patch("ops.run_data_check.check_freshness") as cf, \
         patch("ops.run_data_check.expected_latest_trade_day", return_value="2026-07-30"):
        from data.freshness import FreshnessResult
        cf.return_value = FreshnessResult(key="daily", ok=True, latest_date="2026-07-30",
                                          expected_date="2026-07-30", message="PASS")
        from ops.run_data_check import run_check
        run_check("t2", keys=("daily",), deadline_hour=23)  # deadline 远在未来避免 sleep
    got = state_store.get_data_ready("2026-07-30", "daily", db_path=db)
    assert got is not None
    assert got["ok"] == 1


def test_t2_writes_data_ready_on_melt(monkeypatch, tmp_path):
    db = str(tmp_path / "t.db")
    monkeypatch.setattr(state_store, "_DEFAULT_DB", db)
    state_store.init_store()
    with patch("ops.run_data_check.check_freshness") as cf, \
         patch("ops.run_data_check.expected_latest_trade_day", return_value="2026-07-30"), \
         patch("ops.run_data_check._now", return_value="23:30"), \
         patch("ops.run_data_check._resync_key", return_value=(False, "fail")):
        from data.freshness import FreshnessResult
        cf.return_value = FreshnessResult(key="daily", ok=False, latest_date=None,
                                          expected_date="2026-07-30", message="缺")
        from ops.run_data_check import run_check
        run_check("t2", keys=("daily",), deadline_hour=20)
    got = state_store.get_data_ready("2026-07-30", "daily", db_path=db)
    assert got is not None
    assert got["ok"] == 0
