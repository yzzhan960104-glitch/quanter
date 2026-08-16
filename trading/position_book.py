# -*- coding: utf-8 -*-
"""trading.position_book — 本地持仓账本（SQLite，WAL + 事务）。

物理定位：post_close 对账的「本地侧」单一真理源。记录本地系统记账的理论持仓
（独立于 broker 真实持仓），drift 检的就是两者偏差。

Why SQLite（对齐 experiment/store.py ADR3）：幂等天然（UNIQUE）/事务一致/WAL 并发/审计可追溯。

schema（live 准入升级 · 2026-07-28 live-readiness spec §3 地基）：
  - fill 表：UNIQUE(order_id, traded_time) —— 按成交笔增量幂等（非整笔去重），部分成交累加精度
  - position 表：加 avg_price（加权成本，浮盈对账）+ entry_date（首次建仓日，max_holding/trailing 用）

注：熔断 start_equity 基线快照原用 daily_equity 表（pre_open 写/post_close 读），
B5（SSoT Phase B）已删除本模块的 snapshot_start_equity/get_start_equity 与 daily_equity DDL——
熔断基线唯一读口 = state_store.snapshot/get_start_equity(account_daily 表)。
旧库残留 daily_equity 表无害（init_store/init_db 不再 CREATE，旧表存在=历史残留，不读写）。

迁移：init_db 列存在性检测，旧 schema（无 traded_time/avg_price）DROP+重建
（live 前无生产成交，可丢影子数据）。
"""
from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from trading import clock
# I-1 读侧归一 helper 单源（state_store 与本模块共用同一 position 表——归一逻辑不得
# 两处各写一份；state_store 无反向依赖本模块，无循环 import）
from trading.state_store import normalize_entry_date

logger = logging.getLogger(__name__)

_DEFAULT_DB = "logs/trading_state.db"
# 单账户默认 account_id（与 state_store._DEFAULT_ACCOUNT_ID 同值，多账户前所有持仓归此账户）。
# position 复合 PK(account_id, symbol) 改造后，旧调用方无 account_id 时用此默认值，向后兼容。
_DEFAULT_ACCOUNT_ID = "default"


@contextmanager
def _connect(db_path: str):
    """连接上下文：开 WAL，提交/回滚自动。每次操作新建连接（SQLite 非线程安全）。

    复用 experiment/store.py:_connect 范式（WAL + row_factory + 自动 commit/rollback）。
    """
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA journal_mode=WAL")
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


def init_db(db_path: str | None = None) -> None:
    """幂等建表 + schema 迁移。

    历史演进：live-readiness spec §3.1 先建了 symbol-PK 的 position/fill/daily_equity；
    trading-state-store-redesign spec §7.1 进一步把 position 升级为复合 PK (account_id, symbol)
    以支持多账户隔离。本函数现在建/迁移到 state_store 的统一 schema。

    迁移策略（列存在性检测，非 ALTER）：旧 fill 表无 traded_time / 旧 position 表无
    account_id 或 avg_price → DROP+重建（SQLite 改 UNIQUE/PK 必须重建表，直接重建比
    「建新+INSERT SELECT+DROP+RENAME」简洁）。live 前无生产成交，丢影子数据可接受。
    不引 schema_version（YAGNI）。

    与 state_store.init_store 的关系：state_store 是真相源，建完整 6 张表（含 account/origin/
    FK）。本函数只建 position_book 自身读写需要的子集（position/fill），与 state_store
    共用同一 db 文件，表结构一致（复合 PK），二者对同一表互不破坏。

    daily_equity 表（B5 已删）：原熔断 start_equity 基线快照表，C-1/W4 已把读写口迁到
    state_store.account_daily，本模块不再 CREATE/读写 daily_equity。旧库残留表无害（不读写）。
    """
    db_path = db_path or _DEFAULT_DB
    with _connect(db_path) as con:
        # 迁移：旧 fill（无 traded_time 列）→ DROP 重建（UNIQUE 改 order_id+traded_time）
        if _table_exists(con, "fill") and not _has_column(con, "fill", "traded_time"):
            logger.info("position_book 迁移：旧 fill 表无 traded_time，DROP 重建（部分成交增量幂等）")
            con.execute("DROP TABLE fill")
        # 迁移：旧 position（无 account_id 或无 avg_price）→ DROP 重建（复合 PK + 加权成本+entry_date）
        # 复合 PK 改造：单列 symbol PK 无法 ADD COLUMN 改 PK，必须重建（state-store-redesign spec §7.1）
        if _table_exists(con, "position") and (
            not _has_column(con, "position", "account_id")
            or not _has_column(con, "position", "avg_price")
        ):
            logger.info("position_book 迁移：旧 position 表无 account_id/avg_price，DROP 重建（复合PK+加权成本）")
            con.execute("DROP TABLE position")
        # 建表（IF NOT EXISTS：已 DROP 的重建，新库直接建，已是新结构的 no-op）
        # position 复合 PK(account_id, symbol)——多账户隔离；单账户默认 account_id=_DEFAULT_ACCOUNT_ID
        con.execute("""
            CREATE TABLE IF NOT EXISTS position (
                account_id  TEXT NOT NULL DEFAULT '""" + _DEFAULT_ACCOUNT_ID + """',
                symbol      TEXT NOT NULL,
                qty         REAL NOT NULL,
                avg_price   REAL,
                entry_date  TEXT,
                updated_at  TEXT NOT NULL,
                PRIMARY KEY (account_id, symbol)
            )
        """)
        # fill 兼容既有：traded_time 增量幂等；account_id 可选（state_store.fill 有，这里 IF NOT EXISTS 不改既有列）
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
        # 注：daily_equity 表 B5 已删（C-1/W4 读写口迁 state_store.account_daily），
        # init_db 不再 CREATE daily_equity，旧库残留表无害（不读写）。
        con.execute("CREATE INDEX IF NOT EXISTS idx_fill_symbol ON fill(symbol)")


