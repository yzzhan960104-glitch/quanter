# -*- coding: utf-8 -*-
"""caisen.replay_tasks_db 异步回测任务表 SQLite 访问层（Spec 1 · Task 1）。

（待迁·Step4 移出 caisen 包至执行编排层）本模块当前物理位于 caisen/infra/ 过渡子包，
Step4 将连同 storage/execution/backtest_replay/replay_*/viz_* 整体迁出 caisen 包至独立的
执行编排层。当前位置仅为 Step3 分层重构的中间态。

物理定位：异步回测任务全生命周期持久化（PENDING/RUNNING/SUCCESS/FAILED/CANCELLED）。
单一真相源——吸收原 replay_runs JSON 归档（成功 report 内嵌 report_json 列）。
标准库 sqlite3 + WAL，无新依赖（合 Karpathy 极简；不引 SQLAlchemy）。

并发模型：主进程（API + 调度器）单点写，worker 经 Queue 上报进度由主进程落库，
避免跨进程 SQLite 写锁。WAL 模式读不阻塞写。

路径解析约定（关键，防测试隔离失效）：
    所有公共函数 path 参数默认 None，函数内 fallback 读模块级 _DEFAULT_DB_PATH。
    之所以不用 `path=_DEFAULT_DB_PATH` 形式的默认参数——Python 默认参数在 def 时
    一次性求值绑定，后续测试 monkeypatch.setattr(_DEFAULT_DB_PATH, tmp) 不会改变
    已绑定默认值，会导致用例写进真实 data/replay_tasks.db 互相污染。故统一 None+fallback。
"""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime
from typing import Optional

# 任务表 DB 文件（与其他数据资产同源 data/ 目录）。测试 monkeypatch 覆盖。
_DEFAULT_DB_PATH = "data/replay_tasks.db"
# 合法状态机取值（PENDING→RUNNING→SUCCESS/FAILED/CANCELLED）。
_VALID_STATUS = ("PENDING", "RUNNING", "SUCCESS", "FAILED", "CANCELLED")


def _now_iso() -> str:
    """ISO 微秒时间戳（列表降序排序键 + 展示 + 心跳比对）。"""
    return datetime.now().isoformat(timespec="microseconds")


def _resolve(path: Optional[str]) -> str:
    """path=None → 读模块级 _DEFAULT_DB_PATH（让 monkeypatch 隔离生效，见模块 docstring）。"""
    return _DEFAULT_DB_PATH if path is None else path


