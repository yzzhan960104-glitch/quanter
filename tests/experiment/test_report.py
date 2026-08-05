# -*- coding: utf-8 -*-
"""report 命令：扫 logs/trading_plans/plan_*.json 按 experiment_id 聚合。

历史：
- C2d 曾切 DB（state_store.list_signals_with_meta_by_plan_date_range），但 experiment 是
  纯配置叶子层（layer 铁律 5：禁任何 trading import，含 lazy 函数内），DB 读违反 layer，
  test_experiment_pure_leaf fail —— 故回退 JSON。
- C3 删 save_plan 后 plan_*.json 不再写，report plan 聚合暂出空表（follow-up 重设计，
  详见 experiment/cli.py::_load_all_plans docstring），不阻塞本分支合并。
"""
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
