# -*- coding: utf-8 -*-
"""V6：TableSnapshotCollector 每日每表快照。"""
from __future__ import annotations

from datetime import date


def test_snapshot_reads_all_six_tables_plus_plan(isolated_state):
    """snapshot(T+1) → 读 trade_event/order/fill/position/account/account_daily + plan JSON 计数。"""
    from tests.e2e_long_cycle.table_snapshot import TableSnapshotCollector
    from trading import state_store, trading_plan

    # 预置一单（eod_plan → pre_open → fill）
    trading_plan.save_plan("2026-07-02", [
        {"order": {"symbol": "300001.SZ", "qty": 100, "side": "buy", "price": 10.5},
         "stop_price": 9.5, "take_profit": 11.5, "neckline": 10.5, "atr": 0.5,
         "formed_at": "2026-07-01", "max_wait": 5, "tp1": None, "tp1_portion": None,
         "cancel_on": None, "experiment_id": None, "experiment_weight": 1.0, "rr": 1.0}])
    state_store.upsert_account("e2e_long_acc", broker="qmt")
    state_store.insert_order("o1", "e2e_long_acc_300001.SZ_2026-07-02", "e2e_long_acc",
                             "2026-07-02", "300001.SZ", "buy", "OPEN", 100, 10.5, state="FILLED")

    snap = TableSnapshotCollector().snapshot(date(2026, 7, 2))
    assert snap["plan_orders"] == 1
    assert snap["order_count"] >= 1
    assert "trade_event" in snap and "fill" in snap and "position" in snap


def test_snapshot_collects_detail_rows(isolated_state):
    """快照应返回 fill/order/trade_event/position/account_daily 明细行（不只计数）。

    物理意图（design §4.4）：报表要能列出真实交易/持仓列表，快照必须先采集明细行；
    若只返回计数（旧实现），ReportBuilder 无从渲染列表。
    """
    from datetime import date
    from trading import state_store, engine
    from tests.e2e_long_cycle.table_snapshot import TableSnapshotCollector

    account = engine._resolve_account_id()
    state_store.upsert_account(account, broker="qmt")
    state_store.insert_order("o1", f"{account}_300001.SZ_2026-07-02", account,
                             "2026-07-02", "300001.SZ", "buy", "OPEN", 100, 10.0,
                             state="SUBMITTED", broker_oid="b1")
    state_store.insert_trade_event(account, f"{account}_300001.SZ_2026-07-02",
                                   "300001.SZ", "FILLED", order_id="b1", qty=100,
                                   price=10.0, timestamp="2026-07-02 09:25:00")
    state_store.insert_fill("b1", account, "2026-07-02 09:25:00", "300001.SZ", "BUY", 100, 10.0)
    state_store.apply_fill_to_position(account, "300001.SZ", "BUY", 100, 10.0,
                                       "2026-07-02 09:25:00")
    state_store.snapshot_start_equity(account, "2026-07-02", 1_000_000.0, 500_000.0)

    snap = TableSnapshotCollector().snapshot(date(2026, 7, 2))
    assert any(r["symbol"] == "300001.SZ" for r in snap["fills"]), "fills 明细应含 300001.SZ"
    assert any(r["symbol"] == "300001.SZ" for r in snap["orders"]), "orders 明细应含 300001.SZ"
    assert any(r["action"] == "FILLED" for r in snap["trade_events"]), "事件明细应含 FILLED"
    assert any(r["symbol"] == "300001.SZ" for r in snap["positions"]), "positions 明细应含持仓"
    assert snap["positions"][0]["holding_days"] == 0, "当日建仓 holding_days 应为 0"
    assert any(r["date"] == "2026-07-02" for r in snap["account_daily_rows"]), "账户明细应含当日行"
