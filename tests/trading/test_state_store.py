# -*- coding: utf-8 -*-
"""state_store 单测：统一交易状态库（6 张表 + 幂等写入 + 查询）。

物理意图（trading-state-store-redesign spec §2）：把散落在 gw._orders 内存 /
_tp_placed 内存 / position_book / live_trades.csv / trading_plan JSON 的交易状态，
收口到一个事务一致、跨重启、幂等保护的 SQLite 交易状态库。本测试覆盖：
- T1: 6 张表建表 + schema 迁移（account/trade_event/order/fill/position/account_daily）+ FK 引用完整性
- T2: account 表 CRUD + .env 迁移
- T3: trade_event / order / fill 幂等写入（UNIQUE 冲突返 False）
- T4: position（加权 + 归零）+ account_daily（快照 + daily_pnl）
- T5: 查询接口（has_order / get_active_trades / get_pending_orders / get_trade_plan / get_entry_dates / get_latest_action）

约定（对齐 test_position_book.py 风格）：file-based sqlite + per-file _DEFAULT_DB fixture，
async 测试用 asyncio.run（本模块全 sync，无 async）。全中文注释。
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from trading import state_store


@pytest.fixture
def db(tmp_path, monkeypatch):
    """每个测试用独立 tmp db（隔离），patch _DEFAULT_DB 让 engine 间接调用也命中 tmp。

    先 init_store 建 6 张表（state_store 是真相源，position_book 的表由 state_store 统一建）。
    """
    db_path = str(tmp_path / "state.db")
    monkeypatch.setattr(state_store, "_DEFAULT_DB", db_path)
    state_store.init_store()
    return db_path


def _table_cols(con, table: str) -> set[str]:
    """读表列名集合（PRAGMA table_info）。"""
    return {r[1] for r in con.execute(f"PRAGMA table_info({table})").fetchall()}


def _tables(con) -> set[str]:
    """读所有用户表名。"""
    return {
        r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    }


# ============================= T1：6 张表建表 + FK 引用完整性 =============================

def test_init_store_creates_6_tables(db):
    """init_store 后 6 张表存在（account/trade_event/order/fill/position/account_daily）。"""
    with sqlite3.connect(db) as con:
        tables = _tables(con)
    expected = {"account", "trade_event", "order", "fill", "position", "account_daily"}
    assert expected <= tables, f"缺失表: {expected - tables}"


def test_fill_table_has_account_id(db):
    """fill 表有 account_id 列（spec §7.1 迁移：ALTER ADD COLUMN 兼容既有 fill 数据）。"""
    with sqlite3.connect(db) as con:
        cols = _table_cols(con, "fill")
    assert "account_id" in cols


def test_position_pk_is_composite(db):
    """position PK = (account_id, symbol)（spec §2.2 ⑤：多账户隔离的复合主键）。"""
    with sqlite3.connect(db) as con:
        pk_cols = {
            r[1] for r in con.execute("PRAGMA table_info(position)").fetchall() if r[5] != 0
        }
    # PRAGMA pk 列名（组合键两列均非 0，标记主键序号）
    assert "account_id" in pk_cols
    assert "symbol" in pk_cols


def test_order_idempotent_unique(db):
    """order 表 UNIQUE(account_id, trade_date, symbol, purpose)——重复挂单幂等键存在。"""
    with sqlite3.connect(db) as con:
        # 先插一个 account（FK 引用完整性需要）
        con.execute("INSERT INTO account(account_id, broker, created_at) VALUES('ACC1','qmt','now')")
        con.commit()
        # 同 (account_id, trade_date, symbol, purpose) 第二次插应触发 UNIQUE 冲突
        base = "INSERT INTO \"order\"(order_id, trade_id, account_id, trade_date, symbol, side, purpose, qty, price) VALUES(?, ?, 'ACC1', '2026-07-30', '600000.SH', 'buy', 'OPEN', 100, 10.0)"
        con.execute(base, ("o1", "ACC1_600000.SH_2026-07-30"))
        con.commit()
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(base, ("o2", "ACC1_600000.SH_2026-07-30"))  # 同幂等键不同 order_id → 冲突
            con.commit()


def test_foreign_keys_enforced(db):
    """trade_event 引用不存在的 account_id → IntegrityError（PRAGMA foreign_keys=ON 生效）。"""
    with sqlite3.connect(db) as con:
        con.execute("PRAGMA foreign_keys=ON")  # sqlite3 默认 per-connection 关 FK，显式开
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(
                "INSERT INTO trade_event(account_id, trade_id, symbol, action, timestamp)"
                " VALUES('GHOST', 'GHOST_X_2026', 'X', 'SIGNAL', 'now')"
            )
            con.commit()


# ============================= T2：account 表 CRUD + .env 迁移 =============================

def test_upsert_account_idempotent(db):
    """upsert_account 同 account_id 覆盖不报错（INSERT OR REPLACE 幂等）。"""
    state_store.upsert_account("ACC1", broker="qmt", name="东北模拟盘")
    state_store.upsert_account("ACC1", broker="qmt", name="东北模拟盘改")  # 覆盖
    acc = state_store.get_account("ACC1")
    assert acc["account_id"] == "ACC1"
    assert acc["broker"] == "qmt"
    assert acc["name"] == "东北模拟盘改"


def test_get_account_returns_none_if_missing(db):
    """读不存在的 account_id → None。"""
    assert state_store.get_account("NOPE") is None


def test_migrate_env_to_account(monkeypatch, db):
    """mock env QMT_* → _migrate_env_to_account 写入 account 表，读取一致。"""
    monkeypatch.setenv("QMT_ACCOUNT_ID", "12345")
    monkeypatch.setenv("QMT_USERDATA_PATH", "C:/userdata")
    monkeypatch.setenv("QMT_SESSION_ID", "999")
    monkeypatch.setenv("AUTO_TRADE_MODE", "live")
    state_store._migrate_env_to_account()
    acc = state_store.get_account("12345")
    assert acc is not None
    assert acc["userdata_path"] == "C:/userdata"
    assert acc["session_id"] == 999
    assert acc["mode"] == "live"


# ============================= T3：trade_event / order / fill 幂等写入 =============================

def test_insert_trade_event_idempotent(db):
    """同 (account_id, trade_id, action) 重插 → 返 False（UNIQUE 冲突幂等）。"""
    state_store.upsert_account("ACC1", broker="qmt")
    ok1 = state_store.insert_trade_event("ACC1", "ACC1_X_2026", "600000.SH", "SIGNAL")
    ok2 = state_store.insert_trade_event("ACC1", "ACC1_X_2026", "600000.SH", "SIGNAL")
    assert ok1 is True
    assert ok2 is False  # 幂等跳过


def test_insert_order_idempotent(db):
    """同 (account_id, trade_date, symbol, purpose) 重插 → 返 False（重复挂单幂等）。"""
    state_store.upsert_account("ACC1", broker="qmt")
    ok1 = state_store.insert_order(
        "o1", "ACC1_X_2026", "ACC1", "2026-07-30", "600000.SH", "buy", "OPEN", 100, 10.0)
    ok2 = state_store.insert_order(
        "o2", "ACC1_X_2026", "ACC1", "2026-07-30", "600000.SH", "OPEN", 100, 10.0) if False else \
        state_store.insert_order(
            "o3", "ACC1_X_2026", "ACC1", "2026-07-30", "600000.SH", "buy", "OPEN", 100, 10.0)
    assert ok1 is True
    assert ok2 is False


def test_insert_fill_idempotent(db):
    """同 (order_id, traded_time) 重插 → 返 False（成交回报重推幂等）。"""
    state_store.upsert_account("ACC1", broker="qmt")
    state_store.insert_order("o1", "ACC1_X_2026", "ACC1", "2026-07-30", "600000.SH", "buy", "OPEN", 100, 10.0)
    ok1 = state_store.insert_fill("o1", "ACC1", "09:30:00", "600000.SH", "BUY", 100, 10.0)
    ok2 = state_store.insert_fill("o1", "ACC1", "09:30:00", "600000.SH", "BUY", 100, 10.0)
    assert ok1 is True
    assert ok2 is False


def test_insert_trade_event_signal_with_meta(db):
    """SIGNAL action 带 meta JSON（计划参数快照，后续 get_trade_plan 读）。"""
    state_store.upsert_account("ACC1", broker="qmt")
    meta = {"stop_price": 9.5, "tp1": 11.0, "take_profit": 12.0}
    state_store.insert_trade_event(
        "ACC1", "ACC1_X_2026", "600000.SH", "SIGNAL", meta=json.dumps(meta))
    with sqlite3.connect(db) as con:
        row = con.execute(
            "SELECT meta FROM trade_event WHERE action='SIGNAL' AND trade_id='ACC1_X_2026'"
        ).fetchone()
    assert json.loads(row[0]) == meta


# ============================= T4：position / account_daily 读写 =============================

def test_apply_fill_to_position_buy_weighted(db):
    """BUY 100@10 + 100@12 → avg=11.0；SELL avg 不变（A 股口径）。"""
    state_store.upsert_account("ACC1", broker="qmt")
    state_store.apply_fill_to_position("ACC1", "600000.SH", "BUY", 100, 10.0, "09:30:00")
    state_store.apply_fill_to_position("ACC1", "600000.SH", "BUY", 100, 12.0, "10:00:00")
    pos = state_store.get_position("ACC1", "600000.SH")
    assert pos["qty"] == pytest.approx(200.0)
    assert pos["avg_price"] == pytest.approx(11.0, abs=0.01)
    # SELL 不动 avg
    state_store.apply_fill_to_position("ACC1", "600000.SH", "SELL", 100, 11.5, "11:00:00")
    pos = state_store.get_position("ACC1", "600000.SH")
    assert pos["avg_price"] == pytest.approx(11.0, abs=0.01)
    assert pos["qty"] == pytest.approx(100.0)


def test_apply_fill_to_position_zero_clears(db):
    """归零 → position 行删除（对账并集不被 0 干扰）。"""
    state_store.upsert_account("ACC1", broker="qmt")
    state_store.apply_fill_to_position("ACC1", "600000.SH", "BUY", 100, 10.0, "09:30:00")
    state_store.apply_fill_to_position("ACC1", "600000.SH", "SELL", 100, 11.0, "15:00:00")
    assert state_store.get_position("ACC1", "600000.SH") is None


def test_snapshot_start_equity_idempotent(db):
    """INSERT OR REPLACE 同日覆盖（pre_open 崩溃重入安全）。"""
    state_store.upsert_account("ACC1", broker="qmt")
    state_store.snapshot_start_equity("ACC1", "2026-07-30", 1_000_000.0, 500_000.0)
    state_store.snapshot_start_equity("ACC1", "2026-07-30", 999_000.0, 499_000.0)
    with sqlite3.connect(db) as con:
        row = con.execute(
            "SELECT start_total_asset FROM account_daily WHERE account_id='ACC1' AND date='2026-07-30'"
        ).fetchone()
    assert row[0] == 999_000.0


def test_snapshot_close_equity_pnl(db):
    """snapshot_close_equity：close_total - start_total = daily_pnl（写收盘快照 + 盈亏）。"""
    state_store.upsert_account("ACC1", broker="qmt")
    state_store.snapshot_start_equity("ACC1", "2026-07-30", 1_000_000.0, 500_000.0)
    state_store.snapshot_close_equity(
        "ACC1", "2026-07-30", close_total_asset=1_020_000.0, close_cash=510_000.0,
        close_market_value=510_000.0)
    with sqlite3.connect(db) as con:
        row = con.execute(
            "SELECT daily_pnl, daily_pnl_pct FROM account_daily WHERE account_id='ACC1' AND date='2026-07-30'"
        ).fetchone()
    assert row[0] == pytest.approx(20_000.0)  # 1020000 - 1000000
    assert row[1] == pytest.approx(0.02, abs=1e-6)  # 2%


# ============================= T5：查询接口 =============================

def _seed_for_queries(db):
    """T5 查询测试共用种子数据：建账户 + 一个 OPEN 委托已挂 + SIGNAL 事件。"""
    state_store.upsert_account("ACC1", broker="qmt")
    meta = {"stop_price": 9.5, "tp1": 11.0, "take_profit": 12.0, "atr": 0.4}
    state_store.insert_trade_event(
        "ACC1", "ACC1_600000.SH_2026-07-30", "600000.SH", "SIGNAL", meta=json.dumps(meta))
    state_store.insert_trade_event(
        "ACC1", "ACC1_600000.SH_2026-07-30", "600000.SH", "CONFIRMED")
    state_store.insert_order(
        "o1", "ACC1_600000.SH_2026-07-30", "ACC1", "2026-07-30", "600000.SH", "buy", "OPEN",
        100, 10.0, state="SUBMITTED")


def test_has_order_true_false(db):
    """已挂 OPEN → True；未挂 STOP → False。"""
    _seed_for_queries(db)
    assert state_store.has_order("ACC1", "2026-07-30", "600000.SH", "OPEN") is True
    assert state_store.has_order("ACC1", "2026-07-30", "600000.SH", "STOP") is False


def test_has_order_filters_dead_states(db):
    """C-1 final-review (I-2/?-1)：REJECTED/FAILED/CANCELLED 死态不算已挂，允许重挂。

    防止挂单被拒（资金不足/涨跌停挡板）后 has_order 恒 True → 永久漏挂（pre_open OPEN）
    / 裸奔（stop_loss STOP 被拒不再发卖）/ 永不补挂（TP 被拒）。live 真金致命。
    """
    _seed_for_queries(db)  # o1 OPEN SUBMITTED → has_order True
    assert state_store.has_order("ACC1", "2026-07-30", "600000.SH", "OPEN") is True
    # 三种死态 → has_order False（可重挂）
    for _dead in ("REJECTED", "FAILED", "CANCELLED"):
        state_store.update_order_state("o1", _dead)
        assert state_store.has_order("ACC1", "2026-07-30", "600000.SH", "OPEN") is False, _dead
    # 活态恢复（SUBMITTED）→ True
    state_store.update_order_state("o1", "SUBMITTED")
    assert state_store.has_order("ACC1", "2026-07-30", "600000.SH", "OPEN") is True


def test_get_active_trades(db):
    """最新 action 非终态（CLOSED/EXPIRED/VETOED）的 trade 列表。"""
    _seed_for_queries(db)
    # 另一个已 CLOSED 的 trade 不应出现
    state_store.insert_trade_event(
        "ACC1", "ACC1_688001.SH_2026-07-30", "688001.SH", "SIGNAL")
    state_store.insert_trade_event(
        "ACC1", "ACC1_688001.SH_2026-07-30", "688001.SH", "CLOSED")
    active = state_store.get_active_trades("ACC1")
    trade_ids = {t["trade_id"] for t in active}
    assert "ACC1_600000.SH_2026-07-30" in trade_ids
    assert "ACC1_688001.SH_2026-07-30" not in trade_ids


def test_get_pending_orders(db):
    """state IN (PENDING/SUBMITTED/PARTIAL) 的 order（撤单用）。"""
    _seed_for_queries(db)
    # 加一个 FILLED 的（终态，不应返回）
    state_store.insert_order(
        "o2", "ACC1_600001.SH_2026-07-30", "ACC1", "2026-07-30", "600001.SH", "buy", "OPEN",
        100, 10.0, state="FILLED")
    pending = state_store.get_pending_orders("ACC1")
    order_ids = {o["order_id"] for o in pending}
    assert "o1" in order_ids  # SUBMITTED
    assert "o2" not in order_ids  # FILLED 终态


def test_get_trade_plan_from_signal(db):
    """读 trade_event SIGNAL 行的 meta JSON（plan 参数，stop_loss/pre_open 用）。"""
    _seed_for_queries(db)
    plan = state_store.get_trade_plan("ACC1_600000.SH_2026-07-30")
    assert plan is not None
    assert plan["stop_price"] == 9.5
    assert plan["tp1"] == 11.0


def test_get_entry_dates(db):
    """position entry_date 字典（max_holding/trailing 用）。"""
    state_store.upsert_account("ACC1", broker="qmt")
    state_store.apply_fill_to_position("ACC1", "600000.SH", "BUY", 100, 10.0, "09:30:00")
    entries = state_store.get_entry_dates("ACC1")
    assert "600000.SH" in entries


def test_get_latest_action(db):
    """某 trade_id 的最新 action（当前状态）。"""
    _seed_for_queries(db)
    assert state_store.get_latest_action("ACC1_600000.SH_2026-07-30") == "CONFIRMED"

# ============================================================================
# Task D1（live-mainchain-fixes）：get_order_placed_qty（止盈差额补挂）
# ============================================================================
def test_get_order_placed_qty_excludes_terminal(monkeypatch, tmp_path):
    """get_order_placed_qty：只合计未终态 TP 行（REJECTED/CANCELLED 不计）。"""
    from trading import state_store

    monkeypatch.setattr(state_store, "_DEFAULT_DB", str(tmp_path / "state.db"))
    state_store.init_store()
    aid, d, sym = "TEST_ACC", "2026-08-01", "600000.SH"
    state_store.upsert_account(aid, broker="qmt")
    state_store.insert_order(f"{d}_{sym}_TP2_1", f"{aid}_{sym}_{d}", aid, d, sym, "sell", "TP2", 100, 11.0, state="SUBMITTED")
    state_store.insert_order(f"{d}_{sym}_TP2_2", f"{aid}_{sym}_{d}", aid, d, sym, "sell", "TP2", 100, 11.0, state="REJECTED")
    assert state_store.get_order_placed_qty(aid, d, sym, "TP2") == 100.0
