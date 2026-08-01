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
