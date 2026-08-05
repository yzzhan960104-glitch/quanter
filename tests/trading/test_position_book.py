# -*- coding: utf-8 -*-
"""position_book 单测：本地持仓账本 schema/增量幂等/加权avg/entry_date。

物理意图（live-readiness spec §3 地基）：验证对账「本地侧」单一真理源 ACID 行为——
- fill 增量幂等 UNIQUE(order_id, traded_time)（部分成交累加精度，R1 红线）
- BUY 加权 avg_price / SELL avg 不变（A 股口径）/ entry_date 首次锁定
- schema 迁移（旧表 DROP 重建）

注（B5）：daily_equity 快照 reader/writer（snapshot_start_equity/get_start_equity）
已删——熔断基线快照迁 state_store.account_daily（test_state_store.py 覆盖）。
"""
from __future__ import annotations

import sqlite3

import pytest

from trading import position_book


@pytest.fixture
def db(tmp_path, monkeypatch):
    """每个测试用独立 tmp db（隔离），patch _DEFAULT_DB 让 engine 间接调用也命中 tmp。"""
    db_path = str(tmp_path / "state.db")
    monkeypatch.setattr(position_book, "_DEFAULT_DB", db_path)
    position_book.init_db()
    return db_path


# ===== 既有用例（加 traded_time 参数，对齐增量幂等签名）=====

def test_init_db_idempotent(db):
    """重复 init_db 不报错（CREATE TABLE IF NOT EXISTS 幂等）。"""
    position_book.init_db()
    position_book.init_db()


def test_apply_fill_buy_accumulates(db):
    """BUY 两次（不同 order_id+traded_time）→ qty 累加。"""
    assert position_book.apply_fill("o1", "300001.SZ", "BUY", 100, 10.0, "t1") is True
    assert position_book.apply_fill("o2", "300001.SZ", "BUY", 200, 10.5, "t2") is True
    assert position_book.get_local_positions() == {"300001.SZ": 300.0}


def test_apply_fill_sell_decrements_and_clears_zero(db):
    """BUY 后 SELL → qty 减；归零从 position 删除。"""
    position_book.apply_fill("o1", "300001.SZ", "BUY", 100, 10.0, "t1")
    position_book.apply_fill("o2", "300001.SZ", "SELL", 100, 11.0, "t2")
    assert position_book.get_local_positions() == {}


def test_apply_fill_idempotent(db):
    """同 (order_id, traded_time) 重推 → 返 False，qty 不变（R1 幂等红线）。"""
    assert position_book.apply_fill("o1", "300001.SZ", "BUY", 100, 10.0, "t1") is True
    assert position_book.apply_fill("o1", "300001.SZ", "BUY", 100, 10.0, "t1") is False  # 重推
    assert position_book.get_local_positions() == {"300001.SZ": 100.0}


def test_apply_fill_unknown_direction_raises(db):
    """direction 非 BUY/SELL → 抛 ValueError。"""
    with pytest.raises(ValueError):
        position_book.apply_fill("o1", "300001.SZ", "TRADE", 100, 10.0, "t1")


def test_get_local_positions_excludes_zero(db):
    """qty=0 不返回；多标的混合正确。"""
    position_book.apply_fill("o1", "300001.SZ", "BUY", 100, 10.0, "t1")
    position_book.apply_fill("o2", "688001.SH", "BUY", 200, 20.0, "t2")
    position_book.apply_fill("o3", "688001.SH", "SELL", 200, 21.0, "t3")  # 归零
    assert position_book.get_local_positions() == {"300001.SZ": 100.0}


# ===== live-readiness 地基新增（增量幂等 + 加权 avg + entry_date + schema 迁移 + 熔断基线）=====

def test_apply_fill_partial_increment(db):
    """同 order_id 多笔部分成交（不同 traded_time）→ qty 累加；同笔重推幂等跳过。

    物理意图（R1 部分成交精度）：100 股买单分 30+40+30 三笔回报，每笔 traded_time 不同，
    全部入账累加 → qty=100。同笔（同 traded_time）重推返 False 不重复加。
    """
    assert position_book.apply_fill("o1", "300001.SZ", "BUY", 30, 10.0, "09:30:00") is True
    assert position_book.apply_fill("o1", "300001.SZ", "BUY", 40, 10.0, "10:00:00") is True
    assert position_book.apply_fill("o1", "300001.SZ", "BUY", 30, 10.0, "14:00:00") is True
    assert position_book.get_local_positions() == {"300001.SZ": 100.0}
    # 同笔重推（同 traded_time）幂等跳过
    assert position_book.apply_fill("o1", "300001.SZ", "BUY", 30, 10.0, "09:30:00") is False
    assert position_book.get_local_positions() == {"300001.SZ": 100.0}  # 没翻倍