def _connect(path: str) -> sqlite3.Connection:
    """打开 SQLite 连接（autocommit + WAL + Row 工厂）。

    timeout=30：防极端并发写时抛 SQLITE_BUSY；isolation_level=None 即 autocommit，
    事务靠显式 BEGIN/COMMIT（仅 claim_next_pending 用 IMMEDIATE 写锁防并发双领）。
    """
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db(path: Optional[str] = None) -> None:
    """建表 + 索引（幂等，IF NOT EXISTS）。WAL 模式提升并发读。"""
    path = _resolve(path)
    with _connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS replay_tasks (
                task_id        TEXT PRIMARY KEY,
                created_at     TEXT NOT NULL,
                status         TEXT NOT NULL,
                progress       INTEGER DEFAULT 0,
                start          TEXT,
                end            TEXT,
                universe_n     INTEGER,       -- -1=全市场；正数=标的个数（列表长度）
                universe_json  TEXT,           -- 完整 symbol 列表（null=全市场），worker 还原装配用
                cfg_override   TEXT,
                error          TEXT,
                report_json    TEXT,
                started_at     TEXT,
                finished_at    TEXT,
                last_heartbeat TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_tasks_status  ON replay_tasks(status);
            CREATE INDEX IF NOT EXISTS idx_tasks_created ON replay_tasks(created_at DESC);
            """
        )


def _row_to_dict(row: sqlite3.Row) -> dict:
    """行字典化 + 反序列化 universe/cfg_override/report_json（None 透传）。

    universe_json：NULL 或 "null" → None（全市场）；JSON 数组 → list。
    """
    d = dict(row)
    raw_uni = d.get("universe_json")
    d["universe"] = json.loads(raw_uni) if raw_uni else None
    d["cfg_override"] = json.loads(d["cfg_override"]) if d.get("cfg_override") else {}
    d["report"] = json.loads(d["report_json"]) if d.get("report_json") else None
    return d


def create_task(req: dict, path: Optional[str] = None) -> str:
    """生成 task_id + 写 PENDING 行。universe=None→全市场(universe_n=-1)；列表→存完整+个数。"""
    path = _resolve(path)
    task_id = uuid.uuid4().hex
    universe = req.get("universe")
    universe_n = -1 if universe is None else len(universe)
    with _connect(path) as conn:
        conn.execute(
            """INSERT INTO replay_tasks
               (task_id, created_at, status, progress, start, end, universe_n,
                universe_json, cfg_override)
               VALUES (?, ?, 'PENDING', 0, ?, ?, ?, ?, ?)""",
            (task_id, _now_iso(), req.get("start"), req.get("end"),
             universe_n, json.dumps(universe),
             json.dumps(req.get("cfg_override") or {}, ensure_ascii=False)),
        )
    return task_id


def get_task(task_id: str, path: Optional[str] = None) -> Optional[dict]:
    path = _resolve(path)
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT * FROM replay_tasks WHERE task_id=?", (task_id,)).fetchone()
    return _row_to_dict(row) if row else None


def list_tasks(status: Optional[str] = None, limit: int = 100,
               path: Optional[str] = None) -> list[dict]:
    """按 created_at 降序；status=None 全量，否则按状态精确过滤。"""
    path = _resolve(path)
    with _connect(path) as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM replay_tasks WHERE status=? ORDER BY created_at DESC LIMIT ?",
                (status, limit)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM replay_tasks ORDER BY created_at DESC LIMIT ?",
                (limit,)).fetchall()
    return [_row_to_dict(r) for r in rows]


def list_success_runs(path: Optional[str] = None) -> list[dict]:
    """供 /replay/runs：只返 SUCCESS（成功回测档案，等价 list_tasks('SUCCESS')）。"""
    return list_tasks(status="SUCCESS", limit=1000, path=path)


def claim_next_pending(path: Optional[str] = None) -> Optional[dict]:
    """调度器原子领取最老 PENDING → 即时标 RUNNING（事务防并发双领）。无 PENDING 返 None。

    物理意图：concurrency=1 串行，BEGIN IMMEDIATE 拿写锁后 SELECT+UPDATE 同事务内完成，
    杜绝两个调度器线程/实例同时领到同一 PENDING（双跑同一回测浪费算力）。
    """
    path = _resolve(path)
    with _connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")          # 写锁，防并发 claim
        row = conn.execute(
            "SELECT * FROM replay_tasks WHERE status='PENDING' "
            "ORDER BY created_at ASC LIMIT 1").fetchone()
        if row is None:
            conn.execute("COMMIT")
            return None
        conn.execute(
            "UPDATE replay_tasks SET status='RUNNING', started_at=?, last_heartbeat=? "
            "WHERE task_id=?",
            (_now_iso(), _now_iso(), row["task_id"]))
        conn.execute("COMMIT")
    return get_task(row["task_id"], path)


def update_progress(task_id: str, progress: int, path: Optional[str] = None) -> None:
    path = _resolve(path)
    with _connect(path) as conn:
        conn.execute("UPDATE replay_tasks SET progress=? WHERE task_id=?", (progress, task_id))


def update_heartbeat(task_id: str, path: Optional[str] = None) -> None:
    path = _resolve(path)
    with _connect(path) as conn:
        conn.execute("UPDATE replay_tasks SET last_heartbeat=? WHERE task_id=?",
                     (_now_iso(), task_id))


def mark_success(task_id: str, report_json: str, path: Optional[str] = None) -> None:
    """SUCCESS：写完整 report_json + progress=100 + finished_at。"""
    path = _resolve(path)
    with _connect(path) as conn:
        conn.execute(
            "UPDATE replay_tasks SET status='SUCCESS', report_json=?, progress=100, "
            "finished_at=? WHERE task_id=?",
            (report_json, _now_iso(), task_id))


def mark_failed(task_id: str, error: str, path: Optional[str] = None) -> None:
    path = _resolve(path)
    with _connect(path) as conn:
        conn.execute(
            "UPDATE replay_tasks SET status='FAILED', error=?, finished_at=? WHERE task_id=?",
            (error, _now_iso(), task_id))


def mark_cancelled(task_id: str, path: Optional[str] = None) -> None:
    path = _resolve(path)
    with _connect(path) as conn:
        conn.execute("UPDATE replay_tasks SET status='CANCELLED', finished_at=? WHERE task_id=?",
                     (_now_iso(), task_id))


def delete_task(task_id: str, path: Optional[str] = None) -> bool:
    """删除单任务（DELETE 端点用）。返回是否真的删除了行。"""
    path = _resolve(path)
    with _connect(path) as conn:
        cur = conn.execute("DELETE FROM replay_tasks WHERE task_id=?", (task_id,))
    return cur.rowcount > 0


def reset_running_to_failed(path: Optional[str] = None) -> int:
    """重启恢复：崩溃/重启残留的 RUNNING 标 FAILED（不自动重跑，由用户决定重提）。

    物理意图：uvicorn 重启时上一轮卡 RUNNING 的任务无法继续，标 FAILED + 原因，
    避免无意识重复消耗几十分钟~几小时算力（与 spec §3.3/§7 统一语义）。
    """
    path = _resolve(path)
    with _connect(path) as conn:
        cur = conn.execute(
            "UPDATE replay_tasks SET status='FAILED', error='进程重启中断（需手动重提）', "
            "finished_at=? WHERE status='RUNNING'",
            (_now_iso(),))
    return cur.rowcount
