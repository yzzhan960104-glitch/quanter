# -*- coding: utf-8 -*-
"""report 命令：C2d 切 DB（trade_event SIGNAL.meta）按 experiment_id 聚合。

历史：原扫 logs/trading_plans/plan_*.json；C2d 改读 state_store DB（致命日期轴：
plan_*.json 文件 mtime = T 日盘后写入 ≠ plan_date T+1，since 按 mtime 恒错）。
聚合逻辑不变（report 只换数据源），故老 mock 测试仍绿 + 新 DB 测试覆盖 since 锚 plan_date。
"""
import json

import pytest
from unittest.mock import patch

from experiment import cli


def test_report_aggregates_by_experiment(capsys, monkeypatch):
    """同 experiment_id 的 order 聚到一组（mock _load_all_plans · 聚合逻辑不变）。"""
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
    """无 experiment_id 的老 order 归「未归因」桶，不崩（mock · 聚合不变）。"""
    plans = [{"date": "2026-07-01", "confirmed": True, "orders": [
        {"order": {"symbol": "X", "qty": 100, "side": "buy", "price": 10},
         "stop_price": 9, "take_profit": 11}]}]
    monkeypatch.setattr(cli, "_DEFAULT_DB", ":memory:")
    with patch("experiment.cli._load_all_plans", return_value=plans):
        rc = cli.main(["report", "--since", "2026-07-01"])
    assert rc == 0
    assert "未归因" in capsys.readouterr().out


# ============ C2d：DB-source 真实路径（since 锚 plan_date 致命日期轴）============


def _seed_signal(tmp_db, account_id, symbol, plan_date, *, experiment_id=None,
                 experiment_weight=None, order=None):
    """落一行 trade_event(SIGNAL) 到 tmp_db，meta 含 plan_date/experiment_id/order。"""
    from trading import state_store
    tid = state_store.build_trade_id(account_id, symbol, plan_date)
    meta = {
        "plan_date": plan_date,
        "order": order or {"symbol": symbol, "qty": 100, "side": "buy", "price": 10},
        "stop_price": 9, "take_profit": 11,
    }
    if experiment_id is not None:
        meta["experiment_id"] = experiment_id
    if experiment_weight is not None:
        meta["experiment_weight"] = experiment_weight
    state_store.insert_trade_event(account_id, tid, symbol, "SIGNAL", meta=json.dumps(meta))


def test_report_reads_db_and_aggregates_by_experiment(capsys, tmp_db, monkeypatch):
    """C2d DB 真实路径：_load_all_plans 读 trade_event(SIGNAL).meta 按 experiment_id 聚合。

    致命日期轴：since 锚 plan_date（trade_id 后缀 substr(-10)），非文件 mtime。
    插两日三单跨实验，since=2026-07-02 应只聚合 7-2 那一单。
    """
    monkeypatch.setattr(cli, "_DEFAULT_DB", ":memory:")
    monkeypatch.setenv("QMT_ACCOUNT_ID", "ACC_TEST")
    _seed_signal(tmp_db, "ACC_TEST", "600000.SH", "2026-07-01",
                 experiment_id="e_prod", experiment_weight=0.8)
    _seed_signal(tmp_db, "ACC_TEST", "600001.SH", "2026-07-02",
                 experiment_id="e_cand", experiment_weight=0.2)
    _seed_signal(tmp_db, "ACC_TEST", "600002.SH", "2026-07-02",
                 experiment_id="e_cand", experiment_weight=0.2)

    rc = cli.main(["report", "--since", "2026-07-02"])
    assert rc == 0
    out = capsys.readouterr().out
    # since=07-02 过滤掉 07-01 的 e_prod 单 → e_prod 不在结果，e_cand 2 单聚合
    assert "e_cand" in out
    assert "e_prod" not in out
    # 表头 + 订单数 2 + 标的数 2
    assert "订单数" in out


def test_load_all_plans_since_filters_by_plan_date_not_timestamp(tmp_db, monkeypatch):
    """致命日期轴：since 按 plan_date（trade_id 后缀），非 trade_event.timestamp。

    防回归：若 C2d 误用 timestamp 做 since 过滤，T 日盘后写入时间 < plan_date T+1，
    since=T+1 会把所有 SIGNAL 滤掉（timestamp < since 恒真）。这里显式插一单
    plan_date=2026-07-05 + timestamp 由 init_store 写入（test 当下时间），since=2026-07-05
    按 plan_date 应命中 1 单；按 timestamp 会因 now < 2026-07-05 漏。
    """
    from trading import state_store
    _seed_signal(tmp_db, "ACC_TEST", "600000.SH", "2026-07-05",
                 experiment_id="e1", experiment_weight=1.0)
    plans = cli._load_all_plans(since="2026-07-05")
    assert len(plans) == 1
    assert plans[0]["date"] == "2026-07-05"
    assert plans[0]["orders"][0].get("experiment_id") == "e1"
