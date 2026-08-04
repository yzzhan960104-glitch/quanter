# -*- coding: utf-8 -*-
"""replay_tasks.db 中 legacy 报告（max_drawdown=累计 rr 口径）在播报前重算为净值口径。"""
import pytest

import backtest.tasks_db as tdb
import broadcast.__main__ as bm


def _fake_tasks_db(monkeypatch, report):
    monkeypatch.setattr(tdb, "init_db", lambda *a, **k: None)
    monkeypatch.setattr(tdb, "list_tasks", lambda **k: [
        {"status": "SUCCESS", "created_at": "2026-08-03T00:47:57", "report": report},
    ])


def test_recent_runs_recomputes_legacy_drawdown(monkeypatch):
    """-412.62（rr 口径）→ 从 equity_curve 重算净值回撤（min 0.84 ≈ -0.16）。"""
    report = {
        "n_hits": 1314, "win_rate": 0.31, "max_drawdown": -412.62,
        "annualized_return": -0.313, "avg_holding_bars": 1.0,
        "equity_curve": [
            {"date": "2026-08-01", "equity": 1.0},
            {"date": "2026-08-02", "equity": 0.84},
            {"date": "2026-08-03", "equity": 0.90},
        ],
    }
    _fake_tasks_db(monkeypatch, report)
    runs = bm._recent_runs_from_tasks_db()
    assert len(runs) == 1
    dd = runs[0]["max_drawdown"]
    assert dd is not None
    assert dd == pytest.approx(-0.16, abs=1e-9)


def test_recent_runs_keeps_new_format_drawdown(monkeypatch):
    """净值口径（[-1, 0]）保持原值，不重算。"""
    report = {
        "n_hits": 100, "win_rate": 0.26, "max_drawdown": -0.1419,
        "annualized_return": -0.77, "avg_holding_bars": 1.0,
        "equity_curve": [{"date": "2026-08-01", "equity": 1.0}],
    }
    _fake_tasks_db(monkeypatch, report)
    runs = bm._recent_runs_from_tasks_db()
    assert runs[0]["max_drawdown"] == -0.1419
