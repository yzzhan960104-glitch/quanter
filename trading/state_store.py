# -*- coding: utf-8 -*-
"""trading.state_store — 统一交易状态库（7 张表 = 6 核心 account/trade_event/order/fill/position/account_daily + C-2 S1 data_ready；幂等写入 + 查询）。

物理定位：trading-state-store-redesign spec §2 的「单一真相源」。把散落在 5+ 处互不同步
的存储（gw._orders 内存 / _tp_placed 内存 / position_book / fill 表 / trading_plan JSON）
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
import re
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

# ============================= G7 终态集单源（消三套集合漂移）=============================
# 物理意图：has_order / get_order_placed_qty 的 NOT IN 与 get_pending_orders 的 IN 三处
# SQL 子句历史上各写一份硬编码集合，漂移 bug：has_order 曾漏 PARTIAL_CANCELLED → 部分
# 取消单被误判「已挂」→ 漏补挂（live 真金致命；get_order_placed_qty 的 docstring 自称
# 「与 has_order 排除集同口径」却实际不一致）。现收口为两个单源常量，三处 SQL 同源引用。
#
# DB state 列约定（order_state_to_db 契约）：OrderState.PARTIAL_FILLED 在 DB 存短形式
# "PARTIAL"，其余长形式（FILLED/CANCELLED/REJECTED/PARTIAL_CANCELLED/FAILED/SUBMITTED/PENDING）。
# FILLED（成功终态）独立于活/死二分类——它不算死态（不可重挂，已成交），也不算活态
# （不可撤单）；has_order/get_order_placed_qty 的 NOT IN dead 隐式把 FILLED 归「已挂」侧。
_ACTIVE_ORDER_STATES = ("PENDING", "SUBMITTED", "PARTIAL")            # 活态（未终态，可撤/可推进）
_DEAD_ORDER_STATES = ("REJECTED", "FAILED", "CANCELLED", "PARTIAL_CANCELLED")  # 死态（可重挂）
# SQL IN 占位符（参数化防注入；由集合长度派生，单一计算点，三处共享）
_ACTIVE_STATE_PLACEHOLDERS = ",".join("?" * len(_ACTIVE_ORDER_STATES))   # '?,?,?'
_DEAD_STATE_PLACEHOLDERS = ",".join("?" * len(_DEAD_ORDER_STATES))       # '?,?,?,?'


@contextmanager
def _connect(db_path: str):
    """连接上下文：开 WAL + foreign_keys + busy_timeout，提交/回滚自动。每次操作新建连接。

    红线契约（DG-G6 钉死）：**仅事件循环线程可写**。
        engine 的 insert_*/update_*/snapshot_* 等写路径全部在事件循环线程同步执行
        （state_store 是同步 API，engine 用 run_in_threadpool 把读口 export_trades/
        query_trades 扔到工作线程，但写口仍由事件循环直接调用）。WAL + busy_timeout
        是为跨线程读不阻塞写、跨连接写串行化的兜底，**不是鼓励多线程并发写**——多线程
        并发写同一 DB 仍会因 SQLite 单写者语义排队（不 BUSY 但串行）。如需工作线程写，
        必须显式加应用层互斥（参考 discovery/store.py:50 _write_lock 范式）。

    DG-G6（2026-08-14 g-wave-p0-guards · 对齐 backtest/tasks_db.py:45 + discovery/store.py:59
    正范式）：补齐 SQLite 并发协调三件套——
      ① ``timeout=30``：默认 5s→30s，跨线程读（export_trades/query_trades 已走
         run_in_threadpool）与事件循环写并发时，读连接遇 RESERVED 锁会等 30s 而非秒抛 BUSY；
      ② ``PRAGMA journal_mode=WAL``：Write-Ahead Logging 让读不阻塞写——跨线程读口
         查询时事件循环仍可写，rollback journal 模式下读也会被 EXCLUSIVE 锁挡住；
      ③ ``PRAGMA busy_timeout=30000``：连接级锁等待 30s（与 timeout 参数语义同源，
         显式 PRAGMA 钉死口径，防 timeout 参数被误改后失去兜底）。

    Why foreign_keys=ON（spec §2.2 顶部 PRAGMA）：trade_event/order/fill/position/account_daily
    均外键引用 account(account_id)。开 FK 引用完整性，插孤儿行（引用不存在 account）直接 IntegrityError，
    防止状态碎片化。注意 sqlite3 默认 per-connection 关 FK，必须每次 connect 后显式开。
    复用 position_book._connect 范式（WAL + row_factory + 自动 commit/rollback）。
    """
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path, timeout=30)  # DG-G6：5s→30s 防跨线程读写并发 BUSY
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=30000")      # DG-G6：锁等待 30s 兜底
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


# CR-5：fill.direction 的 CHECK 约束形态（迁移幂等判定用，与本文件两处 fill DDL 的
# CHECK 子句同源）。re.IGNORECASE 容忍 DDL 文本空白/大小写重排（ALTER 追加列会重写
# sqlite_master 存的建表文本），只认「direction IN ('BUY','SELL')」这一语义。
_FILL_DIRECTION_CHECK_RE = re.compile(
    r"CHECK\s*\(\s*direction\s+IN\s*\(\s*'BUY'\s*,\s*'SELL'\s*\)\s*\)", re.IGNORECASE)


def _fill_direction_check_present(con) -> bool:
    """fill.direction 列是否已有 BUY/SELL CHECK（只读 sqlite_master DDL 文本检测）。

    Why 不用 PRAGMA table_info：它不返回 CHECK 约束；Why 不试探 INSERT：那要写库
    （live 引擎运行中生产库只允许只读判定）。读 sqlite_master.sql 文本 + 正则是
    只读、幂等、零副作用的检测点。
    """
    if not _table_exists(con, "fill") or not _has_column(con, "fill", "direction"):
        return False
    row = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='fill'"
    ).fetchone()
    if row is None or not row[0]:
        return False
    return _FILL_DIRECTION_CHECK_RE.search(row[0]) is not None


# ============================================================================
# G5：schema 迁移备份式重建（导出 → DROP → CREATE → 回灌，治「连错库即清零」）
# ============================================================================
# 物理意图（2026-08-13 g-wave-p0-guards · Task G5 · spec 2026-08-13-audit-remediation-design:174）：
#   原 init_store 的旧表迁移用 ``DROP TABLE`` 直接重建——SQLite 改 PK/类型/NOT NULL 约束
#   确需重建，但 DROP 无条件丢数据。spec §7.1 docstring 标注「live-前-无-生产-成交/快照，
#   丢影子数据可接受」——但这一假设在「连错库 / 历史回灌 / 影子库误判」场景下被破坏，
#   成交/持仓真相源即清零。备份式迁移先 SELECT 导出 → DROP → CREATE → 回灌，保证数据不丢。
#
#   备份目录：默认 logs/（与 trading_state.db 同根，运行态可观测）；测试 monkeypatch 此
#   常量到 tmp_path 隔离（防污染 logs/）。
_MIGRATION_BACKUP_DIR = "logs"


def _dump_migration_sidecar(table: str, rows: list[dict]) -> None:
    """旧表行导出到 sidecar JSON（连错库即清零的兜底保险）。

    物理意图：DROP 重建前的双保险——即使内存回灌失败/事务回滚，sidecar JSON 仍留档，
    运维可从 JSON 完整恢复旧数据。文件名带时间戳防同表多次迁移覆盖（精确到秒）。
    备份失败不阻断迁移（内存 rows 仍可回灌），仅 log warning 暴露——这是有意的容错：
    迁移推进比备份完美更重要，sidecar 是双保险而非主路径。
    """
    try:
        backup_dir = Path(_MIGRATION_BACKUP_DIR)
        backup_dir.mkdir(parents=True, exist_ok=True)
        ts = clock.now().strftime("%Y%m%dT%H%M%S")
        backup_path = backup_dir / f"state_store_{table}_backup_{ts}.json"
        backup_path.write_text(
            json.dumps(rows, ensure_ascii=False, default=str), encoding="utf-8")
        logger.info("state_store 迁移：%s 表 %d 行已备份到 %s", table, len(rows), backup_path)
    except Exception:
        # 备份失败不阻断迁移（内存 rows 仍可回灌），但 log warning 暴露
        logger.warning("state_store 迁移：%s 表 sidecar 备份失败（内存回灌仍进行）",
                       table, exc_info=True)


def _migrate_with_backup(
    con, table: str, new_ddl: str, *,
    defaults: dict | None = None,
    column_copies: dict | None = None,
    backfill: bool = True,
) -> int:
    """SQLite 表备份式重建（导出 → DROP → CREATE → 回灌，治「连错库即清零」）。

    三段式安全重建（DROP 前必须先 SELECT 导出——这是与原 ``con.execute("DROP TABLE")``
    的本质差异）：
      ① SELECT * 旧表 → 内存缓存行 + 写 sidecar JSON（双保险，连错库可恢复）；
      ② DROP TABLE + 执行 new_ddl（新 schema，CREATE 语义）；
      ③ 回灌共享列（旧∩新列名）+ 新表独有列按 ``column_copies`` 行级拷贝 / ``defaults``
         兜底；其余 NULL。``backfill=False`` 时跳过回灌（旧行语义无法融入新 schema，仅备份）。

    Args:
        con: sqlite3 连接（_connect 上下文，事务由其 commit/rollback）。
        table: 表名（如 "fill" / "position"）。
        new_ddl: 完整 CREATE TABLE 语句（DROP 后执行；含 IF NOT EXISTS 语义冗余但无害，
                 因 DROP 已确保表不存在）。
        defaults: 新表独有列的回灌「静态」默认值（如 ``{"account_id": "default"}``）——
                  全部行取同一个值。回灌时新表独有列若无默认，取 NULL；若该列 NOT NULL
                  则撞约束 → 该行跳过（保数据不丢即可，sidecar 已存可恢复）。
        column_copies: 新表独有列的「行级按列拷贝」映射（如
                  ``{"traded_time": "applied_at"}``）——每行各自取映射源列的旧值，适合
                  新列语义可由某旧列近似的场景。优先级：旧列直匹配 > column_copies >
                  defaults > NULL。典型：fill 表迁移 traded_time NOT NULL 但旧表无此列，
                  用 applied_at 作成交时间近似回灌（order_id 已 UNIQUE 保证
                  (order_id, traded_time) 不撞）。
        backfill: True 回灌共享列（旧行语义可融入新 schema）；False 仅备份不回灌
                  （旧行无法融入新 schema，仅留 sidecar）。

    Returns:
        回灌行数（backfill=False 时为 0；备份始终执行）。

    红线（绝不静默丢数据）：
        - 备份失败（磁盘满/权限）只 log warning 不抛——内存 rows 仍可回灌，迁移继续；
        - 回灌撞约束（NOT NULL/UNIQUE/FK）跳过该行 + log warning，不阻断整批；
        - DROP 前必须先 SELECT（导出），这是与原 ``con.execute("DROP TABLE")`` 的本质差异。
    """
    defaults = defaults or {}
    column_copies = column_copies or {}
    # ① 导出旧行到内存 + sidecar JSON（DROP 前必须先读，DROP 后无源可读）
    rows = [dict(r) for r in con.execute(f"SELECT * FROM {table}").fetchall()]
    if rows:
        _dump_migration_sidecar(table, rows)
    # ② DROP + CREATE 新 schema
    con.execute(f"DROP TABLE {table}")
    con.execute(new_ddl)
    if not rows or not backfill:
        return 0
    # ③ 回灌共享列（旧∩新列名）+ 新表独有列取 defaults 兜底
    new_cols = [r[1] for r in con.execute(f"PRAGMA table_info({table})").fetchall()]
    old_cols = set(rows[0].keys())
    col_list = ", ".join(f'"{c}"' for c in new_cols)
    placeholders = ", ".join("?" * len(new_cols))
    inserted = 0
    for row in rows:
        vals = []
        for col in new_cols:
            if col in old_cols:
                vals.append(row[col])
            elif col in column_copies and column_copies[col] in old_cols:
                # 行级按列拷贝：新列取映射源列的旧值（如 traded_time ← applied_at，
                # 每行各自的成交落库时刻作 traded_time 近似）
                vals.append(row[column_copies[col]])
            elif col in defaults:
                vals.append(defaults[col])
            else:
                vals.append(None)
        try:
            con.execute(
                f'INSERT INTO {table}({col_list}) VALUES({placeholders})',
                vals,
            )
            inserted += 1
        except sqlite3.IntegrityError:
            # 回灌撞约束（NOT NULL/UNIQUE/FK）跳过——旧行语义已损坏，备份已存可恢复
            logger.warning("state_store 迁移：%s 表回灌撞约束跳过该行：%s", table, row)
    if inserted:
        logger.info("state_store 迁移：%s 表回灌 %d/%d 行（共享列 + defaults 兜底）",
                    table, inserted, len(rows))
    return inserted


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
            # 旧 fill 无 traded_time（position_book 早期 schema）→ 备份式重建 + 回灌
            # G5 schema 迁移安全：原 DROP 直接丢数据（spec §7.1「live-前-无-数据」假设在
            # 连错库/历史回灌场景下被破坏即清零成交真相源）；改 _migrate_with_backup 三段式
            # （导出 → DROP → CREATE → 回灌）。
            # 回灌（backfill 默认 True）+ column_copies={"traded_time": "applied_at"}：
            #   旧 fill 表 schema（order_id TEXT NOT NULL, symbol, direction, qty, price,
            #   applied_at, UNIQUE(order_id)——见 position_book 早期建表 + test_position_book.py
            #   test_fill_schema_migration 历史范式）只缺 traded_time 一列。回灌时令
            #   traded_time = applied_at 作成交时间近似（applied_at 是成交回报落库时刻，
            #   与 traded_time 同语义近值，是最优近似）：
            #     - traded_time NOT NULL：applied_at 旧值非空即满足；
            #     - UNIQUE(order_id, traded_time)：旧表 order_id 已 UNIQUE ⇒ (order_id,
            #       traded_time) 必然唯一 ⇒ 回灌永不撞约束（reviewer 实测 2/2 零冲突）。
            #   fill 是成交真相源，应保 DB 而非仅 sidecar——回灌让旧成交流水在新表存活。
            #   兜底退化：若极旧 fill 行缺 applied_at，traded_time 取 NULL → 撞 NOT NULL
            #   → 该行跳过 + warning（sidecar 已备份，数据不丢）。
            logger.info("state_store 迁移：旧 fill 表无 traded_time，备份式重建 + 回灌（traded_time=applied_at 近似）")
            _migrate_with_backup(
                con, "fill",
                """CREATE TABLE fill (
                    fill_id      INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id     TEXT NOT NULL,
                    traded_time  TEXT NOT NULL,
                    symbol       TEXT NOT NULL,
                    direction    TEXT NOT NULL CHECK(direction IN ('BUY','SELL')),
                    qty          REAL NOT NULL,
                    price        REAL NOT NULL,
                    applied_at   TEXT NOT NULL,
                    UNIQUE(order_id, traded_time)
                )""",
                column_copies={"traded_time": "applied_at"},
            )
        con.execute("""
            CREATE TABLE IF NOT EXISTS fill (
                fill_id      INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id     TEXT NOT NULL,
                traded_time  TEXT NOT NULL,
                symbol       TEXT NOT NULL,
                direction    TEXT NOT NULL CHECK(direction IN ('BUY','SELL')),
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
        # fill 加 strategy 列（SSoT Phase A · Task A1：digest 过滤口径的真相源字段）。
        # 物理意图：digest/filter 消费端按 strategy 过滤成交流水（如「只看颈线法」），
        # 若 strategy 散在 CSV 而 fill 表无对应字段，真相源（fill）与消费诉求脱钩——
        # 重放/补推场景下 CSV 重复但 fill 去重会导致 strategy 漏过滤。A1 把 strategy
        # 持久化到 fill 表，让真相源本身带 strategy 维度，消费端无需旁路 CSV。
        # 默认 NULL：向后兼容既有 insert_fill 调用（A1 后续 task 才迁 engine 老调用方）。
        if not _has_column(con, "fill", "strategy"):
            con.execute("ALTER TABLE fill ADD COLUMN strategy TEXT")
            logger.info("state_store 迁移：fill 表加 strategy 列（A1 断点-4 真相源字段）")
        # CR-5（2026-08-15 tech-debt）：fill.direction CHECK 约束迁移——「防超卖＞防漏挂」
        # 盲区的 schema 层收口。Why：direction 脏值（如小写 'buy'）会让 audit 的
        # ``direction == "BUY"`` 净额符号判定失真（小写被当 -qty 记成卖出）→ 漏挂向
        # 静默 PASS；应用层校验（apply_fill_to_position/insert_fill）只是约定，DB 无
        # CHECK 则旁路写入（运维脚本/迁移回灌）无兜底。SQLite 给既有列加 CHECK 必须
        # 重建表 → 复用 G5 _migrate_with_backup（导出→DROP→CREATE→回灌，撞约束跳行
        # + sidecar 兜底）。幂等触发条件：现表 direction 列无 CHECK（只读 sqlite_master
        # DDL 文本检测，见 _fill_direction_check_present）。
        # 迁移目标 DDL 特意带上 account_id/strategy（当前生产形态的 ALTER 追加列）：
        # 共享列回灌保住既有归因数据（digest/归因消费口真相源字段），回灌后上方两个
        # _has_column ALTER 守卫自然跳过，表形态与生产一致。历史脏 direction 行会在
        # 回灌时撞 CHECK 跳行 + warning（设计行为：脏值不进新表，sidecar 留档人工订正）。
        # 迁移前置实证（2026-08-15 只读 mode=ro）：生产 logs/trading_state.db fill 表
        # 4 行 direction 全部 'BUY'（大写）→ 迁移 4/4 回灌零跳行。
        if (_table_exists(con, "fill") and _has_column(con, "fill", "direction")
                and not _fill_direction_check_present(con)):
            logger.info("state_store 迁移：fill.direction 无 CHECK 约束，备份式重建 + 回灌（CR-5）")
            _migrate_with_backup(
                con, "fill",
                """CREATE TABLE fill (
                    fill_id      INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id     TEXT NOT NULL,
                    traded_time  TEXT NOT NULL,
                    symbol       TEXT NOT NULL,
                    direction    TEXT NOT NULL CHECK(direction IN ('BUY','SELL')),
                    qty          REAL NOT NULL,
                    price        REAL NOT NULL,
                    applied_at   TEXT NOT NULL,
                    account_id   TEXT REFERENCES account(account_id),
                    strategy     TEXT,
                    UNIQUE(order_id, traded_time)
                )""",
            )
        con.execute("CREATE INDEX IF NOT EXISTS idx_fill_symbol ON fill(symbol)")
        # ⑤ position（当前持仓 · fill 累加汇总，可变）——升级为复合 PK
        if _table_exists(con, "position") and (
            not _has_column(con, "position", "account_id")
            or not _has_column(con, "position", "avg_price")
        ):
            # G5 schema 迁移安全：原 DROP 直接丢持仓数据（敞口真相失真红线）；改备份式重建
            # （导出 → DROP → CREATE → 回灌共享列）。回灌时 account_id 用 _DEFAULT_ACCOUNT_ID
            # 兜底（NOT NULL 约束 + 单账户默认口径，spec §7.1 live-前-无-数据 假设兼容）。
            # FK 引用 account(account_id)：回灌前先 INSERT OR IGNORE 默认 account 行，
            # 防 FK RESTRICT 挡住回灌（account 表是 init_store 第一步建的，此处必已存在）。
            logger.info("state_store 迁移：旧 position 表无 account_id/avg_price，备份式重建（回灌共享列）")
            con.execute(
                "INSERT OR IGNORE INTO account(account_id, broker, created_at) VALUES(?, 'unknown', ?)",
                (_DEFAULT_ACCOUNT_ID, clock.now().isoformat()),
            )
            _migrate_with_backup(
                con, "position",
                """CREATE TABLE position (
                    account_id  TEXT NOT NULL REFERENCES account(account_id) ON DELETE RESTRICT,
                    symbol      TEXT NOT NULL,
                    qty         REAL NOT NULL,
                    avg_price   REAL,
                    entry_date  TEXT,
                    updated_at  TEXT NOT NULL,
                    PRIMARY KEY (account_id, symbol)
                )""",
                defaults={"account_id": _DEFAULT_ACCOUNT_ID},
            )
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
        # SSoT Phase B · B2：position 加 strategy/entry_rationale 列（持仓归因落 DB 真相源）。
        # 物理意图：原持仓归因散在 trading_service._position_attribution 内存字典，进程
        # 重启即丢，且与持仓真相源（position 表）分立两处——对账无法回答「这只票是哪个
        # 策略建的仓」。B2 把归因与 qty/avg_price 同行持久化，重启后归因随持仓行存活。
        # 断点-3：B2 只做「落列 + upsert/clear」，不做重启重建（C1 从 SIGNAL.meta 补 rebuild）。
        # 向後兼容：旧库 position 表无此列，ALTER ADD COLUMN 补（参考 fill.account_id 范式，
        # 不能 DROP 重建——DROP 会丢既有持仓数据 = 敞口真相失真红线）。默认 NULL。
        if not _has_column(con, "position", "strategy"):
            con.execute("ALTER TABLE position ADD COLUMN strategy TEXT")
            logger.info("state_store 迁移：position 表加 strategy 列（B2 持仓归因）")
        if not _has_column(con, "position", "entry_rationale"):
            con.execute("ALTER TABLE position ADD COLUMN entry_rationale TEXT")
            logger.info("state_store 迁移：position 表加 entry_rationale 列（B2 持仓归因）")
        # ⑥ account_daily（账户日级快照 · pre_open 写 start / post_close 写 close + 熔断读 start）
        # ——全新表，W4 + C-1 后是【权益快照与熔断基线】的唯一真相源：pre_open 写 start_total_asset，
        # post_close 写 close_total_asset + 算 daily_pnl，并【读 start_total_asset 作 -3% 熔断基线】。
        #
        # daily_equity 表状态（B5 已收口）：C-1 把熔断读口迁到 account_daily.start，
        # W4 把 pre_open 写口迁到 account_daily.start → daily_equity 表已无生产写入方/读口。
        # B5（SSoT Phase B）已删 position_book.snapshot_start_equity / get_start_equity
        # 函数 + init_db 不再 CREATE daily_equity。**旧库残留 daily_equity 表无害**：
        # init_store/init_db 都不再 CREATE，旧表存在=历史残留，不读写，account_daily 已完全替代。
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


# ============================= M2：actual_sid 单 SSoT 读/写口 =============================
# 物理意图（tech-debt master design「actual_sid 单 SSoT」· 2026-08-15）：实际 sid 的
# 唯一真相源 = DB ``account.session_id``；``logs/engine_session.json`` 降级为运行态快照
# （broker 轮换时顺手写，供人眼看，消费端一律不走它）。两个写口保 DB 新鲜：
#   ① engine bootstrap L3 回写（trading/engine.py `_bootstrap`）；
#   ② L2 轮换成功点（broker/qmt_connection._write_runtime_session）。
# 读口（本函数）是 supervisor / ops 端点取 actual_sid 的唯一通道——单口收编消
# 「读 JSON 与写 JSON 双源漂移不可观测」的旧病（json 只在轮换时写、非轮换连接不刷新）。
def get_session_id(account_id: str, *, db_path: str | None = None) -> int | None:
    """M2 单 SSoT 读口：账户实际 sid（DB account.session_id 真相源）。

    诚实语义：账户行不存在 / session_id 列 NULL → None（观测端显示空，不猜值）。
    健壮性：DB 文件不存在时直接返 None 且**不创建空库文件**——本函数是只读探测
    （supervisor 在未 init 的环境/异 CWD 下也会调），sqlite3.connect 默认会顺手
    建空文件，必须前置 is_file 守卫防「探测留垃圾库」。
    """
    db_path = db_path or _DEFAULT_DB
    if not Path(db_path).is_file():
        return None
    with _connect(db_path) as con:
        row = con.execute(
            "SELECT session_id FROM account WHERE account_id=?", (account_id,)
        ).fetchone()
    if row is None or row["session_id"] is None:
        return None
    return int(row["session_id"])


def set_session_id(account_id: str, session_id: int, *, db_path: str | None = None) -> int:
    """M2 单 SSoT 写口：列级精准 UPDATE account.session_id（返受影响行数）。

    Why 不复用 upsert_account：UPSERT 的 ON CONFLICT 是**全列** UPDATE——只传
    session_id 时其余列落到默认值（mode→'dry_run'、userdata_path→NULL），等于把
    bootstrap 期刚从 .env 迁好的配置整行抹掉。M2 写口只动 session_id 一列。
    行不存在时 no-op 返 0（账户行由 _migrate_env_to_account / upsert_account 负责
    造，sid 写口不静默造行——防测试/旁路把幽灵账户写进真相源）。
    健壮性（终审 Minor · 2026-08-16）：DB 文件不存在时前置 is_file 守卫直接返 0
    且**不创建空库文件**——与 get_session_id 同款守卫。写口本就要求「账户行已存在」
    （UPDATE 匹配 0 行返 0），库文件都不存在时账户行必然不存在，绝不能让
    sqlite3.connect 顺手建空垃圾库（broker 轮换/测试在未 init 或异 CWD 环境下
    调用时的「探测留垃圾库」同源坑）。
    """
    db_path = db_path or _DEFAULT_DB
    if not Path(db_path).is_file():
        return 0
    with _connect(db_path) as con:
        cur = con.execute(
            "UPDATE account SET session_id=? WHERE account_id=?",
            (int(session_id), account_id))
        return cur.rowcount


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

def build_trade_id(account_id: str, symbol: str, date: str) -> str:
    """构造 trade_id（account_symbol_date）单点。

    物理意图：消 engine.py:636（eod_plan）/ engine.py:3126（_handle_order_update）/
    原 trading_plan 确认函数 三处 `f"{account_id}_{symbol}_{date}"` 复制——
    单点既保幂等键口径统一（trade_event UNIQUE 约束依赖），又便于后续 task 单测。
    date 语义：【计划生效日】（next_trading_day，非写入日）—— 同一交易日从 SIGNAL
    到 FILL 共用同一 trade_id 串起事件流，错用写入日会切断归因链。
    """
    return f"{account_id}_{symbol}_{date}"


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

    G7 FSM 写校验（软告警 · 2026-08-13 g-wave-p0-guards）：
        写入前读 old_state，过 OrderStateMachine._is_valid_transition 校验。非法迁移告警
        （logger.warning 含 old/new/order_id）+ **仍写入**（不拒写），幂等回推（old==new）
        静默。Why 软告警而非硬拒：FSM 迁移表归 architecture/05 红线不可改（order_state.py
        顶部注释明示），生产 pre_open 存在 PENDING→REJECTED 等表外业务跳变、现有测试存在
        终态循环（test_has_order_filters_dead_states）——硬拒会破生产 + 测试；白名单方案需
        改 FSM 表 → 越红线。软告警达成「消监控盲区」顶层目标（非法迁移不再静默），硬拒写
        延后至 FSM 表重构 Task。详见 _fsm_check_on_write。

    按 order_id 主键更新（调用方持有内部 order_id 时用，如 pre_open submit 后回填）。
    柜台查询撤单路径持有 broker_oid（非主键）→ 用 cancel_order_by_broker_oid_db。
    """
    db_path = db_path or _DEFAULT_DB
    with _connect(db_path) as con:
        # G7 FSM 写校验（软告警）：非法迁移告警 + 仍写入（不拒写），幂等回推静默。
        _fsm_check_on_write(con, order_id, state)
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


# DB state 列字符串 → OrderState 枚举映射（order_state_to_db 的逆）。
# DB 约定：OrderState.PARTIAL_FILLED 存短形式 "PARTIAL"，其余长形式（见 order_state_to_db）。
_DB_STATE_TO_ENUM = None  # lazy 构造（见 _fsm_check_on_write，避免模块级 import 循环）


def _fsm_check_on_write(con, order_id: str, new_state: str) -> None:
    """G7 FSM 写校验（软告警）：读 old_state，过 OrderStateMachine 校验，非法则告警。

    物理意图（消「非法迁移静默落库」盲区）：
        原 update_order_state 裸 UPDATE 不过 FSM，FILLED→PENDING 倒退等非法迁移静默落库，
        状态机约束形同虚设。本 helper 在写入前读 old_state，过 _is_valid_transition，
        非法则告警让人看见（消盲区）。

    软告警契约（不拒写）：
        - 非法迁移 → logger.warning（含 old/new/order_id）+ **仍写入**（控制流不变）。
        - 幂等回推（old==new）→ 静默（合法 no-op，broker 重推同终态）。
        - 行不存在 / 未知状态字符串 → 不校验（避免误报；行不存在是 insert 路径）。
        - 校验自身异常 → debug log，不阻断写入（可观测降级，写路径不能因观测宕机）。

    Why 软告警（不硬拒）见 update_order_state docstring。lazy import OrderStateMachine
    防 state_store↔order_state 循环（order_state.py 顶部 import state_store）。
    """
    global _DB_STATE_TO_ENUM
    try:
        from trading.order_state import OrderStateMachine  # lazy 防 state_store↔order_state 循环
        from trading.types.order_state import OrderState
    except Exception:
        return  # FSM 模块不可用（极端环境）→ 不校验，写入优先于观测

    if _DB_STATE_TO_ENUM is None:
        _DB_STATE_TO_ENUM = {
            "PENDING": OrderState.PENDING, "SUBMITTED": OrderState.SUBMITTED,
            "PARTIAL": OrderState.PARTIAL_FILLED,  # DB 短形式 → 枚举长形式
            "FILLED": OrderState.FILLED, "CANCELLED": OrderState.CANCELLED,
            "PARTIAL_CANCELLED": OrderState.PARTIAL_CANCELLED,
            "REJECTED": OrderState.REJECTED, "FAILED": OrderState.FAILED,
        }
    try:
        row = con.execute('SELECT state FROM "order" WHERE order_id=?', (order_id,)).fetchone()
        if row is None:
            return  # 行不存在（insert 路径/测试 fixture 直接插），不校验
        old_state = row["state"]
        if old_state == new_state:
            return  # 幂等回推（合法 no-op），静默不告警（broker 重推同终态）
        old_enum = _DB_STATE_TO_ENUM.get(old_state)
        new_enum = _DB_STATE_TO_ENUM.get(new_state)
        if old_enum is None or new_enum is None:
            return  # 未知状态字符串（未来扩展/自定义），不校验避免误报
        fsm = OrderStateMachine()
        if not fsm._is_valid_transition(old_enum, new_enum):
            # 软告警：非法迁移告警让人看见（消盲区），仍写入（不拒写，不破生产/测试）。
            logger.warning(
                "订单状态迁移偏离 FSM 表 order_id=%s %s→%s（软告警：仍写入；可能为 broker "
                "异步回报边角跳变或业务回填，硬拒需 FSM 表重构 architecture/05 红线）",
                order_id, old_state, new_state)
    except Exception:
        # 校验自身异常不阻断写入（可观测降级，不致写路径宕机）
        logger.debug("FSM 写校验异常 order_id=%s state=%s", order_id, new_state, exc_info=True)


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
        # G7 终态集单源：死态排除集引用 _DEAD_ORDER_STATES（与 has_order 同源），消漂移。
        row = con.execute(
            "SELECT COALESCE(SUM(qty), 0) AS total FROM \"order\" "
            "WHERE account_id=? AND trade_date=? AND symbol=? AND purpose=? "
            "AND state NOT IN ({})".format(_DEAD_STATE_PLACEHOLDERS),
            (account_id, trade_date, symbol, purpose, *_DEAD_ORDER_STATES)).fetchone()
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
                strategy: str | None = None,
                db_path: str | None = None) -> bool:
    """写 fill（成交流水，append-only，幂等）。

    幂等（spec §2.4）：UNIQUE(order_id, traded_time) —— 同笔成交（同 order_id + 同 traded_time）
    重推 IntegrityError → 返 False（不重复记）。部分成交（同 order_id 不同 traded_time）各自一行，
    累加到 position（apply_fill_to_position）。

    strategy（A1 新增，默认 None 保向后兼容）：成交流水的策略归属（如 "neckline"），
    落 fill.strategy 列——digest/filter 消费端按 strategy 过滤的真相源字段。Why 必须落
    fill 而非仅 CSV：CSV 在重放/补推下重复，fill 表去重才是真相源，strategy 必须与真相
    源同字段才能在重放场景下正确过滤。

    Returns: True=首次写入；False=重复 (order_id, traded_time) 跳过。
    """
    # CR-5：direction 入口校验（先于 DB CHECK——脏值若只靠 DB CHECK 拦，会走下方
    # IntegrityError→返 False 分支，被调用方误当「重复成交」静默吞掉（审计断链）；
    # 入口 ValueError 快速失败，与 apply_fill_to_position 同款先例）。
    if direction not in ("BUY", "SELL"):
        raise ValueError(f"insert_fill direction 仅 BUY/SELL，收到 {direction}")
    db_path = db_path or _DEFAULT_DB
    now = clock.now().isoformat()
    with _connect(db_path) as con:
        try:
            con.execute(
                "INSERT INTO fill(order_id, traded_time, symbol, direction, qty, price,"
                " applied_at, account_id, strategy) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (order_id, traded_time, symbol, direction, float(qty), float(price), now,
                 account_id, strategy))
            return True
        except sqlite3.IntegrityError:
            # 同 (order_id, traded_time) 成交回报重推幂等跳过。
            logger.info("insert_fill 跳过重复 order_id=%s traded_time=%s", order_id, traded_time)
            return False


def query_fills(start: str, end: str, *, symbol: str | None = None,
                direction: str | None = None, db_path: str | None = None) -> list[dict]:
    """查 [start, end]（YYYY-MM-DD）闭区间内 fill 表成交流水（W3.2 消费端真相源读口）。

    物理意图（08-04 事故修复）：
        消费端（query_trades/简报/导出/复盘）必须读 fill 表而非 CSV 镜像。CSV 在
        重放/补推场景下会重复（同一笔成交被 record_live_trade 多次 append），而 fill
        表 UNIQUE(order_id, traded_time) 天然去重，是成交流水的真相源（spec §2.4）。
        T6 已让 insert_fill 幂等，本函数是消费端切真相源的纯查询读口。

    traded_time 口径（与 insert_fill 一致）：
        YYYYMMDDHHMMSS 整数串（如 "20260805101000"），按日期前缀（前 8 位）与
        [start, end] 闭区间字典序比较（与 query_trades 的 timestamp 日期前缀比较同口径）。

    返回结构（对齐 query_trades 消费契约，direction 规范化为小写）：
        [{order_id, traded_time, symbol, direction(小写), shares(float), price(float),
          account_id, strategy}, ...] 按 traded_time 升序（与 CSV 时间序一致，简报明细
          保持原序）。

    strategy 字段（A3 补 SELECT · A1 只加了 INSERT/列，SELECT 漏改）：成交流水的策略归属，
    digest 消费端按 strategy 过滤（新断点-4，保原 CSV 口径）；老 fill 行（A1 迁移前）此列
    可能为 NULL，消费端用 `(r.get("strategy") or "").strip()` 防御性兜底。
    """
    db_path = db_path or _DEFAULT_DB
    with _connect(db_path) as con:
        # substr(traded_time,1,8) 取 YYYYMMDD 日期前缀，与 [start,end] 闭区间字典序比较；
        # start/end 入参为 YYYY-MM-DD，去掉 "-" 后比较（与 fill 表 traded_time 同口径）。
        sql = ("SELECT order_id, traded_time, symbol, direction, qty, price, account_id,"
               " strategy FROM fill WHERE substr(traded_time, 1, 8) BETWEEN ? AND ?")
        params: list = [start.replace("-", ""), end.replace("-", "")]
        if symbol:
            sql += " AND symbol = ?"
            params.append(symbol)
        if direction:
            # DB 存大写（BUY/SELL，与 insert_fill 入参一致），调用方传小写亦能命中
            sql += " AND direction = ?"
            params.append(direction.upper())
        rows = con.execute(sql + " ORDER BY traded_time", params).fetchall()
    # 返 dict 列表，direction 统一小写口径（与 query_trades 的 CSV 读口一致，
    # 前端/简报消费者一律拿小写，避免 BUY/SELL 大写导致前端着色颠倒）
    return [{
        "order_id": r["order_id"],
        "traded_time": r["traded_time"],
        "symbol": r["symbol"],
        "direction": (r["direction"] or "").lower(),
        "shares": float(r["qty"]),
        "price": float(r["price"]),
        "account_id": r["account_id"],
        "strategy": r["strategy"],
    } for r in rows]


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
    """读单标的当前持仓 {qty, avg_price, entry_date, strategy, entry_rationale}。不存在返 None。

    B2 后追加 strategy/entry_rationale（持仓归因，与 qty/avg_price 同行）。
    """
    db_path = db_path or _DEFAULT_DB
    with _connect(db_path) as con:
        row = con.execute(
            "SELECT qty, avg_price, entry_date, strategy, entry_rationale"
            " FROM position WHERE account_id=? AND symbol=?",
            (account_id, symbol)
        ).fetchone()
    return dict(row) if row else None


def upsert_position_attribution(account_id: str, symbol: str, strategy: str,
                                entry_rationale: str = "", *, db_path: str | None = None) -> None:
    """写/覆盖持仓归因（strategy + entry_rationale），持仓行须已存在（由 apply_fill_to_position 建）。

    物理意图（spec §5 B2）：
        BUY 成交 → apply_fill_to_position 建 position 行（qty/avg_price）→ 本函数补写归因。
        分两步而非合并到 apply_fill：apply_fill 是「账本累加」语义（每次成交都调），归因是
        「建仓元数据」语义（首次建仓写一次）——职责分离避免重复覆盖。
    幂等：UPDATE 同 (account_id, symbol) 多次调用覆盖写（最后一次生效）。
    持仓行不存在（SELL 平仓删行后/未建仓）：UPDATE 命中 0 行，无副作用（不报错——
        engine BUY 路径 apply_fill_to_position 必先建行，正常不会触此分支）。
    """
    db_path = db_path or _DEFAULT_DB
    with _connect(db_path) as con:
        con.execute(
            "UPDATE position SET strategy=?, entry_rationale=?"
            " WHERE account_id=? AND symbol=?",
            (strategy, entry_rationale, account_id, symbol))


def clear_position_attribution(account_id: str, symbol: str, *, db_path: str | None = None) -> None:
    """清空持仓归因（strategy/entry_rationale 置 NULL），持仓行保留。

    物理意图：与 upsert 对称的清除语义。B2 实际生产中**不调用本函数**——SELL 平仓
    apply_fill_to_position 归零删 position 行（state_store.py DELETE WHERE qty=0），
    归因随行消失，clear 会 UPDATE 0 行（空操作）。本函数保留供显式清除场景（如
    人审发现归因错标需擦除，但持仓仍留）+ 测试断言 upsert/clear 往返幂等。
    """
    db_path = db_path or _DEFAULT_DB
    with _connect(db_path) as con:
        con.execute(
            "UPDATE position SET strategy=NULL, entry_rationale=NULL"
            " WHERE account_id=? AND symbol=?",
            (account_id, symbol))


def rebuild_position_attribution(account_id: str, *, db_path: str | None = None) -> int:
    """从 trade_event(SIGNAL).meta 回填 position.strategy/entry_rationale（C1 弥补 B2 重启窗口）。

    物理意图（spec §5 断点-3 弥补 · C1）：
        B2 把归因落到 position 表（strategy/entry_rationale 列），但只做「落列 + upsert/clear」，
        不做重启重建。重启丢失窗口内：BUY 成交 apply_fill_to_position 建 position 行（strategy IS NULL）
        + B2 归因未及写就崩，position 行裸奔无归因。本函数在 engine lifespan 启动期补扫：
        反查 trade_event(SIGNAL).meta 真实 strategy_name/rationale 回填。

    红线（IS NULL 守卫，绝不覆盖 B2 已写）：
        SQL UPDATE WHERE strategy IS NULL OR strategy='' —— 已写归因（B2 算法/人工 upsert）
        的行不被覆盖。验收 test_rebuild_skips_already_attributed：已写 "manual" 的行 rebuild
        后仍是 "manual"，不被 SIGNAL.meta 的 "neckline" 改写。

    读真实 strategy_name（非默认）：
        meta.get("strategy_name") or "neckline" —— 读 SIGNAL.meta 真实字段（C1 eod_plan 补），
        多策略扩展的物理基础（未来多策略并存时归因随真实值）。无 strategy_name 字段兜底单策略
        "neckline"（向后兼容老 SIGNAL.meta）。验收 test_rebuild_reads_alternative_strategy_name：
        meta strategy_name='momentum' → 回填 'momentum'（非写死 neckline）。

    无 SIGNAL.meta 处理：
        position 行可能由历史迁移/手动建仓产生（无 SIGNAL 事件）→ 静默跳过（continue），
        不 raise 中断整批回填。验收 test_rebuild_skips_position_without_signal：回填 0 行。

    Args:
        account_id: 账户 ID（与 position.account_id / trade_event.account_id 同口径）。
        db_path: DB 路径（测试注入；缺省 _DEFAULT_DB）。
    Returns:
        回填行数（IS NULL 守卫下的实际 UPDATE 命中数）。
    """
    db_path = db_path or _DEFAULT_DB
    n = 0
    with _connect(db_path) as con:
        # 仅扫 strategy IS NULL 的行（红线守卫：不覆盖 B2 已写归因）
        positions = con.execute(
            "SELECT symbol FROM position"
            " WHERE account_id=? AND (strategy IS NULL OR strategy='')",
            (account_id,)).fetchall()
        for p in positions:
            # 反查最新 SIGNAL.meta（同 symbol 可能多次信号，取最新 event_id DESC LIMIT 1）
            row = con.execute(
                "SELECT meta FROM trade_event"
                " WHERE account_id=? AND symbol=? AND action='SIGNAL'"
                " ORDER BY event_id DESC LIMIT 1",
                (account_id, p["symbol"])).fetchone()
            if not row or not row["meta"]:
                continue  # 无 SIGNAL.meta 静默跳过（历史/手动建仓）
            try:
                meta = json.loads(row["meta"])
            except Exception:
                # meta 损坏（非合法 JSON）跳过不阻断（保护整批回填不被单条脏数据中断）
                continue
            # 读真实 strategy_name（非默认 neckline）；rationale 兜底 formed_at 拼
            strategy = meta.get("strategy_name") or "neckline"
            rationale = meta.get("rationale") or f"颈线法@{meta.get('formed_at', '')}"
            # UPDATE 再加 IS NULL 守卫（双保险，防并发写竞争：SELECT 到 UPDATE 之间 B2 可能已写）
            con.execute(
                "UPDATE position SET strategy=?, entry_rationale=?"
                " WHERE account_id=? AND symbol=? AND (strategy IS NULL OR strategy='')",
                (strategy, rationale, account_id, p["symbol"]))
            n += 1
    return n


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


def get_start_equity(account_id: str, date: str, *, db_path: str | None = None) -> float | None:
    """读 account_daily.start_total_asset（W4 后熔断基线统一读口，与 snapshot_start_equity 同表）。

    物理意图（C-1 收口 · 08-04 Task 9 review 发现）：post_close 熔断步骤1 需要当日开盘前
    start_equity 作为基线。W4 已把 pre_open 写口迁到 ``snapshot_start_equity``（写 account_daily
    的 start 字段），但熔断读口仍在读 ``position_book.get_start_equity``（读已无生产写入方的
    daily_equity 表）→ 恒返 None → ``breaker_skipped=True`` → **日内 -3% 熔断永久失效，实盘
    敞口失控红线**。本函数是 W4 闭合的最后一块拼图：让熔断读口回到与 pre_open 写口同表的
    account_daily，基线真正可达。

    Why 与 snapshot_start_equity 同表（account_daily）：pre_open 写 start，post_close 写 close
    并算 daily_pnl = close - start，全在同一行（account_id, date）；熔断读 start 也命中此行，
    消除「写 daily_equity / 读 account_daily」的两表断链。

    Args:
        account_id: 与 pre_open 写入同口径（engine._resolve_account_id，QMT_ACCOUNT_ID 优先，
                    缺失退默认账户）。口径必须一致，否则读不到 pre_open 写的基线。
        date:       业务日（与 pre_open/post_close 入参 date 同口径，clock.today）。

    Returns:
        start_total_asset（float）或 None（pre_open 未写基线 / 查询异常）。
        None 时调用方 post_close 走 T-1 close 兜底；兜底仍无则 None 直传 breaker 触发
        fail-closed（DG-G3：dry 停手+CRITICAL / live raise _CriticalHalt；breaker_skipped
        仅保留给 curr_equity 缺失的「无法判定」语义）。绝不拿 0 写基线——0 会致 daily_pnl
        除零 + 语义模糊（基线缺失是「该停手」，非旧 fail-open 的「永不熔断」）。
    """
    db_path = db_path or _DEFAULT_DB
    with _connect(db_path) as con:
        row = con.execute(
            "SELECT start_total_asset FROM account_daily WHERE account_id=? AND date=?",
            (account_id, date)).fetchone()
    return float(row["start_total_asset"]) if row and row["start_total_asset"] is not None else None


def get_prev_close_equity(account_id: str, date: str, *, db_path: str | None = None) -> float | None:
    """读 account_daily 上一交易日（T-1）的 close_total_asset（补基线兜底用）。

    物理意图（account_daily.start 漏采修复 · 2026-08-11）：pre_open 抓基线失败
    （query_asset 返空 / gw=None / 非盘前启动）→ start_total_asset 未写 → post_close
    熔断缺基线（breaker_skipped）。本函数读 T-1 的 close_total_asset 作 start 近似
    （隔夜无交易，T-1 收盘 ≈ T 开盘），让 post_close 熔断仍能基于近似基线工作（不裸奔）。
    详见 docs/superpowers/specs/2026-08-11-account-daily-start-baseline-design.md。

    Args:
        date: T 日（业务日，clock.today 口径，与 get_start_equity 同）。
    Returns:
        T-1 的 close_total_asset（float）或 None（T-1 行不存在 / close 未写 / 日历异常）。
    """
    db_path = db_path or _DEFAULT_DB
    # T-1 = 上一交易日（clock.pretrade_date）；读 account_daily T-1 行的 close_total_asset
    try:
        prev_date = clock.pretrade_date(date)
    except Exception:
        # 日历未就绪 / date 非法 → 无法算 T-1，返 None（调用方走 breaker_skipped）
        return None
    with _connect(db_path) as con:
        row = con.execute(
            "SELECT close_total_asset FROM account_daily WHERE account_id=? AND date=?",
            (account_id, prev_date)).fetchone()
    return float(row["close_total_asset"]) if row and row["close_total_asset"] is not None else None


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
        # C-1 final-review fix (I-2/?-1)：过滤死态委托——REJECTED/FAILED/CANCELLED/
        # PARTIAL_CANCELLED 不算「已挂」，允许重挂。否则挂单被拒（资金不足/涨跌停挡板）后
        # has_order 恒 True → 当日永久漏挂（pre_open OPEN）+ stop_loss/TP 被拒后裸奔/永不
        # 补挂（live 真金致命）。
        # G7 终态集单源：死态排除集引用 _DEAD_ORDER_STATES（与 get_order_placed_qty 同源），
        # 修复历史漏 PARTIAL_CANCELLED 的漂移 bug。
        row = con.execute(
            "SELECT 1 FROM \"order\" WHERE account_id=? AND trade_date=? AND symbol=? AND purpose=?"
            " AND state NOT IN ({})".format(_DEAD_STATE_PLACEHOLDERS),
            (account_id, trade_date, symbol, purpose, *_DEAD_ORDER_STATES)
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


# 「未终态/活态」概念单一归宿 = _ACTIVE_ORDER_STATES（get_pending_orders / has_order /
# get_order_placed_qty 三处同源）。G7 收口后不再保留 _PENDING_ORDER_STATES 别名（零消费者）。


def get_pending_orders(account_id: str, *, db_path: str | None = None) -> list[dict]:
    """未终态 order 列表：state IN (PENDING/SUBMITTED/PARTIAL)。

    物理意图：cancel_all_open_orders 查柜台前可先查 DB 未终态委托（对照），post_close 查未成交委托。
    """
    db_path = db_path or _DEFAULT_DB
    with _connect(db_path) as con:
        # G7 终态集单源：活态集引用 _ACTIVE_ORDER_STATES（单一真相源），消硬编码漂移。
        rows = con.execute(
            "SELECT * FROM \"order\" WHERE account_id=? AND state IN ({})".format(
                _ACTIVE_STATE_PLACEHOLDERS),
            (account_id, *_ACTIVE_ORDER_STATES)
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


def is_trade_confirmed(trade_id: str, *, db_path: str | None = None) -> bool:
    """某 trade_id 是否已通过研究员人审闸（confirmed gate，语义单点）。

    物理意图（ssot-review P1 fix · 与 trading_plan.load_plan:95 语义对齐）：
        pre_open 挂单后 trade_event 会写 ORDERED（event_id > CONFIRMED），导致
        ``get_latest_action(tid) == "CONFIRMED"`` 严格匹配在挂单后失效——_stoploss
        取不到 confirmed_signals、pre_open 重入判 all_confirmed=False，止损监控
        静默失效（live 红线）。

    语义统一（生命周期晚于 CONFIRMED 均视作已确认）：
        - 未确认集合 = {None, "SIGNAL", "VETOED"}（VETOED 是 veto 终局，仍视作未确认）
        - 已确认集合 = {"CONFIRMED", "ORDERED", "FILLED", "CLOSED", "TP_FILLED"}
          （ORDERED/FILLED/CLOSED/TP_FILLED 生命周期晚于 CONFIRMED，意味着 plan 已
          确认且已挂单/成交；_stoploss 必须 continues 监控这些标的，pre_open 重入
          必须把它们视作「已处理」不再阻塞补挂剩余标的）。

    单点职责（DRY）：_stoploss / pre_open / load_plan 三处确认闸统一调本函数，
        防语义漂移（避免某处改了未确认集合另一处漏改，再现 P1 静默失效）。

    Returns:
        True  = latest action ∈ 已确认集合
        False = latest action ∈ 未确认集合（None/SIGNAL-only/VETOED）
    """
    return get_latest_action(trade_id, db_path=db_path) not in (None, "SIGNAL", "VETOED")


def is_vetoed(trade_id: str, *, db_path: str | None = None) -> bool:
    """某 trade_id 是否被人审否决（VETOED 终局，语义单点 · tech-debt M2）。

    物理意图：eod_plan 自动确认闸（auto_confirmed 分支）原散落
    ``get_latest_action(trade_id) != "VETOED"`` 字面量——多一个消费方就多一处漂移面
    （某处改了终局集合另一处漏改 → veto 保护失效/误伤）。与 is_trade_confirmed 同款
    收口为单点判断。

    None 安全：无任何事件（get_latest_action 返 None）= 未否决 → False，
    与旧字面量 ``None != "VETOED"`` → True（不拦 CONFIRMED 落盘）语义一致。

    Returns:
        True  = latest action == "VETOED"（人审否决终局，veto 保护生效）
        False = 其余一切（None / SIGNAL / CONFIRMED / ORDERED / ...）
    """
    return get_latest_action(trade_id, db_path=db_path) == "VETOED"


def count_signals_by_plan_date(plan_date: str, *, db_path: str | None = None) -> int:
    """读某计划日（T+1）的 SIGNAL 数（即当日选股/扫描出多少标的）。

    物理意图（SSoT C2a）：broadcast 的 scan_count 不再读 plan_*.json，改读 DB。
    **致命日期轴**：trade_event.timestamp = clock.now() 写入时间 = T 日盘后（非计划日 T+1），
    计划日仅在 trade_id 后缀（``{account_id}_{symbol}_{plan_date}``，build_trade_id 单点）。
    按 timestamp 查计划日恒 0（T 日写，查 T+1 永远漏），故必须按 trade_id 后缀查。

    数学验证（substr(trade_id,-10)=plan_date）：
    - trade_id = "ACC1_600000.SH_2026-08-05"（A 股 ts_code 不含下划线，YYYY-MM-DD 恰 10 字符）
    - substr(trade_id,-10) = "2026-08-05" = plan_date ✓
    - 用 substr(trade_id,-10)=? 而非 LIKE '%_2026-08-05'——``_`` 是 LIKE 通配符，
      LIKE 会误匹配（如 "...SH_2026X08-05"），substr 精确。

    去重口径：COUNT(DISTINCT symbol)（同 symbol 同 plan_date 多 SIGNAL 理论上 UNIQUE 约束防不住
    多 account，生产只一个 account 不会出现；DISTINCT 保守取「当日选股数」语义对齐，无害）。
    无 SIGNAL 行返 0（不是 None，调用方判 None=降级 / 0=当日未扫描）。
    """
    db_path = db_path or _DEFAULT_DB
    with _connect(db_path) as con:
        row = con.execute(
            "SELECT COUNT(*) FROM (SELECT DISTINCT symbol FROM trade_event "
            "WHERE action='SIGNAL' AND substr(trade_id, -10) = ?)",
            (plan_date,)
        ).fetchone()
    return int(row[0]) if row else 0


def list_signals_with_meta_by_plan_date(plan_date: str, *,
                                        db_path: str | None = None) -> list[dict]:
    """读某计划日（T+1）全部 SIGNAL 行的 meta 列表（C2c 真相源）。

    物理意图（SSoT Phase C · C2c）：pre_open / _stoploss / review_report / review_service
    不再读 plan_*.json 的 orders 集合，改直接读 trade_event(SIGNAL).meta 拿「精确 per-symbol
    计划参数」（stop_price / take_profit / neckline / atr / formed_at / max_wait /
    cancel_on / order / tp1 / experiment_id 等）。

    **致命日期轴（红线，与 count_signals_by_plan_date 同口径）**：
        trade_event.timestamp = T 日盘后写入时间（非计划日 T+1），按 timestamp 查计划日
        恒空（T 日写、查 T+1 永远漏）。计划日仅在 trade_id 后缀（build_trade_id 单点
        ``{account_id}_{symbol}_{plan_date}``，YYYY-MM-DD 恰 10 字符），故必须按
        ``substr(trade_id, -10) = plan_date`` 查。

    数学验证（substr(trade_id,-10)=plan_date）：
        - trade_id = "ACC1_600000.SH_2026-08-05" → substr(trade_id,-10)="2026-08-05" ✓
        - 用 substr 而非 LIKE '%_plan_date'——``_`` 是 LIKE 通配符会误匹配，substr 精确。

    返 shape：``list[dict]``，每项 ``{symbol: str, **meta_dict}``（meta JSON 解析后展开 +
    symbol 字段）。meta 解析失败/为 None 的行跳过（保守，不喂脏数据给消费方）。返空列表
    表示当日无 SIGNAL（调用方 pre_open 视作「无计划」返 ``{"submitted":0,"reason":"无计划"}``）。

    去重：同 (account, trade_id, action) UNIQUE 约束 + build_trade_id 单点保证同一 symbol
    同一计划日只有一行 SIGNAL（多 account 生产不出现）。ORDER BY event_id ASC 取最早一条
    （SIGNAL 是事件流起点，最早一条即「首次落盘的计划参数快照」，避免后续重跑 eod_plan
    写入更新 meta 时取到新值与 confirm 时序错位）。

    Args:
        plan_date: 计划日（YYYY-MM-DD，与 trade_id 后缀同口径 = T+1 计划生效日）。
    Returns:
        meta dict 列表（每项含 symbol + meta 全部字段）；无 SIGNAL 返 []。
    """
    db_path = db_path or _DEFAULT_DB
    out: list[dict] = []
    with _connect(db_path) as con:
        rows = con.execute(
            "SELECT symbol, meta FROM trade_event "
            "WHERE action='SIGNAL' AND substr(trade_id, -10) = ? "
            "ORDER BY event_id ASC",
            (plan_date,)
        ).fetchall()
    for r in rows:
        if r["meta"] is None:
            continue
        try:
            meta = json.loads(r["meta"])
        except (TypeError, ValueError):
            logger.warning("list_signals_with_meta_by_plan_date meta JSON 解析失败 "
                           "symbol=%s plan_date=%s", r["symbol"], plan_date)
            continue
        if not isinstance(meta, dict):
            continue
        # shape：{symbol: str, **meta_dict}——消费方读 meta.stop_price 等字段 +
        # symbol 用于 build_trade_id(account_id, symbol, plan_date)。
        out.append({"symbol": r["symbol"], **meta})
    return out


def list_signals_with_meta_by_plan_date_range(since: str | None = None,
                                              until: str | None = None, *,
                                              db_path: str | None = None) -> list[dict]:
    """读 plan_date 落在 [since, until] 区间内的全部 SIGNAL meta 列表（C2d 真相源）。

    物理意图（SSoT Phase C · C2d）：experiment report 的 ``_load_all_plans(since)`` 不再
    扫 ``plan_*.json``（文件 mtime = T 日盘后写入 ≠ 计划日 T+1，since 按 mtime 恒错），
    改读 ``trade_event(SIGNAL)`` 按 ``plan_date``（trade_id 后缀）做区间聚合。

    **致命日期轴（红线，与 list_signals_with_meta_by_plan_date 同口径）**：
        过滤按 ``plan_date``（trade_id 后缀 ``substr(trade_id,-10)``），**非文件 mtime /
        非 trade_event.timestamp**。timestamp = T 日盘后写入时间，plan_date = T+1 计划生效日，
        二者差一天——若误用 timestamp 做 since 过滤，experiment report 的 since 永远偏移一天。

    数学验证（字典序 = ISO 日期序）：
        - since="2026-07-01" / until=None → ``substr(trade_id,-10) >= '2026-07-01'``
        - since="2026-07-01" / until="2026-07-31" → BETWEEN '2026-07-01' AND '2026-07-31'
        - trade_id="ACC1_600000.SH_2026-07-15" → substr(-10)="2026-07-15" 命中区间 ✓
        - YYYY-MM-DD 10 字符字典序 == 时间序（C2d 同 list_signals_with_meta_by_plan_date 论证）。

    返 shape：``list[dict]`` 每项 ``{symbol, plan_date, **meta}``（含 plan_date 字段方便
    experiment report 按 ``p["date"]`` 聚合）。since/until 任一为 None 表示该侧不约束。
    since 与 until 同为 None → 返全量 SIGNAL（等价于扫盘）。

    Args:
        since: 起始 plan_date（YYYY-MM-DD，含）；None = 不设下界。
        until: 结束 plan_date（YYYY-MM-DD，含）；None = 不设上界。
    Returns:
        meta dict 列表（每项含 symbol / plan_date / **meta）；无命中返 []。
    """
    db_path = db_path or _DEFAULT_DB
    # 动态拼 WHERE：since/until 可选，参数化绑定防 SQL 注入。
    where = ["action='SIGNAL'"]
    params: list = []
    if since is not None:
        where.append("substr(trade_id, -10) >= ?")
        params.append(since)
    if until is not None:
        where.append("substr(trade_id, -10) <= ?")
        params.append(until)
    sql = ("SELECT symbol, meta, substr(trade_id, -10) AS plan_date FROM trade_event "
           "WHERE " + " AND ".join(where) + " ORDER BY event_id ASC")
    out: list[dict] = []
    with _connect(db_path) as con:
        rows = con.execute(sql, params).fetchall()
    for r in rows:
        if r["meta"] is None:
            continue
        try:
            meta = json.loads(r["meta"])
        except (TypeError, ValueError):
            logger.warning("list_signals_with_meta_by_plan_date_range meta JSON 解析失败 "
                           "symbol=%s plan_date=%s", r["symbol"], r["plan_date"])
            continue
        if not isinstance(meta, dict):
            continue
        out.append({"symbol": r["symbol"], "plan_date": r["plan_date"], **meta})
    return out


def list_signal_symbols_by_formed_at(dates: list[str], *,
                                     db_path: str | None = None) -> set[str]:
    """读 formed_at 落在给定自然日列表内的 SIGNAL 标的集（C2b cooldown 锚点查询）。

    物理意图（SSoT C2b）：engine._load_recent_plan_symbols 不再扫 plan_*.json，改读
    trade_event SIGNAL 行的 meta.formed_at（信号突破日 T）做 cooldown 跨日去重——
    cooldown 锚点是 formed_at（T 日信号突破）而非 timestamp（写入日 T 盘后）也非
    plan_date（T+1 计划生效日），用错锚点会把 T+1 才生效的标的算入 T 的 cooldown 窗口。

    **致命日期轴·formed_at 时间戳坑（红线）**：
        meta.formed_at 来源 = str(pd.Timestamp)（method_v0.py:268 ``W.index[-1]`` →
        plan.py:158 ``str(s.formed_at)``），落盘格式 = ``"2026-08-03 00:00:00"``（带时间戳），
        **非纯日期 "2026-08-03"**。若直接 ``json_extract(meta,'$.formed_at') IN (纯日期列表)``
        恒空（"2026-08-03 00:00:00" != "2026-08-03"）——同款查询轴坑（cf. count_signals_by_plan_date
        substr(trade_id,-10) 坑）。
        故必须 ``substr(json_extract(meta,'$.formed_at'),1,10)`` 取前 10 字符（YYYY-MM-DD）匹配。

    数学验证（substr(1,10)）：
        - formed_at="2026-08-03 00:00:00" → substr(1,10)="2026-08-03" ✓
        - dates=["2026-08-03","2026-08-04","2026-08-05"] → "2026-08-03" IN (...) 命中 ✓
        - 漏 substr：json_extract="2026-08-03 00:00:00" ∉ 纯日期列表 → 恒空（隐藏坑）。

    SQL 注入防护：IN 列表用 ``?, ?, ...`` 占位符参数化（非字符串拼接），dates 元素经 SQLite
    绑定参数类型安全。空列表返空集（不构造空 IN（）——SQL 语法错）。

    去重：SELECT DISTINCT symbol（同 symbol 同日多 SIGNAL 由 UNIQUE(account_id,trade_id,action)
    在 build_trade_id 单点口径下天然防重；DISTINCT 保守取「该日是否发过此标的」语义对齐）。

    Args:
        dates: 纯日期列表（YYYY-MM-DD，engine._load_recent_plan_symbols 算最近 N 自然日传入）。
    Returns:
        标的集；dates 为空返空集（短路，避免空 IN SQL 语法错）。
    """
    if not dates:
        return set()
    db_path = db_path or _DEFAULT_DB
    # IN 参数化：构造 len(dates) 个占位符（?,?,...,?），values 经绑定传入防 SQL 注入。
    placeholders = ",".join("?" * len(dates))
    sql = (
        "SELECT DISTINCT symbol FROM trade_event "
        "WHERE action='SIGNAL' "
        f"AND substr(json_extract(meta, '$.formed_at'), 1, 10) IN ({placeholders})"
    )
    with _connect(db_path) as con:
        rows = con.execute(sql, list(dates)).fetchall()
    return {r[0] for r in rows}


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


# 数据就绪单口判定的默认数据集清单。
# 物理意图：A 股日线引擎当前唯一内容数据集是 daily（a_shares_daily.parquet 对应
# data_ready.dataset='daily'）。pre_open gate 调 get_ready 不传 datasets 时用此默认。
# 未来扩展（minute/moneyflow 等）只需在此清单加 key，调用方无需改。
_DEFAULT_READY_DATASETS = ["daily"]


def get_ready(date: str, datasets: list[str] | None = None,
              db_path: str | None = None) -> bool:
    """W5 数据就绪单口判定（spec #13 · 消除「三张嘴」漂移）。

    物理意图（spec §4 T10）：
        08-04 问题——数据「就绪」三源无对账：
          ① data_ready 表（内容校验，pipeline_then_eod 完成后落盘 ok=1）；
          ② job_ledger.pipeline 状态（running/done/failed，跨重启运行台账）；
          ③ parquet mtime + .syncing 哨兵（data_service 派生态，观测健康度）。
        三源各自写、各自读 → 「台账 done、内容缺、播报 healthy」三方不一致。

        本函数合成 ① + ②（③ 为观测口径，保留双口径展示，不参与放行决策），
        pre_open gate③ / catchup / 播报端统一消费此函数判定「数据是否就绪」，
        任一源失败 → False + logger.warning 显式暴露差异（让研究员一眼看见哪源漂移）。

    判定逻辑（任一未绿即 False）：
        1. 遍历 datasets，逐个查 get_data_ready；None 或 ok!=1 → False + warning；
        2. job_ledger.latest_status("pipeline", date) != "done" → False + warning。

    错误分级（C-4 软降级语义）：
        任一源检查异常（DB 损坏/文件缺失/表不存在）→ False + warning/exception，
        不抛出——pre_open gate 调本函数时走「未就绪」分支软降级（不挂单 + CRITICAL
        由上层 _critical_guard 兜底），守「数据就绪判定不能猜」的安全底线。

    Args:
        date:     业务日（YYYY-MM-DD，与 data_ready.date / job_ledger.business_date 同口径）。
        datasets: 待校验的数据集 key 列表（默认 ["daily"]）。None 用默认清单。
        db_path:  trading_state.db 路径（测试隔离用）；job_ledger DB 走自己的 env/默认。

    Returns:
        True = ① 内容全绿 AND ② pipeline 台账 done（可放行挂单）；
        False = 任一源未绿或检查异常（不可放行，warning 已显式暴露差异原因）。
    """
    from trading import job_ledger  # 局部 import 避免模块级循环依赖风险

    keys = datasets if datasets is not None else _DEFAULT_READY_DATASETS

    # ① 内容校验：逐 dataset 查 data_ready 表
    for k in keys:
        try:
            ready = get_data_ready(date, k, db_path=db_path)
        except Exception:
            # DB 读异常（文件损坏/表不存在/IO 错）→ False + exception，不阻断 gate
            logger.exception("get_ready data_ready 检查异常 date=%s dataset=%s",
                             date, k)
            return False
        if ready is None:
            logger.warning(
                "get_ready=False：data_ready 未采集 date=%s dataset=%s", date, k)
            return False
        if not ready.get("ok"):
            msg = ready.get("message", "") or "ok=0"
            logger.warning(
                "get_ready=False：data_ready 内容校验未绿 date=%s dataset=%s msg=%s",
                date, k, msg)
            return False

    # ② pipeline 台账：job_ledger.latest_status("pipeline", date) 必须为 done
    try:
        status = job_ledger.latest_status("pipeline", date)
    except Exception:
        logger.exception("get_ready job_ledger 检查异常 date=%s", date)
        return False
    if status != "done":
        logger.warning(
            "get_ready=False：job_ledger.pipeline 非 done date=%s status=%s",
            date, status)
        return False

    return True
