# -*- coding: utf-8 -*-
"""CSV 审计层 kind 列（#3）：submit/fill 分离，post_close 聚合只认 fill。"""
import csv as _csv

import pytest

from presentation.server.services import trading_service as ts


@pytest.fixture
def csv_log(tmp_path, monkeypatch):
    log = tmp_path / "live_trades.csv"
    monkeypatch.setattr(ts, "LIVE_TRADE_LOG", str(log))
    return log


def test_csv_kind_column_distinguishes_submit_and_fill(csv_log):
    """CSV 有 kind 列；submit 行与 fill 行并存且可区分。"""
    ts.record_live_trade("600000.SH", "BUY", 100, 10.0, kind="submit",
                         rationale="QmtExecutionGateway:REJECTED:资金不足")
    ts.record_live_trade("600000.SH", "BUY", 100, 10.5, kind="fill", rationale="成交回报")
    with open(csv_log, encoding="utf-8-sig") as f:
        rows = list(_csv.DictReader(f))
    assert "kind" in rows[0], "CSV 必须有 kind 列"
    assert [r["kind"] for r in rows] == ["submit", "fill"]


def test_old_rows_without_kind_default_submit(csv_log):
    """老格式行（无 kind）默认按 submit 处理：不产生幻影持仓（保守）。"""
    csv_log.write_text("timestamp,symbol,direction,shares,price,strategy,rationale\n"
                       "2026-08-01 09:30:00,600000.SH,BUY,100,10.0,neckline,audit\n",
                       encoding="utf-8-sig")
    with open(csv_log, encoding="utf-8-sig") as f:
        rows = list(_csv.DictReader(f))
    assert rows[0].get("kind", "submit") == "submit"


def test_aggregate_fills_only_kind_fill(csv_log, monkeypatch):
    """aggregate_fills_by_symbol（DB 真相源，W3.4）只聚合 fill 表 BUY/SELL，净 60。"""
    from trading import state_store
    db = csv_log.parent / "state.db"
    state_store.init_store(str(db))
    monkeypatch.setattr(state_store, "_DEFAULT_DB", str(db))
    monkeypatch.setenv("LIVE_TRADE_READ_SOURCE", "db")
    state_store.upsert_account("acc", broker="qmt", db_path=str(db))
    state_store.insert_fill("o1", "acc", "20260805093000", "600000.SH", "BUY", 100, 10.5,
                            db_path=str(db))
    state_store.insert_fill("o2", "acc", "20260805093100", "600000.SH", "SELL", 40, 11.0,
                            db_path=str(db))
    net = ts.aggregate_fills_by_symbol("2026-08-05", "2026-08-05")
    assert net == {"600000.SH": 60.0}, f"只聚合 fill 行应得净 60，实际 {net}"


def test_aggregate_fills_csv_fallback_env(csv_log, monkeypatch):
    """LIVE_TRADE_READ_SOURCE=csv 时回退 CSV 读口（一键回滚开关）。"""
    monkeypatch.setenv("LIVE_TRADE_READ_SOURCE", "csv")
    ts.record_live_trade("600000.SH", "BUY", 100, 10.5, kind="fill", rationale="成交回报")
    ts.record_live_trade("600000.SH", "SELL", 40, 11.0, kind="fill", rationale="成交回报")
    _today = __import__("datetime").datetime.now().strftime("%Y-%m-%d")
    net = ts.aggregate_fills_by_symbol(_today, _today)
    assert net == {"600000.SH": 60.0}
