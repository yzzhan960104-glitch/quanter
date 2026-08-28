# -*- coding: utf-8 -*-
"""双腿注册表与多腿消费方（2026-08-28 双轨方案 §4）。

测试焦点：LEGS 发现语义（env 覆写/未启用跳过/main 恒在）、ingest 双腿同秒同事件
不互吞（唯一键含 account_id 的真雷回归）、旧键迁移、ab_compare 纯函数（信号
diff/订单 diff/工程闸）。外部 I/O（网络/进程）零依赖——全部临时目录与内存数据。
"""
from __future__ import annotations

import json
import sqlite3

from ops import emquant_ab_compare as ab
from ops import emquant_audit_ingest as ingest
from ops import gm_ops_common as gc


# ============================================================ §4.2 注册表
def test_active_legs_main_only_by_default(monkeypatch):
    monkeypatch.delenv("GM_EXP_STRATEGY_DIR", raising=False)
    legs = gc.active_legs()
    assert [l.key for l in legs] == ["main"]


def test_active_legs_exp_enabled_when_dir_exists(tmp_path, monkeypatch):
    monkeypatch.setenv("GM_EXP_STRATEGY_DIR", str(tmp_path))
    legs = gc.active_legs()
    assert [l.key for l in legs] == ["main", "exp"]
    assert gc.leg_strategy_dir(legs[1]) == tmp_path


def test_active_legs_exp_env_set_but_dir_missing_skipped(tmp_path, monkeypatch):
    monkeypatch.setenv("GM_EXP_STRATEGY_DIR", str(tmp_path / "nonexistent"))
    assert [l.key for l in gc.active_legs()] == ["main"]


def test_leg_strategy_dir_env_overrides_main(monkeypatch):
    monkeypatch.setenv("GM_STRATEGY_DIR", r"C:\somewhere")
    assert str(gc.leg_strategy_dir(gc.LEGS[0])) == r"C:\somewhere"


