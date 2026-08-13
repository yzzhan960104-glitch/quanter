# -*- coding: utf-8 -*-
"""C-8 job 运行台账：pipeline/pre_open 的跨重启运行状态（漏跑判定 + 幂等守卫）。

物理意图（spec §3.1）：
    生产机不 7x24，启动补跑需要「某业务日某 job 是否已跑」的持久真相源——plan 文件/
    data_ready/.last 文件都是产物反推，语义模糊（无 plan 可能是无信号而非没跑）。
    本台账以 (job_name, business_date) 为键记录状态机 running/done/skipped/failed，
    cron 与启动补跑共用（先查后写），保证「谁先完成谁生效」的最终一致性。

状态语义（spec §3.1）：
    running  = 执行中（进程崩溃残留由 reset_stale_running 启动重置）；
    done     = 流程正常完成（pipeline 含 run_eod=False 的裁剪态；pre_open 含无单可挂）；
    skipped  = pre_open gate 未过（无计划/未确认/网关/数据未就绪）——不算完成，补跑可重试；
    failed   = 采集失败 / data 未就绪 / 未预期异常。

设计约束（Karpathy 极简）：
    - 独立 sqlite（logs/trading_job_run.db），不混入 trading_state.db——台账是操作元数据，
      写失败绝不影响交易关键路径（调用方全部 try/except 包裹）；
    - 每次操作前 CREATE TABLE IF NOT EXISTS（幂等、零装配负担）；
    - path=None fallback 读 env TRADING_JOB_LEDGER_DB > 模块级 _DEFAULT_DB_PATH
      （同 backtest/tasks_db.py 测试隔离范式）。
"""
from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_DEFAULT_DB_PATH = "logs/trading_job_run.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS job_run (
  job_name      TEXT NOT NULL,
  business_date TEXT NOT NULL,
  status        TEXT NOT NULL,
  started_at    TEXT NOT NULL,
  finished_at   TEXT,
  message       TEXT,
  PRIMARY KEY (job_name, business_date)
);
"""


def _db_path(path: Optional[str] = None) -> str:
    """解析 DB 路径：显式 path > env TRADING_JOB_LEDGER_DB > 默认。"""
    if path is not None:
        return path
    env = os.getenv("TRADING_JOB_LEDGER_DB")
    return env if env else _DEFAULT_DB_PATH


def _connect(path: Optional[str] = None) -> sqlite3.Connection:
    """打开连接并保证表存在（幂等，零装配负担）。

    DG-G6（2026-08-14 g-wave-p0-guards · 对齐 backtest/tasks_db.py:45 + discovery/store.py:59
    正范式）：补齐 SQLite 并发协调三件套——
      ① ``timeout=30``：sqlite3.connect 默认 5s，并发写场景（cron + 启动补跑 + API
         查询）下秒抛 SQLITE_BUSY 中断台账写入。30s 给排队留足够余量；
      ② ``PRAGMA journal_mode=WAL``：Write-Ahead Logging 让读不阻塞写、写不阻塞读，
         rollback journal（默认 delete 模式）写时拿 EXCLUSIVE 锁，并发写直接 BUSY；
      ③ ``PRAGMA busy_timeout=30000``：连接级锁等待 30s（与 timeout 参数语义同源——
         Python sqlite3 ``timeout=X`` 内部就是设 busy_timeout=X*1000ms，显式 PRAGMA
         钉死口径，防 timeout 参数被误改后失去兜底）。

    物理意图：job_run 表是 cron 与启动补跑共用的「跨重启运行状态真相源」，二者并发
    写同一 (job, date) 时，必须由 WAL + timeout 串行化排队（而非 BUSY 中断），否则
    台账漏写会让幂等守卫误判「未跑」导致重复执行（spec §3.1 红线）。
    """
    db = _db_path(path)
    Path(db).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db, timeout=30)  # DG-G6：5s→30s 防并发 BUSY（对齐 tasks_db）
    conn.execute("PRAGMA journal_mode=WAL")           # DG-G6：WAL 让读不阻塞写
    conn.execute("PRAGMA busy_timeout=30000")         # DG-G6：锁等待 30s 兜底
    conn.execute(_SCHEMA)
    return conn


def init_db(path: Optional[str] = None) -> None:
    """建表（幂等）。显式入口，供启动补跑编排先清场。"""
    conn = _connect(path)
    conn.commit()
    conn.close()


def begin_run(job_name: str, business_date: str, started_at: str,
              path: Optional[str] = None) -> None:
    """标记 job 开始（INSERT OR REPLACE → running，重跑可覆盖旧终态）。

    DG-G6（2026-08-14 g-wave-p0-guards · 对齐 tasks_db.claim_next_pending 范式）：
    显式 ``BEGIN IMMEDIATE`` 包裹写操作——立即拿 RESERVED→EXCLUSIVE 写锁，让并发
    begin_run 串行排队（vs DEFERRED 在首条 INSERT 时才升级锁，多连接并发下存在锁升级
    deadlock 窗口）。配合 _connect 的 ``timeout=30`` + ``busy_timeout=30000``，并发
    begin_run 排队 30s 内必拿到锁，不抛 SQLITE_BUSY。

    幂等语义（红线，绝不破现有 test_begin_run_replaces_previous_status）：INSERT OR REPLACE
    覆盖旧终态为 running——无论之前是 done/skipped/failed/running，begin_run 一律重置为
    running + 清空 finished_at/message。这是「重跑覆盖」的物理意图（cron 与启动补跑都
    调 begin_run 标记开始，谁先谁后无关，最终状态由执行流推进，不由 begin_run 顺序决定）。
    """
    conn = _connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")  # 显式写锁，串行化并发 begin_run（DG-G6）
        conn.execute(
            "INSERT OR REPLACE INTO job_run "
            "(job_name, business_date, status, started_at, finished_at, message) "
            "VALUES (?, ?, 'running', ?, NULL, '')",
            (job_name, business_date, started_at),
        )
        conn.commit()
    except Exception:
        # BEGIN IMMEDIATE 拿锁失败（timeout）或 INSERT 异常 → 回滚，防残留事务态污染后续连接
        conn.rollback()
        raise
    finally:
        conn.close()


def finish_run(job_name: str, business_date: str, status: str,
               message: str = "", path: Optional[str] = None) -> None:
    """落终态（done/skipped/failed）。"""
    conn = _connect(path)
    conn.execute(
        "UPDATE job_run SET status=?, finished_at=?, message=? "
        "WHERE job_name=? AND business_date=?",
        (status, datetime.now().isoformat(), message, job_name, business_date),
    )
    conn.commit()
    conn.close()


def latest_status(job_name: str, business_date: str,
                  path: Optional[str] = None) -> Optional[str]:
    """查某 (job, date) 最新状态；无记录返 None。"""
    conn = _connect(path)
    row = conn.execute(
        "SELECT status FROM job_run WHERE job_name=? AND business_date=?",
        (job_name, business_date),
    ).fetchone()
    conn.close()
    return row[0] if row else None


def reset_stale_running(path: Optional[str] = None) -> int:
    """把遗留 running 全部置 failed('interrupted')，返回重置行数。

    物理意图：进程崩溃/重启会留下 running 残留；若不重置，cron/补跑的
    「running 跳过」守卫会永久阻塞该日 job（与 training_loops_db.reset_interrupted 同范式）。
    """
    conn = _connect(path)
    cur = conn.execute(
        "UPDATE job_run SET status='failed', finished_at=?, message='interrupted' "
        "WHERE status='running'",
        (datetime.now().isoformat(),),
    )
    conn.commit()
    n = cur.rowcount
    conn.close()
    return n


def snapshot_for_date(business_date: str, path: Optional[str] = None) -> list[dict]:
    """读某业务日全部 job 的最新台账行（只读 SELECT，不改状态机）。

    返回 [{name, status, started_at, finished_at, message}, ...]，按 job_name 升序。
    无记录返 []。物理意图（spec §5.1）：GET /trading/jobs 消费——把 C-8 台账暴露给
    前端驾驶舱，让研究员一眼看清「今天 pipeline/pre_open 跑没跑、为何没挂单（gate
    拒因=message）」。纯只读，调用方应 try/except 包裹，读失败降级返空，不阻断观测。
    """
    conn = _connect(path)
    rows = conn.execute(
        "SELECT job_name, status, started_at, finished_at, message "
        "FROM job_run WHERE business_date=? ORDER BY job_name",
        (business_date,),
    ).fetchall()
    conn.close()
    return [
        {"name": r[0], "status": r[1], "started_at": r[2],
         "finished_at": r[3], "message": r[4] or ""}
        for r in rows
    ]
