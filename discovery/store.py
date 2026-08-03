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
  data_hash       TEXT DEFAULT '',
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
  run_id                TEXT PRIMARY KEY,
  snapshot_hash         TEXT NOT NULL,
  started_at            TEXT,
  ended_at              TEXT,
  n_trials              INTEGER,
  status                TEXT,
  note                  TEXT,
  frontier_size_prev    INTEGER DEFAULT 0,        -- Plan 4 跨夜判据①：上夜前沿大小（daemon 比对扩张）
  k_rounds_no_expansion INTEGER DEFAULT 0,        -- 连续未扩张夜数（>=K=3 触发收敛自停）
  daemon_run_count      INTEGER DEFAULT 0);       -- 累计 daemon 调用次数（跨夜编排追溯）
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
    """建表（幂等）+ search_run 跨夜列 migration（Plan 4）。

    单点写锁内 executescript + PRAGMA 检列 ALTER，防并发建表/迁移竞争。SQLite 不支持
    ADD COLUMN IF NOT EXISTS，故 PRAGMA table_info 查列存否再 ALTER（仿 backtest/tasks_db.py:88-91）。
    新库由 SCHEMA 直接建全三列；老库（Plan 1-3 建的 search_run 无跨夜列）由本段 ALTER 补上，
    保证 Plan 4 daemon 在已有 trial 库上无缝升级（不要求清库）。
    """
    with _write_lock, connect(db_path) as conn:
        conn.executescript(SCHEMA)
        # P1-3（2026-08-03）：snapshot 表 data_hash 列 migration（老库补列；新库已建）。
        snap_cols = {r[1] for r in conn.execute("PRAGMA table_info(snapshot)")}
        if "data_hash" not in snap_cols:
            conn.execute("ALTER TABLE snapshot ADD COLUMN data_hash TEXT DEFAULT ''")
        # Plan 4 跨夜列 migration（老库补列；新库 SCHEMA 已建，下方 PRAGMA 检测会跳过）
        cols = {r[1] for r in conn.execute("PRAGMA table_info(search_run)")}
        for col, decl in [
            ("frontier_size_prev", "INTEGER DEFAULT 0"),
            ("k_rounds_no_expansion", "INTEGER DEFAULT 0"),
            ("daemon_run_count", "INTEGER DEFAULT 0"),
        ]:
            if col not in cols:
                conn.execute(f"ALTER TABLE search_run ADD COLUMN {col} {decl}")


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
    """落 snapshot 表（INSERT OR REPLACE upsert）。meta: SnapshotMeta。

    单点写锁包裹 execute，与 write_trial 对称：Plan 1 单写者场景虽安全，但 Plan 2 并发
    搜索时多 worker 会触 write_snapshot（跨线程 upsert 未串行化会破坏 ADR6 单点写一致性，
    spec §8 拷问②）。锁粒度同 write_trial——模块级单锁。
    """
    with _write_lock:
        conn.execute(
            "INSERT OR REPLACE INTO snapshot "
            "(snapshot_hash, universe_def, universe_count, date_range, lake_start, data_hash,"
            " created_at) VALUES (?,?,?,?,?,?,?)",
            (meta.snapshot_hash, meta.universe_def, meta.universe_count,
             meta.date_range, meta.lake_start, getattr(meta, "data_hash", ""), _now_iso()))


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


def read_trials_by_snapshot(conn, snapshot_hash):
    """读某 snapshot 下所有 trial（Pareto/DSR 计算用，spec §3.4）。

    返回 list[dict]，每项含 trial_id/inner_metrics/outer_metrics/source。
    inner_metrics/outer_metrics 是 JSON 字符串（write_trial 存的），调用方 json.loads。
    """
    rows = conn.execute(
        "SELECT trial_id, inner_metrics, outer_metrics, source FROM trial WHERE snapshot_hash=?",
        (snapshot_hash,)).fetchall()
    return [dict(r) for r in rows]


def write_search_run(conn, run_id, snapshot_hash, started_at, ended_at, n_trials,
                     status, frontier_size, k_rounds_no_expansion, daemon_run_count, note=""):
    """落 search_run 行（INSERT OR REPLACE upsert，含 Plan 4 跨夜状态字段）。

    每次 run_search/daemon 调用写一行；同 snapshot_hash 下多行（每夜一行），
    read_latest_search_run 按 started_at DESC 取最新做跨夜比对。单点写锁防多线程并发
    write_search_run + write_daemon_state 跨事务锁死（spec §8 拷问②，与 write_trial 同构）。
    frontier_size_prev 命名对应"本次跑完的前沿大小"——次夜 daemon 读作 prev 与本夜新值比对。
    """
    with _write_lock:
        conn.execute(
            "INSERT OR REPLACE INTO search_run "
            "(run_id, snapshot_hash, started_at, ended_at, n_trials, status, note, "
            " frontier_size_prev, k_rounds_no_expansion, daemon_run_count) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (run_id, snapshot_hash, started_at, ended_at, n_trials, status, note,
             frontier_size, k_rounds_no_expansion, daemon_run_count))


def read_latest_search_run(conn, snapshot_hash):
    """读同 snapshot 最新一行 search_run（Plan 4 跨夜判据① 状态源）。

    返回 dict（含 run_id/frontier_size_prev/k_rounds_no_expansion/daemon_run_count/status）
    或 None（首次 daemon，无历史 run——调用方据此跳过跨夜比对，k_rounds_no_expansion 从 0 起算）。
    按 started_at DESC 取最新——daemon 每夜 write_search_run 写一行，最新即上夜。
    不加 _write_lock：纯读操作，WAL 模式下读不阻塞写（spec §8 拷问②）。
    """
    row = conn.execute(
        "SELECT run_id, frontier_size_prev, k_rounds_no_expansion, daemon_run_count, status "
        "FROM search_run WHERE snapshot_hash=? ORDER BY started_at DESC LIMIT 1",
        (snapshot_hash,)).fetchone()
    return dict(row) if row else None


def read_latest_search_run_any(conn):
    """读最近一次 search_run（不限 snapshot，P1-3 数据版本审计用）。

    物理意图：每晚数据增量 → snapshot_hash 变 → read_latest_search_run 按精确 hash
    查不到上夜记录，跨夜判据①静默重置且原因不可见。本函数取全局最新一行（含
    snapshot_hash），daemon 据此判别"数据版本变化"（data_changed）并显式标注。
    """
    row = conn.execute(
        "SELECT run_id, snapshot_hash, frontier_size_prev, k_rounds_no_expansion,"
        " daemon_run_count, status "
        "FROM search_run ORDER BY started_at DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def write_daemon_state(conn, run_id, frontier_size, k_rounds_no_expansion,
                       daemon_run_count, status):
    """daemon 跨夜状态回写（UPDATE 本次 run_id 行的跨夜字段）。

    run_search 收尾已 write_search_run 落了本次行（frontier_size=本次, k=初值 0）；
    daemon 算完跨夜判据①后用本函数更新 k_rounds_no_expansion/daemon_run_count/status，
    使下一夜 read_latest_search_run 读到的是 daemon 算完的最终状态（而非 run_search 初值）。
    单点写锁与 write_search_run 对称，跨夜 daemon 单线程下亦安全，防的是测试/并发场景。
    """
    with _write_lock:
        conn.execute(
            "UPDATE search_run SET frontier_size_prev=?, k_rounds_no_expansion=?, "
            "daemon_run_count=?, status=? WHERE run_id=?",
            (frontier_size, k_rounds_no_expansion, daemon_run_count, status, run_id))
