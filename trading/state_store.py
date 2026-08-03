# -*- coding: utf-8 -*-
"""trading.state_store — 统一交易状态库（7 张表 = 6 核心 account/trade_event/order/fill/position/account_daily + C-2 S1 data_ready；幂等写入 + 查询）。

物理定位：trading-state-store-redesign spec §2 的「单一真相源」。把散落在 5+ 处互不同步
的存储（gw._orders 内存 / _tp_placed 内存 / position_book / live_trades.csv / trading_plan JSON）
收口到一个事务一致、跨重启、幂等保护的 SQLite 交易状态库。

Why 一个新模块而非改 position_book：position_book 只覆盖 position/fill（对账用），缺
account/trade_event/order/account_daily 四张表。state_store 是它的超集（建表含 position_book
的表，schema 一致），二者共用同一 db 文件。position_book 保留（向后兼容，engine 仍读它）。

设计哲学（spec §0）：
  - append-only 事件日志：trade_event + fill 只 INSERT，永不 UPDATE（不可变审计层）。
  - DB UNIQUE 幂等：同 trade 同 action / 同日同标的同目的委托 / 同笔成交 —— 冲突返 False，不抛。
  - 柜台查询替代内存：撤单/状态查 query_orders（柜台），不查 gw._orders 内存（新连接空 → 漏撤）。
  - 不可变审计层：fill 是 append-only，position 是 fill 的累加汇总（可变）。

幂等键（spec §2.4）：
  trade_event  UNIQUE(account_id, trade_id, action)    —— 事件重推跳过
  order        UNIQUE(account_id, trade_date, symbol, purpose) —— 重复挂单跳过
  fill         UNIQUE(order_id, traded_time)           —— 成交回报重推跳过（部分成交增量幂等）
  position     PK(account_id, symbol)                  —— 同标的一行汇总
  account_daily PK(account_id, date)                   —— 同日一行快照

约定（对齐 position_book）：file-based sqlite + _connect 上下文（WAL + foreign_keys=ON +
row_factory + 自动 commit/rollback）。幂等写 catch IntegrityError → log + return False。

C-3 cancel 幂等键约定（2026-07-31 文档化，非重建）：
    撤单路径的幂等落 DB 由「柜台回写」承担，不另起 purpose='CANCEL' 行：
      - cancel_all_open_orders（breaker._cancel_via_broker_query）在 ``account_id`` 提供时
        调 cancel_order_by_broker_oid_db(broker_oid) 把同一条 order 行的 state 回写 CANCELLED
        —— 复用 order 表既有 UNIQUE(account_id, trade_date, symbol, purpose)，不新增行。
      - pre_open 调用必须透传 account_id=_resolve_account_id()（engine.pre_open），否则柜台
        路径不回写 → DB 仍记 SUBMITTED → T+1 对账幽灵单（spec §6.1 判据决策树：撤单落 DB
        即免 CANCEL 行；account_id=None / 内存回退路径才需补 CANCEL 行或 trade_event 兜底）。
    完整审计结论见 docs/superpowers/audits/2026-07-31-cancel-idempotency-audit.md。
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

# C-6 V3：单一时间源（state_store 全部 DB 时间戳/日期 key 走 clock，与 engine.py V2 同款）。
from trading import clock

logger = logging.getLogger(__name__)

_DEFAULT_DB = "logs/trading_state.db"
# 单账户默认 account_id（与 position_book._DEFAULT_ACCOUNT_ID 同值）。多账户前所有数据归此账户。
# engine 从 .env 迁移的真实 QMT_ACCOUNT_ID 会覆盖此默认（_migrate_env_to_account）。
_DEFAULT_ACCOUNT_ID = "default"


@contextmanager
def _connect(db_path: str):
    """连接上下文：开 WAL + foreign_keys，提交/回滚自动。每次操作新建连接（SQLite 非线程安全）。

    Why foreign_keys=ON（spec §2.2 顶部 PRAGMA）：trade_event/order/fill/position/account_daily
    均外键引用 account(account_id)。开 FK 引用完整性，插孤儿行（引用不存在 account）直接 IntegrityError，
    防止状态碎片化。注意 sqlite3 默认 per-connection 关 FK，必须每次 connect 后显式开。
    复用 position_book._connect 范式（WAL + row_factory + 自动 commit/rollback）。
    """
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA foreign_keys=ON")  # 引用完整性（spec §2.2）
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def _table_exists(con, name: str) -> bool:
    """表是否存在（迁移检测用，避免对不存在的表 PRAGMA table_info 报错）。"""
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _has_column(con, table: str, column: str) -> bool:
    """表是否有某列（PRAGMA table_info 检测，迁移判定）。"""
    if not _table_exists(con, table):
        return False
    cols = {r["name"] for r in con.execute(f"PRAGMA table_info({table})").fetchall()}
    return column in cols


def init_store(db_path: str | None = None) -> None:
    """幂等建 6 张表 + schema 迁移（spec §2.2 DDL + §7.1 迁移）。

    6 张表：account / trade_event / order / fill / position / account_daily。
    ER：account 1:N (trade_event/order/fill/position/account_daily)，order 1:N fill，
    trade_event 关联 order（via order_id 字段）。

    迁移（spec §7.1）：
      - 全新库：6 张表 CREATE。
      - 旧 position（无 account_id 或 symbol 单列 PK）→ DROP 重建（复合 PK，SQLite 改 PK 必须重建）。
      - 旧 fill（无 traded_time）→ DROP 重建；旧 fill 有 traded_time 但无 account_id → ALTER ADD COLUMN（不破坏既有数据）。
      - 旧 daily_equity → 迁移到 account_daily（live 前无生产快照，DROP 重建，加 account_id + 收盘字段）。
      - account/trade_event/order/account_daily 全新表 → CREATE IF NOT EXISTS。
    live 前无生产成交/快照，丢影子数据可接受（不引 schema_version，YAGNI）。
    """
    db_path = db_path or _DEFAULT_DB
    with _connect(db_path) as con:
        # ① account（账号配置）——全新表
        con.execute("""
            CREATE TABLE IF NOT EXISTS account (
                account_id     TEXT PRIMARY KEY,
                broker         TEXT NOT NULL,
                name           TEXT,
                userdata_path  TEXT,
                session_id     INTEGER,
                strategy_name  TEXT DEFAULT 'quanter',
                mode           TEXT DEFAULT 'dry_run',
                active         INTEGER DEFAULT 1,
                created_at     TEXT NOT NULL
            )
        """)
        # ② trade_event（标的事件流 · append-only）——全新表
        con.execute("""
            CREATE TABLE IF NOT EXISTS trade_event (
                event_id     INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id   TEXT NOT NULL REFERENCES account(account_id) ON DELETE RESTRICT,
                trade_id     TEXT NOT NULL,
                symbol       TEXT NOT NULL,
                action       TEXT NOT NULL,
                timestamp    TEXT NOT NULL,
                order_id     TEXT,
                qty          REAL,
                price        REAL,
                realized_pnl REAL,
                meta         TEXT,
                UNIQUE(account_id, trade_id, action)
            )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_trade_event_trade ON trade_event(trade_id)")
        # ③ order（委托记录 · 幂等 UNIQUE）——全新表
        con.execute("""
            CREATE TABLE IF NOT EXISTS "order" (
                order_id     TEXT PRIMARY KEY,
                trade_id     TEXT NOT NULL,
                account_id   TEXT NOT NULL REFERENCES account(account_id) ON DELETE RESTRICT,
                trade_date   TEXT NOT NULL,
                symbol       TEXT NOT NULL,
                side         TEXT NOT NULL,
                purpose      TEXT NOT NULL,
                qty          REAL NOT NULL,
                price        REAL NOT NULL,
                broker_oid   TEXT,
                state        TEXT NOT NULL DEFAULT 'PENDING',
                filled_qty   REAL,
                filled_price REAL,
                submitted_at TEXT,
                filled_at    TEXT,
                UNIQUE(account_id, trade_date, symbol, purpose)
            )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_order_trade ON \"order\"(trade_id)")
        # ④ fill（成交流水 · append-only）——升级既有表（加 account_id 列）
        if _table_exists(con, "fill") and not _has_column(con, "fill", "traded_time"):
            # 旧 fill 无 traded_time（position_book 早期 schema）→ DROP 重建
            logger.info("state_store 迁移：旧 fill 表无 traded_time，DROP 重建（增量幂等）")
            con.execute("DROP TABLE fill")
        con.execute("""
            CREATE TABLE IF NOT EXISTS fill (
                fill_id      INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id     TEXT NOT NULL,
                traded_time  TEXT NOT NULL,
                symbol       TEXT NOT NULL,
                direction    TEXT NOT NULL,
                qty          REAL NOT NULL,
                price        REAL NOT NULL,
                applied_at   TEXT NOT NULL,
                UNIQUE(order_id, traded_time)
            )
        """)
        # fill 加 account_id 列（spec §7.1：ALTER ADD COLUMN，默认 NULL，不破坏既有数据）
        # 用 IF NOT EXISTS 语义（SQLite 无 ADD COLUMN IF NOT EXISTS，手动检测）
        if not _has_column(con, "fill", "account_id"):
            con.execute("ALTER TABLE fill ADD COLUMN account_id TEXT REFERENCES account(account_id)")
            logger.info("state_store 迁移：fill 表加 account_id 列（FK 引用 account）")
        con.execute("CREATE INDEX IF NOT EXISTS idx_fill_symbol ON fill(symbol)")
        # ⑤ position（当前持仓 · fill 累加汇总，可变）——升级为复合 PK
        if _table_exists(con, "position") and (
            not _has_column(con, "position", "account_id")
            or not _has_column(con, "position", "avg_price")
        ):
            logger.info("state_store 迁移：旧 position 表无 account_id/avg_price，DROP 重建（复合PK）")
            con.execute("DROP TABLE position")
        con.execute("""
            CREATE TABLE IF NOT EXISTS position (
                account_id  TEXT NOT NULL REFERENCES account(account_id) ON DELETE RESTRICT,
                symbol      TEXT NOT NULL,
                qty         REAL NOT NULL,
                avg_price   REAL,
                entry_date  TEXT,
                updated_at  TEXT NOT NULL,
                PRIMARY KEY (account_id, symbol)
            )
        """)
        # ⑥ account_daily（账户日级快照 · pre_open 写 start / post_close 写 close）——全新表
        # 与 position_book 的 daily_equity 共存（后者是单 date PK 的熔断基线，backward compat；
        # account_daily 是多账户快照 + 收盘字段 + daily_pnl，state_store 收口盈亏用）。
        # 不 DROP daily_equity：position_book.snapshot_start_equity/get_start_equity 仍读写它
        # （pre_open/post_close 熔断基线），删了会让 position_book 的权益函数崩。
        con.execute("""
            CREATE TABLE IF NOT EXISTS account_daily (
                account_id          TEXT NOT NULL REFERENCES account(account_id) ON DELETE RESTRICT,
                date                TEXT NOT NULL,
                start_total_asset   REAL,
                start_cash          REAL,
                close_total_asset   REAL,
                close_cash          REAL,
                close_market_value  REAL,
                daily_pnl           REAL,
                daily_pnl_pct       REAL,
                start_snap_at       TEXT,
                close_snap_at       TEXT,
                PRIMARY KEY (account_id, date)
            )
        """)
        # ⑦ data_ready（数据就绪信号 · eod/pre_open 前置门禁用）——全新表
        # C-2 S1：采集完成后写一行 (date, dataset) 就绪事件，eod/pre_open 流读它判断是否可推进。
        # INSERT OR REPLACE 幂等（同日重采覆盖），PK(date, dataset) 多数据集独立。
        con.execute("""
            CREATE TABLE IF NOT EXISTS data_ready (
                date          TEXT NOT NULL,
                dataset       TEXT NOT NULL,
                ok            INTEGER NOT NULL,
                melted        INTEGER NOT NULL DEFAULT 0,
                latest_date   TEXT,
                expected_date TEXT NOT NULL,
                ready_at      TEXT NOT NULL,
                message       TEXT,
                PRIMARY KEY (date, dataset)
            )
        """)


# ============================= T2：account 表 CRUD + .env 迁移 =============================

def upsert_account(account_id: str, broker: str, *, name: str | None = None,
                   userdata_path: str | None = None, session_id: int | None = None,
                   strategy_name: str | None = None, mode: str | None = None,
                   active: int | None = None, db_path: str | None = None) -> None:
    """写/覆盖 account 配置行（INSERT OR REPLACE 幂等）。

    物理意图：engine 启动期把 .env 的 QMT_* 配置落库（spec §7.2），让账户配置成为 DB 真相源
    （而非散在环境变量）。**必须用 UPSERT（ON CONFLICT DO UPDATE）而非 INSERT OR REPLACE**：
    REPLACE 语义是"先 DELETE 旧行再 INSERT"，而 trade_event/order/fill 等子表
    `REFERENCES account(account_id) ON DELETE RESTRICT`——账户已有成交/事件后，
    REPLACE 的 DELETE 被 RESTRICT 挡住 → `FOREIGN KEY constraint failed` →
    engine bootstrap 装配失败（2026-08-03 23:01 实证：QMT 已连接但 TradingEngine 未装配）。
    UPSERT 只 UPDATE 不删行，子表引用安全。None 字段用 DEFAULT（strategy_name='quanter'
    /mode='dry_run'/active=1），保证最小配置可写。
    """
    db_path = db_path or _DEFAULT_DB
    now = clock.now().isoformat()
    with _connect(db_path) as con:
        con.execute(
            "INSERT INTO account(account_id, broker, name, userdata_path,"
            " session_id, strategy_name, mode, active, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(account_id) DO UPDATE SET"
            " broker=excluded.broker, name=excluded.name,"
            " userdata_path=excluded.userdata_path, session_id=excluded.session_id,"
            " strategy_name=excluded.strategy_name, mode=excluded.mode,"
            " active=excluded.active",
            (account_id, broker, name, userdata_path, session_id,
             strategy_name or "quanter", mode or "dry_run",
             1 if active is None else active, now))


def get_account(account_id: str, *, db_path: str | None = None) -> dict | None:
    """读 account 配置行（broker/name/session_id/mode 等）。不存在返 None。"""
    db_path = db_path or _DEFAULT_DB
    with _connect(db_path) as con:
        row = con.execute("SELECT * FROM account WHERE account_id=?", (account_id,)).fetchone()
    return dict(row) if row else None


def _migrate_env_to_account(db_path: str | None = None) -> str | None:
    """从 .env 读 QMT_* 配置 → upsert_account（spec §7.2）。

    物理意图：engine 启动期 _run_forever 调本函数，把环境变量里的账户配置落库。QMT_ACCOUNT_ID
    缺失时跳过（dry_run 无 broker 配置也允许启动）。session_id 容错非数字（默认 None）。
    返回迁移的 account_id（None 表示无配置可迁）。
    """
    db_path = db_path or _DEFAULT_DB
    account_id = os.getenv("QMT_ACCOUNT_ID")
    if not account_id:
        return None
    session_raw = os.getenv("QMT_SESSION_ID")
    try:
        session_id = int(session_raw) if session_raw else None
    except (TypeError, ValueError):
        session_id = None
    upsert_account(
        account_id, broker="qmt",
        userdata_path=os.getenv("QMT_USERDATA_PATH"),
        session_id=session_id,
        strategy_name=os.getenv("QMT_STRATEGY_NAME"),
        mode=os.getenv("AUTO_TRADE_MODE", "dry_run"),
        db_path=db_path,
    )
    return account_id


# ============================= T3：trade_event / order / fill 幂等写入 =============================

def insert_trade_event(account_id: str, trade_id: str, symbol: str, action: str, *,
                       order_id: str | None = None, qty: float | None = None,
                       price: float | None = None, realized_pnl: float | None = None,
                       meta: str | None = None, timestamp: str | None = None,
                       db_path: str | None = None) -> bool:
    """写 trade_event（append-only，幂等）。

    幂等（spec §2.4）：UNIQUE(account_id, trade_id, action) —— 同 trade 同 action 重推
    IntegrityError → 返 False（不重复记）。不同 action 各自一行（事件流），不覆盖。
    meta：SIGNAL action 时存计划参数 JSON（get_trade_plan 读），其余 action 通常 None。
    Returns: True=首次写入；False=重复 (account_id, trade_id, action) 跳过。
    """
    db_path = db_path or _DEFAULT_DB
    ts = timestamp or clock.now().isoformat()
    with _connect(db_path) as con:
        try:
            con.execute(
                "INSERT INTO trade_event(account_id, trade_id, symbol, action, timestamp,"
                " order_id, qty, price, realized_pnl, meta) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (account_id, trade_id, symbol, action, ts,
                 order_id, qty, price, realized_pnl, meta))
            return True
        except sqlite3.IntegrityError:
            # 同 (account_id, trade_id, action) 重推幂等跳过（事件流不重复记）。
            logger.info("insert_trade_event 跳过重复 account=%s trade=%s action=%s",
                        account_id, trade_id, action)
            return False


def insert_order(order_id: str, trade_id: str, account_id: str, trade_date: str,
                 symbol: str, side: str, purpose: str, qty: float, price: float, *,
                 broker_oid: str | None = None, state: str = "PENDING",
                 filled_qty: float | None = None, filled_price: float | None = None,
                 submitted_at: str | None = None, filled_at: str | None = None,
                 db_path: str | None = None) -> bool:
    """写 order（委托记录，幂等）。

    幂等（spec §2.4）：UNIQUE(account_id, trade_date, symbol, purpose) —— 同日同标的同目的
    重复挂单（pre_open 重跑 / 止盈重挂 / 止损重发）IntegrityError → 返 False（不重复挂）。
    order_id 是主键（{date}_{symbol}_{purpose}_{seq}），但幂等键是 UNIQUE 四元组（防止用不同
    order_id 绕过）。
    Returns: True=首次写入；False=重复委托（同四元组）跳过。
    """
    db_path = db_path or _DEFAULT_DB
    with _connect(db_path) as con:
        try:
            con.execute(
                "INSERT INTO \"order\"(order_id, trade_id, account_id, trade_date, symbol, side,"
                " purpose, qty, price, broker_oid, state, filled_qty, filled_price,"
                " submitted_at, filled_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (order_id, trade_id, account_id, trade_date, symbol, side, purpose,
                 float(qty), float(price), broker_oid, state, filled_qty, filled_price,
                 submitted_at, filled_at))
            return True
        except sqlite3.IntegrityError:
            # 同 (account_id, trade_date, symbol, purpose) 重挂幂等跳过（委托不重复）。
            logger.info("insert_order 跳过重复 account=%s date=%s symbol=%s purpose=%s",
                        account_id, trade_date, symbol, purpose)
            return False


def update_order_state(order_id: str, state: str, *, broker_oid: str | None = None,
                       filled_qty: float | None = None, filled_price: float | None = None,
                       submitted_at: str | None = None, filled_at: str | None = None,
                       db_path: str | None = None) -> None:
    """更新 order 的状态/柜台单号/成交量（撤单/成交回报回填用）。

    物理意图：order 表是可变的（与 trade_event/fill 的 append-only 不同）。pre_open submit 后
    回填 broker_oid（seq→real 映射）；cancel_all_open_orders 回写 state=CANCELLED；成交回报回填
    filled_qty/filled_price/state=FILLED。幂等：多次回填同值是 no-op（UPDATE 不冲突）。

    按 order_id 主键更新（调用方持有内部 order_id 时用，如 pre_open submit 后回填）。
    柜台查询撤单路径持有 broker_oid（非主键）→ 用 cancel_order_by_broker_oid_db。
    """
    db_path = db_path or _DEFAULT_DB
    with _connect(db_path) as con:
        # 动态拼 SET 子句：只更新提供的字段（None 不覆盖已有值，保留历史回填）
        sets = ["state = ?"]
        params: list = [state]
        if broker_oid is not None:
            sets.append("broker_oid = ?"); params.append(broker_oid)
        if filled_qty is not None:
            sets.append("filled_qty = ?"); params.append(float(filled_qty))
        if filled_price is not None:
            sets.append("filled_price = ?"); params.append(float(filled_price))
        if submitted_at is not None:
            sets.append("submitted_at = ?"); params.append(submitted_at)
        if filled_at is not None:
            sets.append("filled_at = ?"); params.append(filled_at)
        params.append(order_id)
        con.execute(f"UPDATE \"order\" SET {', '.join(sets)} WHERE order_id=?", params)


def cancel_order_by_broker_oid_db(broker_oid: str, *, db_path: str | None = None) -> int:
    """按柜台真实单号回写 order.state=CANCELLED（cancel_all_open_orders 柜台查询路径用）。

    物理意图：柜台查询撤单路径（breaker._cancel_via_broker_query）持有 broker_oid（query_orders
    返回的柜台单号），而非内部 order_id 主键。撤单后按 broker_oid 列回写 state=CANCELLED，
    让 DB 账本与柜台一致（pending 列表不再含已撤单）。
    Returns: 更新行数（0=无此 broker_oid 的委托，可能未落库或已终态）。
    """
    db_path = db_path or _DEFAULT_DB
    with _connect(db_path) as con:
        cur = con.execute(
            "UPDATE \"order\" SET state='CANCELLED' WHERE broker_oid=?", (broker_oid,))
        return cur.rowcount


def add_order_qty(account_id: str, trade_date: str, symbol: str, purpose: str,
                  qty: float, price: float, *, db_path: str | None = None) -> bool:
    """止盈差额补挂：purpose 行已存在则累加 qty，否则插入（UPSERT 语义）。

    Why 非 insert_order：同 (account_id, trade_date, symbol, purpose) 一天只允许一行
    （UNIQUE 幂等键），差额补挂第二次触发时须在既有行上累加，否则 INSERT 撞主键失败 →
    柜台已发单但 DB 没记（超挂黑洞）。
    Returns: True=成功（插入或累加）；False=异常（调用方必须告警人工复核）。
    """
    db_path = db_path or _DEFAULT_DB
    oid = f"{trade_date}_{symbol}_{purpose}_1"
    trade_id = f"{account_id}_{symbol}_{trade_date}"
    with _connect(db_path) as con:
        try:
            row = con.execute(
                'SELECT order_id FROM "order" WHERE account_id=? AND trade_date=? '
                'AND symbol=? AND purpose=?',
                (account_id, trade_date, symbol, purpose)).fetchone()
            if row is not None:
                con.execute(
                    'UPDATE "order" SET qty = qty + ?, price = ? WHERE order_id=?',
                    (float(qty), float(price), row["order_id"]))
                return True
            con.execute(
                'INSERT INTO "order"(order_id, trade_id, account_id, trade_date, symbol, side,'
                ' purpose, qty, price, state) VALUES(?,?,?,?,?,?,?,?,?,?)',
                (oid, trade_id, account_id, trade_date, symbol, "sell", purpose,
                 float(qty), float(price), "SUBMITTED"))
            return True
        except Exception:
            logger.exception("add_order_qty 失败 account=%s date=%s symbol=%s purpose=%s",
                             account_id, trade_date, symbol, purpose)
            return False

def get_order_placed_qty(account_id: str, trade_date: str, symbol: str, purpose: str, *,
                         db_path: str | None = None) -> float:
    """已挂委托量合计（未终态 state）：止盈差额补挂用。

    终态（REJECTED/FAILED/CANCELLED/PARTIAL_CANCELLED）不算已挂——被拒的腿
    允许后续事件补挂（与 has_order 排除集同口径）。
    """
    db_path = db_path or _DEFAULT_DB
    with _connect(db_path) as con:
        row = con.execute(
            "SELECT COALESCE(SUM(qty), 0) AS total FROM \"order\" "
            "WHERE account_id=? AND trade_date=? AND symbol=? AND purpose=? "
            "AND state NOT IN ('REJECTED','FAILED','CANCELLED','PARTIAL_CANCELLED')",
            (account_id, trade_date, symbol, purpose)).fetchone()
    return float(row["total"]) if row else 0.0

def get_order_by_broker_oid(broker_oid: str, *, db_path: str | None = None) -> dict | None:
    """按柜台单号查 order（成交回报/方向反查用）。

    broker_oid 列在 async_response 回填前存 str(seq)、回填后存真实单号，
    调用方需按 spec §5.2 先查 real、miss 后经 _seq_to_real 反查 seq 再查。
    """
    db_path = db_path or _DEFAULT_DB
    with _connect(db_path) as con:
        row = con.execute('SELECT * FROM "order" WHERE broker_oid=?', (broker_oid,)).fetchone()
    return dict(row) if row else None


def update_order_state_by_broker_oid(
    lookup_oid: str,
    *,
    state: str | None = None,
    new_broker_oid: str | None = None,
    filled_qty: float | None = None,
    filled_price: float | None = None,
    db_path: str | None = None,
) -> int:
    """按 broker_oid 列定位更新 order（成交回报/async_response 持柜台单号时用）。

    lookup_oid 是定位值（回填前 str(seq)，回填后真实单号）；None 字段不动。
    Returns: 更新行数（0=未命中，调用方必须处理竞态：WARN + 后续事件补推进）。
    """
    db_path = db_path or _DEFAULT_DB
    with _connect(db_path) as con:
        sets: list[str] = []
        params: list = []
        if state is not None:
            sets.append("state = ?"); params.append(state)
        if new_broker_oid is not None:
            sets.append("broker_oid = ?"); params.append(new_broker_oid)
        if filled_qty is not None:
            sets.append("filled_qty = ?"); params.append(float(filled_qty))
        if filled_price is not None:
            sets.append("filled_price = ?"); params.append(float(filled_price))
        if not sets:
            return 0
        params.append(lookup_oid)
        cur = con.execute(f'UPDATE "order" SET {", ".join(sets)} WHERE broker_oid=?', params)
        return cur.rowcount

def insert_fill(order_id: str, account_id: str, traded_time: str, symbol: str,
                direction: str, qty: float, price: float, *,
                db_path: str | None = None) -> bool:
    """写 fill（成交流水，append-only，幂等）。

    幂等（spec §2.4）：UNIQUE(order_id, traded_time) —— 同笔成交（同 order_id + 同 traded_time）
    重推 IntegrityError → 返 False（不重复记）。部分成交（同 order_id 不同 traded_time）各自一行，
    累加到 position（apply_fill_to_position）。
    Returns: True=首次写入；False=重复 (order_id, traded_time) 跳过。
    """
    db_path = db_path or _DEFAULT_DB
    now = clock.now().isoformat()
    with _connect(db_path) as con:
        try:
            con.execute(
                "INSERT INTO fill(order_id, traded_time, symbol, direction, qty, price,"
                " applied_at, account_id) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                (order_id, traded_time, symbol, direction, float(qty), float(price), now,
                 account_id))
            return True
        except sqlite3.IntegrityError:
            # 同 (order_id, traded_time) 成交回报重推幂等跳过。
            logger.info("insert_fill 跳过重复 order_id=%s traded_time=%s", order_id, traded_time)
            return False


# ============================= T4：position / account_daily 读写 =============================

def apply_fill_to_position(account_id: str, symbol: str, direction: str, qty: float,
                           price: float, traded_time: str, *,
                           db_path: str | None = None) -> None:
    """把一笔成交应用到 position（加权 avg + entry_date 锁定，归零删除）。

    物理意图：fill 是 append-only 流水，position 是它的累加汇总（可变）。本函数在 insert_fill
    成功后调用，把本笔增量累加到 position。BUY 加权 avg_price；SELL 不动 avg（A 股口径）；
    entry_date 首次 BUY 锁定；归零（qty=0）删除行（对账并集不被 0 干扰）。
    与 position_book.apply_fill 的区别：本函数不写 fill 流水（由 insert_fill 单独负责，职责分离），
    仅更新 position 汇总行。traded_time 参数保留（未来按成交时间排序重算 avg 的扩展点）。
    """
    if direction not in ("BUY", "SELL"):
        raise ValueError(f"apply_fill_to_position direction 仅 BUY/SELL，收到 {direction}")
    db_path = db_path or _DEFAULT_DB
    now = clock.now().isoformat()
    today = clock.today()
    delta = float(qty) if direction == "BUY" else -float(qty)
    with _connect(db_path) as con:
        row = con.execute(
            "SELECT qty, avg_price, entry_date FROM position WHERE account_id=? AND symbol=?",
            (account_id, symbol)
        ).fetchone()
        old_qty = float(row["qty"]) if row else 0.0
        old_avg = float(row["avg_price"]) if row and row["avg_price"] is not None else None
        old_entry = row["entry_date"] if row else None
        new_qty = old_qty + delta
        if direction == "BUY":
            # 加权 avg_price：old_qty>0 加权，否则（首次建仓）取本笔价
            if old_qty > 0 and old_avg is not None and new_qty != 0:
                new_avg = (old_qty * old_avg + float(qty) * float(price)) / new_qty
            else:
                new_avg = float(price)
            new_entry = old_entry if old_entry is not None else today  # 建仓日锁定
        else:
            new_avg = old_avg
            new_entry = old_entry
        con.execute(
            "INSERT INTO position(account_id, symbol, qty, avg_price, entry_date, updated_at)"
            " VALUES(?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(account_id, symbol) DO UPDATE SET qty=excluded.qty,"
            " avg_price=excluded.avg_price, entry_date=excluded.entry_date, updated_at=excluded.updated_at",
            (account_id, symbol, new_qty, new_avg, new_entry, now))
        con.execute("DELETE FROM position WHERE qty=0")


def get_position(account_id: str, symbol: str, *, db_path: str | None = None) -> dict | None:
    """读单标的当前持仓 {qty, avg_price, entry_date}。不存在返 None。"""
    db_path = db_path or _DEFAULT_DB
    with _connect(db_path) as con:
        row = con.execute(
            "SELECT qty, avg_price, entry_date FROM position WHERE account_id=? AND symbol=?",
            (account_id, symbol)
        ).fetchone()
    return dict(row) if row else None


def snapshot_start_equity(account_id: str, date: str, total_asset: float, cash: float | None = None,
                          *, db_path: str | None = None) -> None:
    """开盘前总资产快照（pre_open 写，熔断 start_equity 基线 + post_close daily_pnl 计算）。

    幂等：INSERT 含 start 字段，已存在行（post_close 先写了 close？罕见）用 UPDATE 仅补 start。
    通常 pre_open 先写 start，post_close 再 UPDATE close。崩溃重启重入安全（覆盖 start）。
    """
    db_path = db_path or _DEFAULT_DB
    now = clock.now().isoformat()
    with _connect(db_path) as con:
        con.execute(
            "INSERT INTO account_daily(account_id, date, start_total_asset, start_cash, start_snap_at)"
            " VALUES(?, ?, ?, ?, ?)"
            " ON CONFLICT(account_id, date) DO UPDATE SET start_total_asset=excluded.start_total_asset,"
            " start_cash=excluded.start_cash, start_snap_at=excluded.start_snap_at",
            (account_id, date, float(total_asset), cash, now))


def snapshot_close_equity(account_id: str, date: str, close_total_asset: float,
                          close_cash: float | None = None, close_market_value: float | None = None,
                          *, db_path: str | None = None) -> None:
    """收盘总资产快照 + daily_pnl 计算（post_close 写）。

    daily_pnl = close_total_asset - start_total_asset（同日 pre_open 快照的差）。
    daily_pnl_pct = daily_pnl / start_total_asset（无 start 时 None）。
    幂等：ON CONFLICT 仅更新 close 字段（保留 pre_open 写的 start）。
    """
    db_path = db_path or _DEFAULT_DB
    now = clock.now().isoformat()
    with _connect(db_path) as con:
        # 读 start 算 daily_pnl（无 start 行时 pnl=None，仅记 close）
        start_row = con.execute(
            "SELECT start_total_asset FROM account_daily WHERE account_id=? AND date=?",
            (account_id, date)
        ).fetchone()
        start_total = float(start_row["start_total_asset"]) if start_row and start_row["start_total_asset"] is not None else None
        daily_pnl = (float(close_total_asset) - start_total) if start_total is not None else None
        daily_pnl_pct = (daily_pnl / start_total) if (start_total and start_total != 0) else None
        con.execute(
            "INSERT INTO account_daily(account_id, date, close_total_asset, close_cash,"
            " close_market_value, daily_pnl, daily_pnl_pct, close_snap_at)"
            " VALUES(?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(account_id, date) DO UPDATE SET close_total_asset=excluded.close_total_asset,"
            " close_cash=excluded.close_cash, close_market_value=excluded.close_market_value,"
            " daily_pnl=excluded.daily_pnl, daily_pnl_pct=excluded.daily_pnl_pct,"
            " close_snap_at=excluded.close_snap_at",
            (account_id, date, float(close_total_asset), close_cash, close_market_value,
             daily_pnl, daily_pnl_pct, now))


# ============================= T5：查询接口 =============================

def has_order(account_id: str, trade_date: str, symbol: str, purpose: str, *,
              db_path: str | None = None) -> bool:
    """幂等检查：是否已存在同 (account_id, trade_date, symbol, purpose) 委托。

    pre_open 挂 OPEN / 成交挂 TP1/TP2 / stop_loss 发 STOP 前调本函数，True 则跳过（不重复挂）。
    """
    db_path = db_path or _DEFAULT_DB
    with _connect(db_path) as con:
        # C-1 final-review fix (I-2/?-1)：过滤非活态委托——REJECTED/FAILED/CANCELLED 不算
        # 「已挂」，允许重挂。否则挂单被拒（资金不足/涨跌停挡板）后 has_order 恒 True →
        # 当日永久漏挂（pre_open OPEN）+ stop_loss/TP 被拒后裸奔/永不补挂（live 真金致命）。
        row = con.execute(
            "SELECT 1 FROM \"order\" WHERE account_id=? AND trade_date=? AND symbol=? AND purpose=?"
            " AND state NOT IN ('REJECTED','FAILED','CANCELLED')",
            (account_id, trade_date, symbol, purpose)
        ).fetchone()
    return row is not None


# trade 终态 action（最新 action 为此则 trade 已结束，不算 active）
_TERMINAL_ACTIONS = frozenset({"CLOSED", "EXPIRED", "VETOED"})


def get_active_trades(account_id: str, *, db_path: str | None = None) -> list[dict]:
    """活跃 trade 列表：最新 action 非终态（CLOSED/EXPIRED/VETOED）的 trade。

    物理意图：pre_open 遍历活跃 trade 挂单；post_close 标记 CLOSED。判断「最新 action」需
    按 event_id 取每 trade 的最大行（事件流 append-only，最新 event_id = 最新状态）。
    """
    db_path = db_path or _DEFAULT_DB
    with _connect(db_path) as con:
        rows = con.execute(
            "SELECT account_id, trade_id, symbol, action FROM trade_event WHERE event_id IN ("
            " SELECT MAX(event_id) FROM trade_event WHERE account_id=? GROUP BY trade_id"
            ") AND action NOT IN ('CLOSED','EXPIRED','VETOED')",
            (account_id,)
        ).fetchall()
    return [dict(r) for r in rows]


# order 未终态（撤单用：这些状态的委托还在柜台挂着，可撤）
_PENDING_ORDER_STATES = frozenset({"PENDING", "SUBMITTED", "PARTIAL"})


def get_pending_orders(account_id: str, *, db_path: str | None = None) -> list[dict]:
    """未终态 order 列表：state IN (PENDING/SUBMITTED/PARTIAL)。

    物理意图：cancel_all_open_orders 查柜台前可先查 DB 未终态委托（对照），post_close 查未成交委托。
    """
    db_path = db_path or _DEFAULT_DB
    with _connect(db_path) as con:
        rows = con.execute(
            "SELECT * FROM \"order\" WHERE account_id=? AND state IN ('PENDING','SUBMITTED','PARTIAL')",
            (account_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_trade_plan(trade_id: str, *, db_path: str | None = None) -> dict | None:
    """从 trade_event SIGNAL 行的 meta 读计划参数（stop_price/tp1/take_profit/atr 等）。

    物理意图（spec §3.3）：stop_loss/pre_open 读 stop_price 改从 DB（不再依赖 plan JSON）。
    meta 是 JSON 字符串（eod_plan 写入计划参数快照），无 meta 或无 SIGNAL 行返 None。
    """
    db_path = db_path or _DEFAULT_DB
    with _connect(db_path) as con:
        row = con.execute(
            "SELECT meta FROM trade_event WHERE trade_id=? AND action='SIGNAL' ORDER BY event_id DESC LIMIT 1",
            (trade_id,)
        ).fetchone()
    if row is None or row["meta"] is None:
        return None
    try:
        return json.loads(row["meta"])
    except (TypeError, ValueError):
        logger.warning("get_trade_plan meta JSON 解析失败 trade_id=%s", trade_id)
        return None


def get_entry_dates(account_id: str, *, db_path: str | None = None) -> dict[str, str]:
    """读 {symbol: entry_date}（首次建仓日，qty!=0）。max_holding/trailing 的 holding_days 基准。"""
    db_path = db_path or _DEFAULT_DB
    with _connect(db_path) as con:
        rows = con.execute(
            "SELECT symbol, entry_date FROM position WHERE account_id=? AND qty != 0 AND entry_date IS NOT NULL",
            (account_id,)
        ).fetchall()
    return {r["symbol"]: r["entry_date"] for r in rows}


def get_latest_action(trade_id: str, *, db_path: str | None = None) -> str | None:
    """某 trade_id 的最新 action（当前状态）。无事件返 None。

    判断 trade 当前处于哪个阶段：CONFIRMED→可挂单，ORDERED→已挂，FILLED→已成交，CLOSED→已平仓。
    """
    db_path = db_path or _DEFAULT_DB
    with _connect(db_path) as con:
        row = con.execute(
            "SELECT action FROM trade_event WHERE trade_id=? ORDER BY event_id DESC LIMIT 1",
            (trade_id,)
        ).fetchone()
    return row["action"] if row else None


# ============================= C-2 S1：data_ready 数据就绪信号 =============================

def upsert_data_ready(date: str, dataset: str, *, ok: bool, melted: bool,
                      latest_date: str | None, expected_date: str,
                      message: str, db_path: str | None = None) -> None:
    """幂等写数据就绪事件（同日重采覆盖，PK (date, dataset) ON CONFLICT REPLACE）。"""
    ready_at = clock.now().isoformat(timespec="seconds")
    with _connect(db_path or _DEFAULT_DB) as con:
        con.execute(
            "INSERT OR REPLACE INTO data_ready "
            "(date, dataset, ok, melted, latest_date, expected_date, ready_at, message) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (date, dataset, int(ok), int(melted), latest_date, expected_date, ready_at, message),
        )


def get_data_ready(date: str, dataset: str = "daily",
                   db_path: str | None = None) -> dict | None:
    """读某日某数据集就绪事件。无记录返 None（未采集）。"""
    with _connect(db_path or _DEFAULT_DB) as con:
        row = con.execute(
            "SELECT * FROM data_ready WHERE date=? AND dataset=?", (date, dataset),
        ).fetchone()
    return dict(row) if row else None
