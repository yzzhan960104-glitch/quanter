# -*- coding: utf-8 -*-
"""SQLite 持久化（experiment/experiments.db，WAL + 事务）+ 审计写入。

Why SQLite 而非 JSON（design ADR3）：结构化查询（按 status/experiment_id 索引）、
事务一致性（promote = 更新版本表 + 写审计，原子）、并发写安全（WAL）。
plan 归因仍走 plans/<date>.json（execution/storage 不动底层），SQLite 只管实验版本/审计。

复用范式：execution/replay_tasks_db.py 的 SQLite 用法（标准库 sqlite3，WAL，上下文管理器）。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from typing import Optional

from experiment.models import (
    AuditLog, ExperimentStatus, ExperimentVersion, validate_transition, validate_weight_sum,
)

_DEFAULT_DB = "experiment/experiments.db"


@contextmanager
def _connect(db_path: str):
    """连接上下文：开 WAL，提交/回滚自动。SQLite 连接非线程安全，每次操作新建连接。"""
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        # WAL：多读单写并发安全（比默认 rollback journal 强），适合 CLI + scan 并发
        con.execute("PRAGMA journal_mode=WAL")
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def init_db(db_path: str = _DEFAULT_DB) -> None:
    """建表 + 索引（幂等，已存在不报错）。design §3.5 schema。"""
    with _connect(db_path) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS experiment_version (
                experiment_id TEXT PRIMARY KEY, strategy_name TEXT NOT NULL,
                params TEXT NOT NULL, weight REAL NOT NULL, status TEXT NOT NULL,
                version INTEGER NOT NULL, source TEXT, note TEXT,
                created_at TEXT NOT NULL, activated_at TEXT, archived_at TEXT,
                UNIQUE(strategy_name, version))
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_status ON experiment_version(status)")
        con.execute("""
            CREATE TABLE IF NOT EXISTS audit_log (
                audit_id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
                action TEXT NOT NULL, experiment_id TEXT NOT NULL,
                changed_fields TEXT, operator TEXT, note TEXT)
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_audit_exp ON audit_log(experiment_id)")


def _row_to_version(row: sqlite3.Row) -> ExperimentVersion:
    """行 → ExperimentVersion（params JSON 反序列化）。"""
    return ExperimentVersion(
        experiment_id=row["experiment_id"], strategy_name=row["strategy_name"],
        params=json.loads(row["params"]), weight=row["weight"],
        status=ExperimentStatus(row["status"]), version=row["version"],
        source=row["source"] or "", note=row["note"] or "",
        created_at=row["created_at"], activated_at=row["activated_at"],
        archived_at=row["archived_at"])


def _write_audit(con, *, action: str, experiment_id: str, operator: str, now: str,
                 changed_fields: Optional[dict] = None, note: str = "") -> None:
    """写一条审计（在调用方事务内，保证与版本变更原子）。"""
    con.execute(
        "INSERT INTO audit_log(timestamp, action, experiment_id, changed_fields, operator, note)"
        " VALUES(?, ?, ?, ?, ?, ?)",
        (now, action, experiment_id,
         json.dumps(changed_fields or {}, ensure_ascii=False), operator, note))


def create_version(db_path: str, version: ExperimentVersion, operator: str = "cli") -> None:
    """create：写一条 DRAFT 版本（params 已定，weight 通常 0，待 promote 设权重）。"""
    with _connect(db_path) as con:
        try:
            con.execute(
                "INSERT INTO experiment_version(experiment_id, strategy_name, params, weight,"
                " status, version, source, note, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (version.experiment_id, version.strategy_name,
                 json.dumps(version.params, ensure_ascii=False), version.weight,
                 version.status.value, version.version, version.source, version.note,
                 version.created_at))
        except sqlite3.IntegrityError as e:
            raise ValueError(f"实验版本已存在或 strategy+version 冲突: {version.experiment_id}") from e
        _write_audit(con, action="create", experiment_id=version.experiment_id,
                     operator=operator, now=version.created_at,
                     changed_fields={"params": version.params})


def _active_versions(con) -> list:
    """读当前所有 ACTIVE 版本（用于权重和校验）。"""
    rows = con.execute("SELECT * FROM experiment_version WHERE status=?",
                       (ExperimentStatus.ACTIVE.value,)).fetchall()
    return [_row_to_version(r) for r in rows]


def promote(db_path: str, experiment_id: str, weight: float, operator: str,
            now: str) -> None:
    """promote: DRAFT→ACTIVE + 设权重。校验状态迁移 + 权重和。"""
    with _connect(db_path) as con:
        row = con.execute("SELECT * FROM experiment_version WHERE experiment_id=?",
                          (experiment_id,)).fetchone()
        if row is None:
            raise ValueError(f"实验版本不存在: {experiment_id}")
        old_status = ExperimentStatus(row["status"])
        # C1 资金守恒红线 / 状态机红线：promote 仅允许 DRAFT→ACTIVE。
        # Why：_LEGAL_TRANSITIONS 同时含 (DRAFT,ACTIVE)=promote 与 (ARCHIVED,ACTIVE)=rollback，
        # 单独 validate_transition 无法区分两种语义。若不在此显式拒绝，已归档版本（ARCHIVED）
        # 会被 promote 当作 rollback 走通，绕开 rollback 的权重和校验（C2），构成资金守恒旁路。
        # ARCHIVED 的恢复必须走 rollback（含权重和校验），不可借 promote 绕过。
        if old_status != ExperimentStatus.DRAFT:
            raise ValueError(
                f"promote 仅 DRAFT→ACTIVE，当前 {old_status}"
                f"（ARCHIVED 请用 rollback，含权重和校验）"
            )
        if not validate_transition(old_status, ExperimentStatus.ACTIVE):
            raise ValueError(f"非法迁移: {old_status}→ACTIVE（experiment_id={experiment_id}）")
        if not validate_weight_sum(_active_versions(con), weight):
            raise ValueError(f"权重和超 1.0（资金守恒红线）：promote weight={weight} 被拒")
        con.execute(
            "UPDATE experiment_version SET status=?, weight=?, activated_at=? WHERE experiment_id=?",
            (ExperimentStatus.ACTIVE.value, weight, now, experiment_id))
        _write_audit(con, action="promote", experiment_id=experiment_id, operator=operator,
                     now=now, changed_fields={"status": [old_status.value, "ACTIVE"],
                                              "weight": [row["weight"], weight]})