def apply_fill(order_id: str, symbol: str, direction: str, qty: float, price: float,
               traded_time: str, *, account_id: str | None = None,
               db_path: str | None = None) -> bool:
    """成交回报应用：写流水（增量幂等）+ 更新持仓（加权 avg + entry_date 锁定）。

    物理意图：_handle_order_update 在 on_stock_trade（kind=="trade"）时调本函数，把
    「本地记账」与 broker 同步。traded_volume 是本笔增量（非累计），apply_fill 每次 += delta。

    幂等（R1 红线）：UNIQUE(order_id, traded_time) —— 同笔成交（同 order_id + 同 traded_time）
    重推 IntegrityError → 返 False（不重复加减）。不同 traded_time 的多笔部分成交各自入账（累加）。

    加权 avg_price（BUY）：new_avg = (old_qty·old_avg + qty·price) / new_qty；
    SELL 不变（A 股口径，卖出不动成本）。
    entry_date：首次 BUY（old 无 entry_date）写当日，加仓/减仓不改（建仓日锁定）。

    Args:
        traded_time: on_stock_trade 本笔成交时间（幂等键一半，来自 update["traded_time"]）。
        account_id: 所属账户（复合 PK 一部分），None 时用 _DEFAULT_ACCOUNT_ID（单账户向后兼容）。
    Returns:
        True=本笔首次入账；False=重复 (order_id, traded_time) 跳过。
    """
    if direction not in ("BUY", "SELL"):
        raise ValueError(f"apply_fill direction 仅 BUY/SELL，收到 {direction}")
    db_path = db_path or _DEFAULT_DB
    account_id = account_id or _DEFAULT_ACCOUNT_ID
    # C-6 V3：单一时间源收口——now/today 走 clock，防同轮跨午夜漂移 + 测试 monkeypatch 单点冻结。
    now = clock.now().isoformat()
    today = clock.today()
    delta = float(qty) if direction == "BUY" else -float(qty)
    with _connect(db_path) as con:
        try:
            con.execute(
                "INSERT INTO fill(order_id, traded_time, symbol, direction, qty, price, applied_at)"
                " VALUES(?, ?, ?, ?, ?, ?, ?)",
                (order_id, traded_time, symbol, direction, float(qty), float(price), now))
        except sqlite3.IntegrityError:
            # (order_id, traded_time) 重推幂等跳过（持仓不重复加减）。
            # IntegrityError 时 INSERT 被语句层拒，事务无 pending，commit no-op，不影响一致性。
            logger.info("apply_fill 跳过重复 order_id=%s traded_time=%s", order_id, traded_time)
            return False
        # 读当前持仓（加权 avg / entry_date 判定用）——复合 PK (account_id, symbol)
        row = con.execute(
            "SELECT qty, avg_price, entry_date FROM position WHERE account_id=? AND symbol=?",
            (account_id, symbol)
        ).fetchone()
        old_qty = float(row["qty"]) if row else 0.0
        old_avg = float(row["avg_price"]) if row and row["avg_price"] is not None else None
        old_entry = row["entry_date"] if row else None

        new_qty = old_qty + delta
        if direction == "BUY":
            # 加权 avg_price：old_qty>0 加权，否则（首次建仓/空头回补）取本笔价
            if old_qty > 0 and old_avg is not None and new_qty != 0:
                new_avg = (old_qty * old_avg + float(qty) * float(price)) / new_qty
            else:
                new_avg = float(price)
            # entry_date 首次 BUY 锁定（old 无值才写今日；加仓不改）
            new_entry = old_entry if old_entry is not None else today
        else:  # SELL
            new_avg = old_avg  # A 股口径：卖出不动 avg_price
            new_entry = old_entry

        # UPSERT（复合 PK 冲突目标 (account_id, symbol)；excluded.* = VALUES 提供的新值）
        con.execute(
            "INSERT INTO position(account_id, symbol, qty, avg_price, entry_date, updated_at) VALUES(?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(account_id, symbol) DO UPDATE SET qty=excluded.qty, avg_price=excluded.avg_price,"
            " entry_date=excluded.entry_date, updated_at=excluded.updated_at",
            (account_id, symbol, new_qty, new_avg, new_entry, now))
        # 归零清理：qty=0 删除（对账并集不被 0 干扰，账本干净）
        con.execute("DELETE FROM position WHERE qty=0")
    return True


