# -*- coding: utf-8 -*-
"""SQLite 三表落库（spec §3.4，对齐 backtest/tasks_db.py 的 WAL + 单点写模式，ADR6）。

三表：snapshot（快照登记）/ trial（单次试验）/ search_run（跑批）。Plan 1 精简：
trial 表直接存 inner/outer metrics JSON；score_fn_version/seed/oos_metrics 等留后续 plan。
WAL 模式 + threading.Lock 单点写，防多进程/多线程跨进程锁（spec §8 拷问②）。
"""
import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime

DEFAULT_DB_PATH = "logs/discovery_trials.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshot (
  snapshot_hash   TEXT PRIMARY KEY,
  universe_def    TEXT NOT NULL,
  universe_count  INTEGER,
  date_range      TEXT NOT NULL,
  lake_start      TEXT,
  created_at      TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS trial (
  trial_id        TEXT PRIMARY KEY,
  params          TEXT NOT NULL,
  snapshot_hash   TEXT NOT NULL,
  engine_hash     TEXT NOT NULL,
  split           TEXT NOT NULL,
  inner_metrics   TEXT,
  outer_metrics   TEXT,
  source          TEXT NOT NULL,
  created_at      TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_trial_snapshot ON trial(snapshot_hash);
CREATE TABLE IF NOT EXISTS search_run (
  run_id          TEXT PRIMARY KEY,
  snapshot_hash   TEXT NOT NULL,
  started_at      TEXT,
  ended_at        TEXT,
  n_trials        INTEGER,
  status          TEXT,
  note            TEXT);
"""

_write_lock = threading.Lock()   # 单点写：跨线程串行化写，配合 WAL 防锁死


def _now_iso() -> str:
    """ISO 时间戳（created_at 落库用）。"""
    return datetime.utcnow().isoformat()


@contextmanager
def connect(db_path=DEFAULT_DB_PATH):
    """连接上下文：WAL + Row 工厂 + commit/close。

    物理意图：sqlite3.connect 的 timeout=30 防 SQLITE_BUSY 短暂等待；WAL 模式让并发读
    不阻塞写。写操作外加 _write_lock（见 write_trial/init_db 调用点），跨线程串行化写，
    保证 SQLite 单写者语义（spec §8 拷问②——多线程并发写不锁死）。
    """
    import os
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)   # timeout 防 SQLITE_BUSY 短暂等待
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path=DEFAULT_DB_PATH):
    """建表（幂等）。单点写锁内 executescript，防并发建表竞争。"""
    with _write_lock, connect(db_path) as conn:
        conn.executescript(SCHEMA)


def trial_id_of(params, snapshot_hash, seed=0):
    """trial_id = sha256(params+snapshot+seed)[:12]，天然去重键（spec §3.2）。

    物理意图：同一组 params 在同一快照、同一 seed 下，回测结果必须可复现——故 trial_id
    作为去重键，使断点续跑/重跑天然幂等（INSERT OR IGNORE 见 write_trial）。
    default=str 防 numpy/decimal 等非原生类型序列化失败。
    """
    sig = json.dumps({"p": params, "s": snapshot_hash, "seed": seed},
                     sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(sig.encode("utf-8")).hexdigest()[:12]


def write_snapshot(conn, meta):
    """落 snapshot 表（INSERT OR REPLACE upsert）。meta: SnapshotMeta。"""
    conn.execute(
        "INSERT OR REPLACE INTO snapshot "
        "(snapshot_hash, universe_def, universe_count, date_range, lake_start, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (meta.snapshot_hash, meta.universe_def, meta.universe_count,
         meta.date_range, meta.lake_start, _now_iso()))


def write_trial(conn, trial_id, params, snapshot_hash, engine_hash, split,
                inner_metrics, outer_metrics, source):
    """落 trial 表（INSERT OR IGNORE 去重——同 trial_id 不覆盖）。

    单点写锁包裹 execute，防多线程并发 connect→commit 跨写事务锁死（spec §8 拷问②）。
    metrics 存 JSON（Plan 1 精简——后续 plan 才拆列或引入 score_fn_version）。锁的粒度
    是模块级单锁（trial 表是单写者），与 backtest/tasks_db.py 的单点写模式同构（ADR6）。
    """
    with _write_lock:
        conn.execute(
            "INSERT OR IGNORE INTO trial "
            "(trial_id, params, snapshot_hash, engine_hash, split, inner_metrics, outer_metrics, source, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (trial_id, json.dumps(params, ensure_ascii=False, default=str), snapshot_hash,
             engine_hash, split, json.dumps(inner_metrics, ensure_ascii=False, default=str),
             json.dumps(outer_metrics, ensure_ascii=False, default=str), source,
             _now_iso()))


def trial_exists(conn, trial_id):
    """trial_id 是否已存在（断点续跑/去重用）。"""
    return conn.execute("SELECT 1 FROM trial WHERE trial_id=?", (trial_id,)).fetchone() is not None