# ============================================================ §4.3 ingest 双腿
def _write_audit(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    import csv
    with path.open("w", encoding="utf-8", newline="") as fh:
        csv.writer(fh).writerows(rows)


def test_ingest_two_legs_same_ts_event_detail_both_survive(tmp_path):
    """双腿真雷回归：两腿各自 10:31:00 的 SCHEDULE_TICK（同 ts+detail）不互吞。"""
    db = tmp_path / "t.db"
    day = "2026-08-28"
    row = [f"{day}T10:31:00", "SCHEDULE_TICK", json.dumps({"probe": True})]
    _write_audit(tmp_path / "main" / "audit" / "audit_20260828.csv",
                 [[f"{day}T09:15:00", "INIT",
                   json.dumps({"account": "acc-main", "strategy_id": "s1"})], row])
    _write_audit(tmp_path / "exp" / "audit" / "audit_20260828.csv",
                 [[f"{day}T09:15:00", "INIT",
                   json.dumps({"account": "acc-exp", "strategy_id": "s2"})], row])
    ingest.ingest_day(day, db_path=db, strategy_dir=tmp_path / "main")
    ingest.ingest_day(day, db_path=db, strategy_dir=tmp_path / "exp")
    with sqlite3.connect(db) as con:
        rows = con.execute(
            "SELECT account_id FROM terminal_audit WHERE event='SCHEDULE_TICK'"
            " ORDER BY account_id").fetchall()
    assert rows == [("acc-exp",), ("acc-main",)]      # 两腿同秒同事件都在——旧键下会只剩一条


def test_ingest_old_unique_key_migrated(tmp_path):
    """存量库旧键 UNIQUE(ts,event,detail) → 迁移到含 account_id 的新键（搬数零丢失）。"""
    db = tmp_path / "old.db"
    day = "2026-08-27"
    with sqlite3.connect(db) as con:
        con.execute("""CREATE TABLE terminal_audit (
            audit_id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
            event TEXT NOT NULL, detail TEXT, account_id TEXT, strategy_id TEXT,
            ingested_at TEXT NOT NULL, UNIQUE(ts, event, detail))""")
        con.execute("INSERT INTO terminal_audit(ts,event,detail,account_id,strategy_id,ingested_at)"
                    " VALUES (?,?,?,?,?,?)",
                    (f"{day}T09:15:00", "INIT", "{}", "acc-main", "s1", "t0"))
    _write_audit(tmp_path / "audit" / "audit_20260827.csv",
                 [[f"{day}T09:15:01", "RECONCILE", "{}"]])
    ingest.ingest_day(day, db_path=db, strategy_dir=tmp_path)
    with sqlite3.connect(db) as con:
        sql = con.execute("SELECT sql FROM sqlite_master"
                          " WHERE name='terminal_audit'").fetchone()[0]
        assert "UNIQUE(ts, event, detail, account_id)" in sql
        n = con.execute("SELECT COUNT(*) FROM terminal_audit").fetchone()[0]
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    assert n == 2                                       # 旧行搬入 + 新行插入
    assert "terminal_audit_old" not in tables           # 迁移残留表已清


# ============================================================ §4.4 ab_compare 纯函数
def _rows(*specs):
    return [(ts, ev, json.dumps(d)) for ts, ev, d in specs]


def test_diff_float_maps_zero_and_drift():
    a = {"300433.SZ": 38.9, "688825.SH": 210.0}
    assert ab.diff_float_maps(a, dict(a)) == {"added": [], "removed": [], "drifted": []}
    d = ab.diff_float_maps(a, {"300433.SZ": 38.9, "301018.SZ": 15.0})
    assert d["added"] == ["301018.SZ"] and d["removed"] == ["688825.SH"]
    d = ab.diff_float_maps(a, {"300433.SZ": 40.0, "688825.SH": 210.0})
    assert d["drifted"][0]["symbol"] == "300433.SZ"


def test_signals_and_orders_map():
    rows = _rows(
        ("2026-08-28T09:31:08", "SIGNAL", {"symbol": "300433.SZ", "entry_price": 38.9}),
        ("2026-08-28T09:31:09", "SIGNAL", {"symbol": "300456.SZ", "entry_price": 11.2}),
        ("2026-08-28T09:32:00", "ORDER_PLACED", {"symbol": "300433.SZ", "price": 38.9, "qty": 100}),
        ("2026-08-28T10:31:00", "SCHEDULE_TICK", {"probe": True}),
    )
    assert ab.signals_map(rows) == {"300433.SZ": 38.9, "300456.SZ": 11.2}
    assert ab.orders_map(rows) == {"300433.SZ": {"price": 38.9, "qty": 100}}


def test_diff_orders_qty_diff_is_expected_category():
    a = {"300433.SZ": {"price": 38.9, "qty": 60000}}
    b = {"300433.SZ": {"price": 38.9, "qty": 100}}
    d = ab.diff_orders(a, b)
    assert d["added"] == [] and d["drifted"] == []
    assert d["qty_diff"] == [{"symbol": "300433.SZ", "incumbent": 60000, "challenger": 100}]


def test_eng_gate_counts_and_init():
    rows = _rows(
        ("2026-08-28T09:15:00", "INIT", {"account": "acc-x", "build_stamp": "s1"}),
        ("2026-08-28T10:31:00", "SCHEDULE_TICK", {"probe": True}),
        ("2026-08-28T11:00:00", "WARN", {"type": "order_rejected"}),
    )
    g = ab.eng_gate(rows)
    assert g["init"]["account"] == "acc-x"
    assert g["counts"]["SCHEDULE_TICK"] == 1 and g["counts"]["WARN"] == 1
    assert g["warn_types"]["order_rejected"] == 1


def test_build_report_zero_diff_calibrates_green():
    rows = _rows(
        ("2026-08-28T09:15:00", "INIT", {"account": "acc-x", "build_stamp": "s"}),
        ("2026-08-28T09:31:08", "SIGNAL", {"symbol": "300433.SZ", "entry_price": 38.9}),
        ("2026-08-28T09:32:00", "ORDER_PLACED", {"symbol": "300433.SZ", "price": 38.9, "qty": 60000}),
    )
    rows_b = _rows(
        ("2026-08-28T09:15:00", "INIT", {"account": "acc-x", "build_stamp": "s [exp]"}),
        ("2026-08-28T09:31:08", "SIGNAL", {"symbol": "300433.SZ", "entry_price": 38.9}),
        ("2026-08-28T09:32:00", "ORDER_PLACED", {"symbol": "300433.SZ", "price": 38.9, "qty": 100}),
    )
    report, flags = ab.build_report("2026-08-28", rows, rows_b, "acc-x", "acc-x")
    assert flags == []                                  # 信号一致+订单结构一致+qty 差=预期
    assert "完全一致" in report and "qty 差" in report


def test_build_report_signal_drift_flags():
    rows_a = _rows(("2026-08-28T09:31:08", "SIGNAL", {"symbol": "300433.SZ", "entry_price": 38.9}))
    rows_b = _rows(("2026-08-28T09:31:08", "SIGNAL", {"symbol": "300456.SZ", "entry_price": 11.0}))
    _report, flags = ab.build_report("2026-08-28", rows_a, rows_b, "a", "a")
    assert any("信号 diff" in f for f in flags)


def test_build_report_init_wrong_account_flags():
    rows_a = _rows(("2026-08-28T09:15:00", "INIT", {"account": "acc-a", "build_stamp": "s"}))
    rows_b = _rows(("2026-08-28T09:15:00", "INIT", {"account": "acc-WRONG", "build_stamp": "s [exp]"}))
    _report, flags = ab.build_report("2026-08-28", rows_a, rows_b, "acc-a", "acc-b")
    assert any("acc-WRONG" in f for f in flags)


def test_ingest_pre_init_row_attributed_via_runtime(tmp_path):
    """bootstrap 序=RECONCILE 先于 INIT：前置行靠文件级兜底（runtime.json）归腿。"""
    db = tmp_path / "t.db"
    day = "2026-08-28"
    leg = tmp_path / "exp"
    (leg / "config").mkdir(parents=True)
    (leg / "config" / "runtime.json").write_text(json.dumps(
        {"token": "t", "strategy_id": "s2", "account_id": "acc-exp"}), encoding="utf-8")
    _write_audit(leg / "audit" / "audit_20260828.csv",
                 [[f"{day}T20:41:32", "RECONCILE", json.dumps({"orders": 0, "positions": 0})],
                  [f"{day}T20:41:32", "INIT",
                   json.dumps({"account": "acc-exp", "strategy_id": "s2"})]])
    ingest.ingest_day(day, db_path=db, strategy_dir=leg)
    with sqlite3.connect(db) as con:
        rows = con.execute("SELECT event, account_id FROM terminal_audit"
                           " ORDER BY audit_id").fetchall()
    assert rows == [("RECONCILE", "acc-exp"), ("INIT", "acc-exp")]   # 前置行不落 NULL


def test_guard_dual_leg_heals_each_leg(monkeypatch):
    """双腿形态：两腿进程齐缺且终端健康 → 各自 relaunch 一次（.env 部署键泄漏
    场景的正面覆盖——本测试显式钉双腿，与 p0 单腿用例互为对照）。"""
    from ops import gm_terminal_guard as guard
    monkeypatch.setattr(guard, "_in_market_window", lambda: True)
    monkeypatch.setattr(guard, "probe_port_7001", lambda: True)
    monkeypatch.setattr(guard, "probe_api", lambda cfg: (True, 200))
    monkeypatch.setattr(guard, "auto_heal_strategy", lambda d: True)
    monkeypatch.setattr(guard, "probe_strategy_process", lambda d: 0)
    monkeypatch.setattr(guard.gc, "runtime_config",
                        lambda d=None: {"token": "t", "account_id": "a"})
    monkeypatch.setattr(guard.gc, "active_legs", lambda: list(gc.LEGS))
    monkeypatch.setattr(guard, "_notify", lambda *a, **k: None)
    st = guard.run_once(auto_heal=True)
    assert st["auto_healed"] and st["ok"]
    assert st["legs"]["main"]["auto_healed"] and st["legs"]["exp"]["auto_healed"]