def get_local_positions(*, db_path: str | None = None) -> dict[str, float]:
    """读本地理论持仓 {symbol: qty}（qty!=0）。供 _post_close 对账注入 local_positions。"""
    db_path = db_path or _DEFAULT_DB
    with _connect(db_path) as con:
        rows = con.execute("SELECT symbol, qty FROM position WHERE qty != 0").fetchall()
    return {r["symbol"]: float(r["qty"]) for r in rows}


def reconcile_qty(symbol: str, qty: float, *, account_id: str | None = None,
                  db_path: str | None = None) -> None:
    """盘后兜底纠正：以 query_trades 聚合为准重写单 symbol 的 position.qty。

    物理意图（plan Task 11 · spec §5.1）：apply_fill 漏记（db lock/异常软降级）致 position_book
    与 CSV 成交流水 drift 时，盘后 query_trades 聚合为准重写 qty，让账本回到真实净持仓。

    Why 保留 avg_price/entry_date：query_trades 流水是逐笔（不含加权 avg），重算 avg 需重放
        全部 fill；entry_date 是建仓日（max_holding/trailing 依赖，保留旧值更稳）。drift 纠正
        通常只改 qty（apply_fill 漏记是少记量，非成本/日期错）。
    新 symbol（position 无该行）→ avg_price/entry_date 写 NULL（CSV 无成本，后续盈亏播报显 N/A，
        不猜价）。qty=0 → 删除（与 apply_fill 归零清理同口径，账本干净）。
    """
    db_path = db_path or _DEFAULT_DB
    account_id = account_id or _DEFAULT_ACCOUNT_ID
    now = clock.now().isoformat()
    with _connect(db_path) as con:
        if abs(float(qty)) < 1e-9:
            con.execute(
                "DELETE FROM position WHERE account_id=? AND symbol=?", (account_id, symbol))
            return
        # UPSERT：复合 PK 冲突目标 (account_id, symbol)；新行写 NULL avg/entry（CSV 无成本）；
        # 已有行 ON CONFLICT 仅更新 qty+updated_at（不动 avg_price/entry_date，保留 apply_fill 算好的成本/建仓日）
        con.execute(
            "INSERT INTO position(account_id, symbol, qty, avg_price, entry_date, updated_at)"
            " VALUES(?, ?, ?, NULL, NULL, ?)"
            " ON CONFLICT(account_id, symbol) DO UPDATE SET qty=excluded.qty, updated_at=excluded.updated_at",
            (account_id, symbol, float(qty), now))


def get_entry_dates(*, db_path: str | None = None) -> dict[str, str]:
    """读 {symbol: entry_date}（首次建仓日，qty!=0）。

    供 max_holding（P0-4）/ trailing（R-3）的 holding_days 计算。entry_date None（老数据/
    迁移残留）不返回。
    I-1 读侧防御（2026-08-16 终审）：entry_date 出库时经 normalize_entry_date 归一
    （8 位紧凑存量 → 横杠格式）——本函数是 stop_loss 扫超期 / engine._stoploss 移动
    止损的 holding_days 数据源，trading_days_between 的 strptime("%Y-%m-%d") 对
    8 位格式抛 ValueError → except 返 0 → max_holding 超时平仓链失明（生产曾有两行
    8 位存量：600519.SH/300654.SZ，backfill hack 写入，已 SQL 迁移 + 本层双保险）。
    """
    db_path = db_path or _DEFAULT_DB
    with _connect(db_path) as con:
        rows = con.execute(
            "SELECT symbol, entry_date FROM position WHERE qty != 0 AND entry_date IS NOT NULL"
        ).fetchall()
    return {r["symbol"]: normalize_entry_date(r["entry_date"]) for r in rows}


