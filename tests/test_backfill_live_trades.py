# -*- coding: utf-8 -*-
"""历史 CSV 成交可信回填 state_store 测试（一次性迁移，排除测试/冒烟行，幂等）。

A4 注：脚本已归档到 scripts/archive/（CSV 写盘链路整体退役，backfill 是一次性
可信回填工具，保留可重跑能力但移出主 scripts/）。import 路径随之改为
scripts.archive.backfill_live_trades_to_state_store。"""
import csv
import sqlite3

from trading import state_store


def _write_csv(p, rows):
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=[
            "timestamp", "symbol", "direction", "shares", "price",
            "strategy", "rationale", "kind",
        ])
        w.writeheader()
        for r in rows:
            w.writerow(r)


def _fill(ts, sym, direction, shares, price, rationale="成交回报", kind="fill"):
    return {
        "timestamp": ts, "symbol": sym, "direction": direction,
        "shares": shares, "price": price, "strategy": "neckline",
        "rationale": rationale, "kind": kind,
    }


def _count(db, table):
    con = sqlite3.connect(db)
    try:
        return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        con.close()


def test_backfill_dry_run_inserts_nothing(tmp_path, monkeypatch):
    """默认 dry-run：只统计候选，不写库。"""
    csv_p = tmp_path / "trades.csv"
    _write_csv(csv_p, [_fill("2026-07-27 12:55:00", "600519.SH", "BUY", 100, 1291.09)])
    db = str(tmp_path / "state.db")
    state_store.init_store(db)
    monkeypatch.setattr(state_store, "_DEFAULT_DB", db)
    from scripts.archive.backfill_live_trades_to_state_store import backfill
    res = backfill(str(csv_p))
    assert res["candidates"] == 1
    assert res["applied"] == 0
    assert _count(db, "fill") == 0


def test_backfill_applies_trusted_fills_excluding_test_rows(tmp_path, monkeypatch):
    """真实 fill 回填；测试/冒烟行（600000/300001/510300 冒烟/DRYRUN）与 submit 不计。"""
    csv_p = tmp_path / "trades.csv"
    _write_csv(csv_p, [
        _fill("2026-07-27 12:55:00", "600519.SH", "BUY", 100, 1291.09, rationale="补录:券商持仓对账"),
        _fill("2026-07-29 09:35:43", "300654.SZ", "BUY", 4300, 11.54, rationale="补录"),
        _fill("2026-07-29 09:36:39", "300654.SZ", "BUY", 4300, 11.54, rationale="补录"),
        _fill("2026-07-29 09:44:26", "300654.SZ", "BUY", 4300, 11.54, rationale="补录"),
        _fill("2026-08-01 10:10:00", "600000.SH", "BUY", 100, 10.5, rationale="成交回报@20260801101000"),
        _fill("2026-08-01 10:10:00", "300001.SZ", "BUY", 100, 10.0, rationale="成交回报@20260801101000"),
        _fill("2026-07-28 09:22:00", "510300.SH", "BUY", 100, 5.0, rationale="_FakeGW"),
        _fill("2026-07-28 09:22:00", "DRYRUN.SZ", "BUY", 100, 5.0, rationale="test"),
        _fill("2026-07-28 09:22:00", "300654.SZ", "BUY", 100, 11.54, rationale="已提交", kind="submit"),
    ])
    db = str(tmp_path / "state.db")
    state_store.init_store(db)
    monkeypatch.setattr(state_store, "_DEFAULT_DB", db)
    from scripts.archive.backfill_live_trades_to_state_store import backfill
    res = backfill(str(csv_p), apply=True)
    assert res["applied"] == 4
    assert _count(db, "fill") == 4
    con = sqlite3.connect(db)
    positions = dict(con.execute("SELECT symbol, qty FROM position").fetchall())
    con.close()
    assert positions == {"600519.SH": 100.0, "300654.SZ": 12900.0}
    # traded_time 必须为 YYYYMMDDHHMMSS 数字串（query_fills 按 substr(...,1,8) 过滤的契约）
    rows = state_store.query_fills("2026-07-27", "2026-07-29", db_path=db)
    assert len(rows) == 4
    assert all(r["traded_time"].isdigit() for r in rows)


def test_backfill_is_idempotent(tmp_path, monkeypatch):
    """重复执行：已存在 (order_id, traded_time) 的 fill 跳过，不重复累加持仓。"""
    csv_p = tmp_path / "trades.csv"
    _write_csv(csv_p, [_fill("2026-07-27 12:55:00", "600519.SH", "BUY", 100, 1291.09)])
    db = str(tmp_path / "state.db")
    state_store.init_store(db)
    monkeypatch.setattr(state_store, "_DEFAULT_DB", db)
    from scripts.archive.backfill_live_trades_to_state_store import backfill
    assert backfill(str(csv_p), apply=True)["applied"] == 1
    res2 = backfill(str(csv_p), apply=True)
    assert res2["applied"] == 0
    assert res2["skipped"] == 1
    assert _count(db, "fill") == 1