def set_weight(db_path: str, experiment_id: str, new_weight: float, operator: str,
               now: str) -> None:
    """set_weight: ACTIVE 内调权重（不改 status）。校验：新权重 + 其他 ACTIVE ≤ 1.0。"""
    with _connect(db_path) as con:
        row = con.execute("SELECT * FROM experiment_version WHERE experiment_id=?",
                          (experiment_id,)).fetchone()
        if row is None:
            raise ValueError(f"实验版本不存在: {experiment_id}")
        if ExperimentStatus(row["status"]) != ExperimentStatus.ACTIVE:
            raise ValueError(f"set_weight 仅对 ACTIVE 有效（当前 {row['status']}）")
        # 校验时排除自身：其他 ACTIVE + new_weight ≤ 1.0
        others = [v for v in _active_versions(con) if v.experiment_id != experiment_id]
        if not validate_weight_sum(others, new_weight):
            raise ValueError(f"权重和超 1.0：set_weight={new_weight} 被拒")
        con.execute("UPDATE experiment_version SET weight=? WHERE experiment_id=?",
                    (new_weight, experiment_id))
        _write_audit(con, action="set-weight", experiment_id=experiment_id, operator=operator,
                     now=now, changed_fields={"weight": [row["weight"], new_weight]})


def archive(db_path: str, experiment_id: str, operator: str, now: str) -> None:
    """archive: ACTIVE→ARCHIVED。"""
    _transition(db_path, experiment_id, ExperimentStatus.ARCHIVED, operator, now, "archive")


def rollback(db_path: str, experiment_id: str, operator: str, now: str) -> None:
    """rollback: ARCHIVED→ACTIVE（恢复上线）。权重沿用归档前值，若冲突由调用方先调权重。"""
    _transition(db_path, experiment_id, ExperimentStatus.ACTIVE, operator, now, "rollback")


def _transition(db_path, experiment_id, target, operator, now, action) -> None:
    """通用状态迁移：校验合法性 → 更新 status + 时间戳 → 写审计。"""
    with _connect(db_path) as con:
        row = con.execute("SELECT * FROM experiment_version WHERE experiment_id=?",
                          (experiment_id,)).fetchone()
        if row is None:
            raise ValueError(f"实验版本不存在: {experiment_id}")
        old_status = ExperimentStatus(row["status"])
        if not validate_transition(old_status, target):
            raise ValueError(f"非法迁移: {old_status}→{target}（{action}）")
        # C2 资金守恒红线：rollback 目标 ACTIVE，须校验归档前 weight 加回后不超 1.0。
        # Why：_transition 原本只调 validate_transition（状态机）不调 validate_weight_sum（资金守恒），
        # 归档前 weight=0.4 的版本，在其他 ACTIVE 合计已达 1.0 时 rollback，
        # 总权重将变 1.4，实盘 budget=capital×pos_cap×weight 会超配真实资金。
        # 故在此处对 target=ACTIVE 的 rollback 显式加资金守恒校验。
        if target == ExperimentStatus.ACTIVE:
            if not validate_weight_sum(_active_versions(con), row["weight"]):
                raise ValueError(
                    f"rollback 后总权重将超 1.0（资金守恒）：当前 ACTIVE 合计 + "
                    f"{row['weight']} > 1.0，先调低其他 ACTIVE 权重再 rollback"
                )
        ts_col = {"ACTIVE": "activated_at", "ARCHIVED": "archived_at"}[target.value]
        con.execute(
            f"UPDATE experiment_version SET status=?, {ts_col}=? WHERE experiment_id=?",
            (target.value, now, experiment_id))
        _write_audit(con, action=action, experiment_id=experiment_id, operator=operator,
                     now=now, changed_fields={"status": [old_status.value, target.value]})


def list_versions(db_path: str, status: Optional[ExperimentStatus] = None) -> list:
    """列版本（可按 status 过滤）。"""
    with _connect(db_path) as con:
        if status is None:
            rows = con.execute("SELECT * FROM experiment_version ORDER BY created_at").fetchall()
        else:
            rows = con.execute("SELECT * FROM experiment_version WHERE status=? ORDER BY created_at",
                               (status.value,)).fetchall()
    return [_row_to_version(r) for r in rows]


def list_audit(db_path: str, experiment_id: Optional[str] = None) -> list:
    """列审计（可按 experiment_id 过滤）。"""
    with _connect(db_path) as con:
        if experiment_id is None:
            rows = con.execute("SELECT * FROM audit_log ORDER BY audit_id").fetchall()
        else:
            rows = con.execute("SELECT * FROM audit_log WHERE experiment_id=? ORDER BY audit_id",
                               (experiment_id,)).fetchall()
    return [AuditLog(timestamp=r["timestamp"], action=r["action"],
                     experiment_id=r["experiment_id"],
                     changed_fields=json.loads(r["changed_fields"] or "{}"),
                     operator=r["operator"] or "", note=r["note"] or "") for r in rows]