def test_apply_fill_avg_price_weighted(db):
    """BUY 加权 avg_price；SELL 不变（A 股口径）。"""
    position_book.apply_fill("o1", "300001.SZ", "BUY", 100, 10.0, "t1")
    position_book.apply_fill("o2", "300001.SZ", "BUY", 100, 12.0, "t2")
    # 加权 avg = (100*10 + 100*12) / 200 = 11.0
    with sqlite3.connect(db) as con:
        row = con.execute("SELECT avg_price FROM position WHERE symbol='300001.SZ'").fetchone()
    assert row[0] == pytest.approx(11.0, abs=0.01)
    # SELL 不动 avg
    position_book.apply_fill("o3", "300001.SZ", "SELL", 100, 11.5, "t3")
    with sqlite3.connect(db) as con:
        row = con.execute("SELECT avg_price FROM position WHERE symbol='300001.SZ'").fetchone()
    assert row[0] == pytest.approx(11.0, abs=0.01)


def test_apply_fill_entry_date_locked(db):
    """首次 BUY 写 entry_date；加仓不改；清仓后重新 BUY 写新 entry_date。

    物理意图：entry_date 是 max_holding/trailing 的 holding_days 基准，必须锁定建仓日
    （加仓不改，否则 holding_days 被加仓日重置）。
    """
    # 首次 BUY → entry_date 写当日
    position_book.apply_fill("o1", "300001.SZ", "BUY", 100, 10.0, "t1")
    entry1 = position_book.get_entry_dates()["300001.SZ"]
    # 加仓 → entry_date 不改（锁定）
    position_book.apply_fill("o2", "300001.SZ", "BUY", 100, 11.0, "t2")
    entry2 = position_book.get_entry_dates()["300001.SZ"]
    assert entry1 == entry2
    # 清仓 → entry_date 随 position 删除
    position_book.apply_fill("o3", "300001.SZ", "SELL", 200, 12.0, "t3")
    assert "300001.SZ" not in position_book.get_entry_dates()
    # 重新 BUY → 写新 entry_date（old 无值，新建仓日）
    position_book.apply_fill("o4", "300001.SZ", "BUY", 100, 13.0, "t4")
    assert "300001.SZ" in position_book.get_entry_dates()


def test_fill_schema_migration(db):
    """老 fill 表（UNIQUE(order_id)，无 traded_time）→ init_db 迁移重建为新结构。

    物理意图：live-readiness 地基 schema 升级，旧 dry_run 影子库 bump 后重建。
    """
    # 手动构造老 fill 表（UNIQUE(order_id)，无 traded_time）
    with sqlite3.connect(db) as con:
        con.execute("DROP TABLE fill")
        con.execute("""CREATE TABLE fill (
            fill_id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT NOT NULL,
            symbol TEXT, direction TEXT, qty REAL, price REAL, applied_at TEXT,
            UNIQUE(order_id))""")
        con.commit()
    # init_db 迁移：检测无 traded_time → DROP 重建
    position_book.init_db()
    # 验证新结构：traded_time 列存在 + UNIQUE(order_id, traded_time)（同 order_id 不同 traded_time 都能插）
    with sqlite3.connect(db) as con:
        cols = {r[1] for r in con.execute("PRAGMA table_info(fill)").fetchall()}
        assert "traded_time" in cols
        con.execute("INSERT INTO fill(order_id,traded_time,symbol,direction,qty,price,applied_at) VALUES('o1','t1','X','BUY',100,10,'now')")
        con.execute("INSERT INTO fill(order_id,traded_time,symbol,direction,qty,price,applied_at) VALUES('o1','t2','X','BUY',100,10,'now')")
        con.commit()


def test_position_schema_migration_avg_price(db):
    """老 position 表（无 avg_price）→ init_db 迁移重建（加 avg_price/entry_date）。"""
    with sqlite3.connect(db) as con:
        con.execute("DROP TABLE position")
        con.execute("""CREATE TABLE position (
            symbol TEXT PRIMARY KEY, qty REAL NOT NULL, updated_at TEXT NOT NULL)""")
        con.commit()
    position_book.init_db()
    with sqlite3.connect(db) as con:
        cols = {r[1] for r in con.execute("PRAGMA table_info(position)").fetchall()}
        assert "avg_price" in cols and "entry_date" in cols


