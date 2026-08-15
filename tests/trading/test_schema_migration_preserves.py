# -*- coding: utf-8 -*-
"""G5：state_store schema 迁移保数据单测（备份式重建）。

物理意图（2026-08-13 g-wave-p0-guards · Task G5）：
    原 init_store 的旧表迁移用 ``DROP TABLE`` 直接重建（state_store.py:170-173 fill 表、
    :203-208 position 表）。SQLite 改 PK/类型/NOT NULL 约束确需重建表，但 DROP 无条件丢数据
    ——连错库/历史回灌/spec §7.1「live-前-无-数据」假设被破坏时，成交/持仓真相源即清零。
    spec 2026-08-13-audit-remediation-design.md:174 点名此风险：「连错库即清零」。

    本测试覆盖改后的「导出旧行 → DROP → CREATE → 回灌」备份式迁移：
      ① position 表：旧表（无 account_id/avg_price）→ 迁移后行数守恒 + symbol/qty 旧列值
         保留 + account_id 用默认兜底（NOT NULL 约束满足）；
      ② fill 表：旧表（无 traded_time，**真实 position_book 早期 schema**：
         order_id NOT NULL + symbol/direction/qty/price/applied_at + UNIQUE(order_id)）
         → 迁移后行数守恒 + traded_time == applied_at（成交时间近似回灌）。

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


def _old_fill_db(db_path: str) -> None:
    """造旧 fill 表（无 traded_time，**真实 position_book 早期 schema**）。

    真实旧 fill schema（见 position_book 历史建表 + test_position_book.py
    ``test_fill_schema_migration`` 范式）：
        order_id TEXT NOT NULL, symbol, direction, qty, price, applied_at,
        UNIQUE(order_id)
    只缺 traded_time 一列。注意：本 schema 是生产真实形态，**非** fill_id/ts_code/trade_date/
    price/vol 虚构 schema（虚构 schema 缺 order_id/direction/applied_at 等 NOT NULL 列，
    会掩盖回灌可行性）。
    """
    con = sqlite3.connect(db_path)
    con.execute("""CREATE TABLE fill (
        fill_id    INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id   TEXT NOT NULL,
        symbol     TEXT,
        direction  TEXT,
        qty        REAL,
        price      REAL,
        applied_at TEXT,
        UNIQUE(order_id)
    )""")
    # 插 2 行旧成交（不同 order_id，各有 applied_at 作 traded_time 近似源）
    con.execute(
        "INSERT INTO fill(fill_id, order_id, symbol, direction, qty, price, applied_at)"
        " VALUES(1, 'o1', '600000.SH', 'BUY', 100.0, 10.0, '2024-01-01T09:30:00')")
    con.execute(
        "INSERT INTO fill(fill_id, order_id, symbol, direction, qty, price, applied_at)"
        " VALUES(2, 'o2', '000001.SZ', 'SELL', 200.0, 15.0, '2024-01-02T14:00:00')")
    con.commit()
    con.close()


def test_fill_migration_backfills_traded_time_from_applied_at(tmp_path, monkeypatch):
    """旧 fill 表（无 traded_time，真实 schema）→ init_store 迁移：
    ① 备份 sidecar 文件存在；② 行数守恒（2 → 2）；③ traded_time == applied_at（回灌近似）；
    ④ 旧列值（symbol/direction/qty/price）保留。

    物理意图（fix round 1 · 重要订正）：fill 是成交真相源，应保 DB 而非仅 sidecar。
    旧 fill 表只缺 traded_time 一列，回灌时令 traded_time = applied_at 作成交时间近似：
      - traded_time NOT NULL：applied_at 旧值非空即满足；
      - UNIQUE(order_id, traded_time)：旧表 order_id 已 UNIQUE ⇒ (order_id, traded_time)
        必然唯一 ⇒ 回灌永不撞约束（reviewer 实测 2/2 零冲突）。
    本测用**真实旧 schema**（order_id/symbol/direction/qty/price/applied_at, UNIQUE(order_id)，
    对齐 test_position_book.py:134-137 范式），非虚构 schema。
    """
    db_path = str(tmp_path / "state.db")
    _old_fill_db(db_path)
    monkeypatch.setattr(state_store, "_DEFAULT_DB", db_path)
    backup_dir = tmp_path / "backup"
    monkeypatch.setattr(state_store, "_MIGRATION_BACKUP_DIR", str(backup_dir))

    state_store.init_store(db_path)

    # ① sidecar 备份文件存在（连错库可恢复双保险）
    backups = list(backup_dir.glob("state_store_fill_backup_*.json"))
    assert backups, "fill 表迁移应备份到 sidecar JSON"
    data = json.loads(backups[0].read_text(encoding="utf-8"))
    assert len(data) == 2
    assert {row["order_id"] for row in data} == {"o1", "o2"}

    # ②③④ 行数守恒 + traded_time == applied_at 回灌 + 旧列值保留
    with sqlite3.connect(db_path) as con:
        rows = con.execute(
            "SELECT order_id, traded_time, symbol, direction, qty, price, applied_at"
            " FROM fill ORDER BY order_id"
        ).fetchall()

    assert len(rows) == 2, f"fill 行数守恒失败：旧 2 行，新 {len(rows)} 行"
    by_oid = {r[0]: r for r in rows}
    # traded_time == applied_at（成交时间近似回灌的核心断言）
    assert by_oid["o1"][1] == "2024-01-01T09:30:00", "traded_time 应取 applied_at 近似回灌"
    assert by_oid["o2"][1] == "2024-01-02T14:00:00", "traded_time 应取 applied_at 近似回灌"
    # 旧列值保留
    assert by_oid["o1"][2] == "600000.SH" and by_oid["o1"][3] == "BUY"
    assert by_oid["o1"][4] == 100.0 and by_oid["o1"][5] == 10.0
    assert by_oid["o2"][2] == "000001.SZ" and by_oid["o2"][3] == "SELL"
    assert by_oid["o2"][4] == 200.0 and by_oid["o2"][5] == 15.0


# ============================================================================
# CR-5（2026-08-15 tech-debt-full-wave）：fill.direction CHECK 约束迁移
# ============================================================================
# 物理意图：「防超卖＞防漏挂」三层同向盲区的 schema 层收口——direction 脏值
# （如小写 'buy'）会让 audit 的 ``direction == "BUY"`` 判定失真（小写被当 -qty
# 记成卖出）→ 净额符号错 → 漏挂向静默 PASS。DB 层 CHECK 让旁路写入（运维脚本/
# 迁移回灌）也有兜底。SQLite 给既有列加 CHECK 必须重建表 → 复用 G5 备份式迁移。
# 迁移前置实证（2026-08-15 只读 mode=ro）：生产 logs/trading_state.db fill 表
# 4 行 direction 全部 'BUY'（大写）→ 迁移 4/4 回灌零跳行。

def _recreate_fill_without_check(db_path: str) -> None:
    """把 fill 表替换为「当前生产形态」（direction 无 CHECK，account_id/strategy 齐）。

    生产 logs/trading_state.db 的 fill 表由旧版 init_store 建（基座 CREATE 无 CHECK
    + ALTER 追加 account_id/strategy——2026-08-15 只读实证 DDL 无 CHECK）。CR-5 后
    init_store 建的新库天然带 CHECK，故测试须手工复刻「升级前旧库」形态，才能让
    后续 init_store 真正触发 CHECK 迁移分支。
    """
    con = sqlite3.connect(db_path)
    con.execute("DROP TABLE fill")
    con.execute("""CREATE TABLE fill (
        fill_id      INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id     TEXT NOT NULL,
        traded_time  TEXT NOT NULL,
        symbol       TEXT NOT NULL,
        direction    TEXT NOT NULL,
        qty          REAL NOT NULL,
        price        REAL NOT NULL,
        applied_at   TEXT NOT NULL,
        account_id   TEXT REFERENCES account(account_id),
        strategy     TEXT,
        UNIQUE(order_id, traded_time)
    )""")
    con.commit()
    con.close()


def test_fill_direction_check_migration_skips_dirty_rows(tmp_path, monkeypatch):
    """CR-5：现形态 fill 表（direction 无 CHECK）含小写脏值 → init_store 迁移：
    ① 脏行撞 CHECK 跳入 sidecar（不进新表）；② 干净行回灌且 account_id/strategy
    归因数据保留；③ 新表 direction 列有 CHECK（脏值 INSERT 直接 IntegrityError）。

    RED 依据：当前 init_store 无 CHECK 迁移——脏行 'buy' 原样留在表里（断言 ②
    剩余行集 FAIL），新表插脏值不报错（断言 ③ FAIL）。
    """
    db_path = str(tmp_path / "state.db")
    backup_dir = tmp_path / "backup"
    # 隔离 _DEFAULT_DB（红线：绝不让任何未显式传 db_path 的调用落到生产库）
    monkeypatch.setattr(state_store, "_DEFAULT_DB", db_path)
    monkeypatch.setattr(state_store, "_MIGRATION_BACKUP_DIR", str(backup_dir))
    # 先 init 建全部表 + 满足 FK 的 account 行，再把 fill 表复刻为「升级前旧形态」
    state_store.init_store(db_path)
    state_store.upsert_account("ACC1", broker="qmt", db_path=db_path)
    _recreate_fill_without_check(db_path)
    con = sqlite3.connect(db_path)
    # 一行干净（BUY 大写，带 account_id/strategy 归因）+ 一行脏（buy 小写——
    # 历史写入侧无校验时代的脏值形态）
    con.execute(
        "INSERT INTO fill(order_id, traded_time, symbol, direction, qty, price,"
        " applied_at, account_id, strategy) VALUES('o1', '2026-01-01T09:30:00',"
        " '600000.SH', 'BUY', 100.0, 10.0, 't1', 'ACC1', 'neckline')")
    con.execute(
        "INSERT INTO fill(order_id, traded_time, symbol, direction, qty, price,"
        " applied_at, account_id, strategy) VALUES('o2', '2026-01-02T09:30:00',"
        " '000001.SZ', 'buy', 200.0, 15.0, 't2', 'ACC1', NULL)")
    con.commit()
    con.close()
    # 第二次 init：升级后引擎重启形态——检测 direction 无 CHECK → 备份式重建
    state_store.init_store(db_path)

    # ① sidecar 备份 2 行（DROP 前导出，脏行也留档可人工订正）
    backups = list(backup_dir.glob("state_store_fill_backup_*.json"))
    assert backups, "CHECK 迁移应先备份到 sidecar JSON"
    data = json.loads(backups[0].read_text(encoding="utf-8"))
    assert {row["order_id"] for row in data} == {"o1", "o2"}

    # ② 新表只剩干净行；脏行 'buy' 撞 CHECK 被跳过（跳行数据在 sidecar）
    with sqlite3.connect(db_path) as con:
        rows = con.execute(
            "SELECT order_id, direction, account_id, strategy FROM fill"
        ).fetchall()
        # ③ 新表 CHECK 生效：脏 direction 直接 INSERT 必须 IntegrityError
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(
                "INSERT INTO fill(order_id, traded_time, symbol, direction, qty,"
                " price, applied_at) VALUES('o3', 't3', 'X', 'buy', 1, 1, 't')")

    assert {r[0] for r in rows} == {"o1"}, \
        f"脏行 o2('buy') 应被 CHECK 跳入 sidecar，实际剩余: {[tuple(r) for r in rows]}"
    clean = rows[0]
    # 干净行回灌后 account_id/strategy 归因数据保留（迁移不得丢既有归因列）
    assert clean[1] == "BUY"
    assert clean[2] == "ACC1" and clean[3] == "neckline", \
        "回灌必须保住 account_id/strategy（digest/归因消费口的真相源字段）"


def test_fill_direction_check_migration_idempotent(tmp_path, monkeypatch):
    """CR-5 迁移幂等：CHECK 已存在时再跑 init_store 不重复重建（无新 sidecar、行不变）。

    Why：engine 启动期多入口重复调 init_store（boot/lifespan/补跑），迁移判定
    「现表 direction 无 CHECK」必须幂等——否则每次启动都 DROP 重建一遍 fill 表
    （fill_id 重排 + sidecar 刷屏 + 回灌风险叠加）。
    """
    db_path = str(tmp_path / "state.db")
    backup_dir = tmp_path / "backup"
    # 隔离 _DEFAULT_DB（红线：绝不让任何未显式传 db_path 的调用落到生产库）
    monkeypatch.setattr(state_store, "_DEFAULT_DB", db_path)
    monkeypatch.setattr(state_store, "_MIGRATION_BACKUP_DIR", str(backup_dir))
    state_store.init_store(db_path)
    state_store.upsert_account("ACC1", broker="qmt", db_path=db_path)
    _recreate_fill_without_check(db_path)
    con = sqlite3.connect(db_path)
    con.execute(
        "INSERT INTO fill(order_id, traded_time, symbol, direction, qty, price,"
        " applied_at, account_id) VALUES('o1', 't1', '600000.SH', 'BUY', 100.0,"
        " 10.0, 't1', 'ACC1')")
    con.commit()
    con.close()
    state_store.init_store(db_path)   # 第一次迁移（无 CHECK → 重建）
    state_store.init_store(db_path)   # 第二次（有 CHECK → 必须跳过）

    backups = list(backup_dir.glob("state_store_fill_backup_*.json"))
    assert len(backups) == 1, f"幂等失败：CHECK 在表上不应再触发迁移 sidecar，实际 {len(backups)} 个"
    with sqlite3.connect(db_path) as con:
        n = con.execute("SELECT COUNT(*) FROM fill").fetchone()[0]
    assert n == 1, "重复 init_store 不得丢/复制 fill 行"
