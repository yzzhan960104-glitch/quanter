# -*- coding: utf-8 -*-
"""report 命令：扫 logs/trading_plans/plan_*.json 按 experiment_id 聚合。"""
import pytest
from unittest.mock import patch

from experiment import cli


def test_report_aggregates_by_experiment(capsys, monkeypatch):
    """同 experiment_id 的 order 聚到一组。"""
    plans = [{"date": "2026-07-10", "confirmed": True, "orders": [
        {"order": {"symbol": "A", "qty": 100, "side": "buy", "price": 10},
         "stop_price": 9, "take_profit": 11,
         "experiment_id": "e_prod", "experiment_weight": 0.8},
        {"order": {"symbol": "B", "qty": 100, "side": "buy", "price": 10},
         "stop_price": 9, "take_profit": 11,
         "experiment_id": "e_cand", "experiment_weight": 0.2}]}]
    monkeypatch.setattr(cli, "_DEFAULT_DB", ":memory:")
    with patch("experiment.cli._load_all_plans", return_value=plans):
        rc = cli.main(["report", "--since", "2026-07-01"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "e_prod" in out and "e_cand" in out


def test_report_handles_unattributed_orders(capsys, monkeypatch):
    """无 experiment_id 的老 order 归「未归因」桶，不崩。"""
    plans = [{"date": "2026-07-01", "confirmed": True, "orders": [
        {"order": {"symbol": "X", "qty": 100, "side": "buy", "price": 10},
         "stop_price": 9, "take_profit": 11}]}]
    monkeypatch.setattr(cli, "_DEFAULT_DB", ":memory:")
    with patch("experiment.cli._load_all_plans", return_value=plans):
        rc = cli.main(["report", "--since", "2026-07-01"])
    assert rc == 0
    assert "未归因" in capsys.readouterr().out
