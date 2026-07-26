# -*- coding: utf-8 -*-
"""trading.position_book — 本地持仓账本（SQLite，WAL + 事务）。

物理定位：post_close 对账的「本地侧」单一真理源。记录本地系统记账的理论持仓
（独立于 broker 真实持仓），drift 检的就是两者偏差。

Why SQLite 而非 JSON（对齐 experiment/store.py ADR3）：
- 幂等天然：fill 表 UNIQUE(order_id) 让重推 INSERT 失败即跳过，无需手写 processed_orders；
- 事务一致性：写流水 + 更新持仓同事务原子（崩溃不 corrupt）；
- 并发安全：WAL 多读单写；
- 审计可追溯：fill 表即成交流水。

复用范式：experiment/store.py 的 _connect（WAL + row_factory + 自动 commit/rollback）。

db_path 默认参数用 None + 运行时解析 _DEFAULT_DB：
    规避 Python 默认参数定义期绑定陷阱——e2e 测试 monkeypatch._DEFAULT_DB 能全局生效
    （engine 内 `position_book.get_local_positions()` 不传 db_path 也能命中 tmp）。
"""
from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_DB = "logs/trading_state.db"


@contextmanager
def _connect(db_path: str):
    """连接上下文：开 WAL，提交/回滚自动。SQLite 连接非线程安全，每次操作新建连接。

    复用 experiment/store.py:_connect 范式（WAL + row_factory + 自动 commit/rollback）。
    首次运行建父目录（logs/trading_state/），否则 sqlite3.connect 写不存在的目录会抛。
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


def init_db(db_path: str | None = None) -> None:
    """幂等建表（CREATE TABLE IF NOT EXISTS）。由 trading/__main__ 启动期调用。

    两张表：
      - position：每个 symbol 当前净持仓（对账读这张，post_close 消费）；
      - fill：成交流水 + UNIQUE(order_id) 幂等去重（重推 INSERT 失败即跳过，R1 红线）。
    """
    db_path = db_path or _DEFAULT_DB
    with _connect(db_path) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS position (
                symbol     TEXT PRIMARY KEY,
                qty        REAL NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS fill (
                fill_id    INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id   TEXT NOT NULL,
                symbol     TEXT NOT NULL,
                direction  TEXT NOT NULL,
                qty        REAL NOT NULL,
                price      REAL NOT NULL,
                applied_at TEXT NOT NULL,
                UNIQUE(order_id)
            )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_fill_symbol ON fill(symbol)")


def apply_fill(order_id: str, symbol: str, direction: str, qty: float, price: float,
               *, db_path: str | None = None) -> bool:
    """成交回报应用：写流水 + 更新持仓（单事务原子）。

    物理意图：成交回报 handler（_handle_order_update）在真实成交（BUY/SELL）时调本函数，
    把「本地记账」与 broker 真实持仓保持同步——post_close 对账才能检出 drift。

    幂等（R1 红线）：order_id 已存在 → INSERT 抛 IntegrityError → 返 False（重推跳过，
    持仓不重复加减）。Phase1 按 order_id 整笔去重（首次成交量入账），部分成交累加精度
    留 live follow-up（按 gw._orders[order_id].qty_traded 单调累计取增量）。

    方向（R3 红线）：BUY → +qty；SELL → -qty；其它 → 抛 ValueError。调用方（_handle_order_update）
    应已过滤 None 方向，但本函数再守一道防误调。

    清理：持仓归零的标的 DELETE（保持账本干净，对账并集不被 qty=0 干扰）。

    Returns:
        True = 首次应用入账；False = 重复 order_id 跳过。
    """
    if direction not in ("BUY", "SELL"):
        raise ValueError(f"apply_fill direction 仅 BUY/SELL，收到 {direction}")
    db_path = db_path or _DEFAULT_DB
    now = datetime.now().isoformat()
    delta = float(qty) if direction == "BUY" else -float(qty)
    with _connect(db_path) as con:
        try:
            con.execute(
                "INSERT INTO fill(order_id, symbol, direction, qty, price, applied_at)"
                " VALUES(?, ?, ?, ?, ?, ?)",
                (order_id, symbol, direction, float(qty), float(price), now))
        except sqlite3.IntegrityError:
            # 重推幂等：order_id 已入账，跳过（持仓不重复加减——R1 红线）。
            # 注：IntegrityError 时 INSERT 被 SQLite 在语句层直接拒，事务无 pending changes，
            # 后续 _connect 上下文的 con.commit() 是 no-op（rollback/commit 对空事务均合法），
            # 不影响账本一致性（fill/position 表均无任何写入）。
            logger.info("apply_fill 跳过重复 order_id=%s symbol=%s", order_id, symbol)
            return False
        # UPSERT 持仓（SQLite ON CONFLICT 语法，3.24+ 支持）。
        con.execute(
            "INSERT INTO position(symbol, qty, updated_at) VALUES(?, ?, ?)"
            " ON CONFLICT(symbol) DO UPDATE SET qty=qty+?, updated_at=?",
            (symbol, delta, now, delta, now))
        # 归零清理：qty=0 的标的删除（对账并集不被 0 干扰，账本保持干净）。
        con.execute("DELETE FROM position WHERE qty=0")
    return True


def get_local_positions(*, db_path: str | None = None) -> dict[str, float]:
    """读本地理论持仓 {symbol: qty}（qty!=0）。供 _post_close 对账注入 local_positions。"""
    db_path = db_path or _DEFAULT_DB
    with _connect(db_path) as con:
        rows = con.execute("SELECT symbol, qty FROM position WHERE qty != 0").fetchall()
    return {r["symbol"]: float(r["qty"]) for r in rows}
