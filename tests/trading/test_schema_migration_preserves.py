# -*- coding: utf-8 -*-
"""G5：state_store schema 迁移保数据单测（备份式重建）。

物理意图（2026-08-13 g-wave-p0-guards · Task G5）：
    原 init_store 的旧表迁移用 ``DROP TABLE`` 直接重建（state_store.py:170-173 fill 表、
    :203-208 position 表）。SQLite 改 PK/类型/NOT NULL 约束确需重建表，但 DROP 无条件丢数据
    ——连错库/历史回灌/spec §7.1「live-前-无-数据」假设被破坏时，成交/持仓真相源即清零。
    spec 2026-08-13-audit-remediation-design.md:174 点名此风险：「连错库即清零」。

    本测试覆盖改后的「导出旧行 → DROP → CREATE → 回灌共享列」备份式迁移：
      ① position 表：旧表（无 account_id/avg_price）→ 迁移后行数守恒 + symbol/qty 旧列值
         保留 + account_id 用默认兜底（NOT NULL 约束满足）；
      ② fill 表：旧表（无 traded_time）→ sidecar 备份文件存在（连错库可恢复）+ 新 fill 表
         为空（traded_time NOT NULL 技术约束下旧行无法回灌，备份是双保险）。

RED 依据：当前 DROP 分支直接 ``DROP TABLE`` 不导出不备份——
      - position 表测试：迁移后新表 0 行（旧行被 DROP 丢），断言行数守恒 FAIL；
      - fill 表测试：迁移后无 sidecar 备份文件，断言备份存在 FAIL。
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from trading import state_store


def _old_position_db(db_path: str) -> None:
    """造旧 position 表（无 account_id、无 avg_price，PK(symbol)）。

    模拟 position_book 早期 schema（init_store docstring：「旧 position（无 account_id 或
    symbol 单列 PK）→ DROP 重建（复合 PK，SQLite 改 PK 必须重建）」）。
    """
    con = sqlite3.connect(db_path)
    con.execute("""CREATE TABLE position (
        symbol     TEXT PRIMARY KEY,
        qty        REAL NOT NULL,
        updated_at TEXT NOT NULL
    )""")
    # 插旧持仓行（无 account_id，无 avg_price——init_store 迁移检测点）
    con.execute(
        "INSERT INTO position(symbol, qty, updated_at) VALUES('600000.SH', 100.0, '2024-01-01')")
    con.execute(
        "INSERT INTO position(symbol, qty, updated_at) VALUES('000001.SZ', 200.0, '2024-01-02')")
    con.commit()
    con.close()


def test_position_migration_preserves_rows_and_values(tmp_path, monkeypatch):
    """旧 position 表（无 account_id/avg_price）→ init_store 迁移：
    ① 行数守恒（2 → 2）；② symbol/qty 旧列值保留；③ account_id 默认兜底。

    RED 依据：当前 DROP 重建分支直接丢旧行 → 迁移后新 position 表 0 行，断言行数守恒 FAIL。
    """
    db_path = str(tmp_path / "state.db")
    _old_position_db(db_path)
    monkeypatch.setattr(state_store, "_DEFAULT_DB", db_path)
    # 备份目录隔离到 tmp_path（防污染 logs/）
    monkeypatch.setattr(state_store, "_MIGRATION_BACKUP_DIR", str(tmp_path / "backup"))

    state_store.init_store(db_path)  # 触发迁移

    with sqlite3.connect(db_path) as con:
        rows = con.execute(
            "SELECT account_id, symbol, qty FROM position ORDER BY symbol"
        ).fetchall()

    # ① 行数守恒（2 行旧数据完整保留）
    assert len(rows) == 2, f"行数守恒失败：旧 2 行，新 {len(rows)} 行（旧行被 DROP 丢）"
    # ② 旧列值保留（symbol/qty）
    by_sym = {r[1]: r for r in rows}
    assert by_sym["600000.SH"][2] == 100.0, "qty 旧值 100 应保留"
    assert by_sym["000001.SZ"][2] == 200.0, "qty 旧值 200 应保留"
    # ③ account_id 默认兜底（NOT NULL 约束满足 + 单账户默认口径）
    assert all(r[0] == state_store._DEFAULT_ACCOUNT_ID for r in rows), \
        "回灌 account_id 应默认兜底（_DEFAULT_ACCOUNT_ID）"


def test_position_migration_sidecar_backup_exists(tmp_path, monkeypatch):
    """旧 position 表迁移 → sidecar 备份文件存在（连错库可恢复双保险）。

    物理意图：备份式迁移的第一道保险——SELECT * 旧表写 sidecar JSON。即使回灌成功，
    sidecar 仍留档（连错库/再迁移可从 JSON 完整恢复）。
    """
    db_path = str(tmp_path / "state.db")
    _old_position_db(db_path)
    monkeypatch.setattr(state_store, "_DEFAULT_DB", db_path)
    backup_dir = tmp_path / "backup"
    monkeypatch.setattr(state_store, "_MIGRATION_BACKUP_DIR", str(backup_dir))

    state_store.init_store(db_path)

    backups = list(backup_dir.glob("state_store_position_backup_*.json"))
    assert backups, "sidecar 备份文件应存在"
    data = json.loads(backups[0].read_text(encoding="utf-8"))
    # 旧行全备份（2 行，含旧 schema 的 symbol/qty/updated_at 字段）
    assert len(data) == 2
    symbols = {row["symbol"] for row in data}
    assert symbols == {"600000.SH", "000001.SZ"}


def _old_fill_db_no_traded_time(db_path: str) -> None:
    """造旧 fill 表（无 traded_time、无 account_id）。

    模拟 position_book 早期 fill schema（init_store docstring：「旧 fill（无 traded_time）
    → DROP 重建」）。旧 fill 列与 position_book 早期 schema 一致（ts_code/trade_date/price/vol）。
    """
    con = sqlite3.connect(db_path)
    con.execute("""CREATE TABLE fill (
        fill_id    INTEGER PRIMARY KEY,
        ts_code    TEXT,
        trade_date TEXT,
        price      REAL,
        vol        REAL
    )""")
    con.execute(
        "INSERT INTO fill(fill_id, ts_code, trade_date, price, vol)"
        " VALUES(1, '600000.SH', '20240101', 10.0, 100.0)")
    con.commit()
    con.close()


def test_fill_migration_no_traded_time_backed_up_not_refilled(tmp_path, monkeypatch):
    """旧 fill 表（无 traded_time）→ init_store 迁移：
    ① 备份 sidecar 文件存在；② 新 fill 表为空（不回灌，traded_time NOT NULL 技术约束）。

    物理意图：fill 表新 schema 的 traded_time NOT NULL + UNIQUE(order_id, traded_time) 幂等键
    约束，旧 fill 行无 traded_time 列 → 回灌 NULL 撞 NOT NULL，或回灌哨兵值撞 UNIQUE——
    技术上无法回灌。备份到 sidecar（连错库可恢复）+ 不回灌（新表 schema 干净，account_id
    也无，避免错误账户污染 fill 真相源）。spec §7.1 live-前-无-数据 假设下 fill 表无生产数据，
    备份是双保险而非数据迁移。
    """
    db_path = str(tmp_path / "state.db")
    _old_fill_db_no_traded_time(db_path)
    monkeypatch.setattr(state_store, "_DEFAULT_DB", db_path)
    backup_dir = tmp_path / "backup"
    monkeypatch.setattr(state_store, "_MIGRATION_BACKUP_DIR", str(backup_dir))

    state_store.init_store(db_path)

    # ① sidecar 备份文件存在
    backups = list(backup_dir.glob("state_store_fill_backup_*.json"))
    assert backups, "fill 表迁移应备份到 sidecar JSON"
    data = json.loads(backups[0].read_text(encoding="utf-8"))
    assert len(data) == 1
    # 旧行字段全备份（ts_code/trade_date/price/vol）
    assert data[0]["ts_code"] == "600000.SH"

    # ② 新 fill 表为空（不回灌）
    with sqlite3.connect(db_path) as con:
        n = con.execute("SELECT COUNT(*) FROM fill").fetchone()[0]
    assert n == 0, "新 fill 表应为空（旧 fill 行无 traded_time 无法回灌）"
