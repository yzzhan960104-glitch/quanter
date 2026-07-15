# -*- coding: utf-8 -*-
"""caisen.training_loops_db 训练 loop 级状态表（Spec 3）。

物理定位：与 replay_tasks 同库（data/replay_tasks.db）不同表——loop 编排器每轮
把单轮回测当作 replay_tasks 的一行（提交+轮询+读 report），loop 自身的状态/累积
cfg/历史统计摘要存 training_loops 表。两表经 task_id/loop_id 解耦。

复用 replay_tasks_db 的连接范式（_connect WAL + Row 工厂）与时间戳工具，零重复：
    - _DEFAULT_DB_PATH / _connect / _now_iso 直接 import（同一物理库的连接/时钟一致）
    - _resolve 自定义但语义同 replay_tasks_db（None fallback 模块常量，让测试 monkeypatch 生效）
"""
from __future__ import annotations

import json
import uuid
from typing import Optional

from caisen.replay_tasks_db import _DEFAULT_DB_PATH, _connect, _now_iso


def _resolve(path: Optional[str]) -> str:
    """path=None → 读模块级 _DEFAULT_DB_PATH（monkeypatch 隔离生效，仿 replay_tasks_db）。

    之所以不用默认参数绑定 _DEFAULT_DB_PATH：Python 默认参数在 def 时一次性求值，
    测试 monkeypatch 模块常量后已绑定的默认值不会变，会污染生产库（详见 replay_tasks_db docstring）。
    """
    return _DEFAULT_DB_PATH if path is None else path


def init_db(path: Optional[str] = None) -> None:
    """建 training_loops 表 + 索引（幂等，IF NOT EXISTS）。

    与 replay_tasks 同库——两个 init_db 各自幂等，互不影响（CREATE TABLE IF NOT EXISTS）。
    status 列加索引：list_active_loops / list_loops(status=) 高频按状态过滤。
    created_at DESC 索引：列表降序展示最新 loop 在前。
    """
    path = _resolve(path)
    with _connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS training_loops (
                loop_id          TEXT PRIMARY KEY,
                created_at       TEXT NOT NULL,
                status           TEXT NOT NULL,
                current_round    INTEGER DEFAULT 0,
                max_rounds       INTEGER NOT NULL,
                start            TEXT,
                end              TEXT,
                universe_json    TEXT,
                base_cfg_json    TEXT,
                current_cfg_json TEXT,
                history_json     TEXT,
                pending_review   TEXT,
                error            TEXT,
                started_at       TEXT,
                finished_at      TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_loops_status ON training_loops(status);
            CREATE INDEX IF NOT EXISTS idx_loops_created ON training_loops(created_at DESC);
            """
        )


# 活跃 loop 状态集合（concurrency=1 守卫据此判定是否已有活跃 loop，正常 ≤1）。
# 不含 IDLE/STOPPED/COMPLETED（这些不算"在跑"）；不含 AWAITING_REVIEW 之外的等待态。
_ACTIVE_STATUSES = ("RUNNING", "ANALYZING", "AWAITING_REVIEW", "CONFIRMING")


def _row_to_dict(row) -> dict:
    """行字典化 + 反序列化 universe/base_cfg/current_cfg/history（None 透传，仿 replay_tasks_db）。

    universe：NULL/空 → None（全市场）；JSON 数组 → list。
    base_cfg/current_cfg：空 → {}（cfg 永远是 dict，避免下游 None 判空）。
    history：空 → []（追加轮次摘要的前置不变量）。
    """
    d = dict(row)
    raw_uni = d.get("universe_json")
    d["universe"] = json.loads(raw_uni) if raw_uni else None
    d["base_cfg"] = json.loads(d["base_cfg_json"]) if d.get("base_cfg_json") else {}
    d["current_cfg"] = json.loads(d["current_cfg_json"]) if d.get("current_cfg_json") else {}
    d["history"] = json.loads(d["history_json"]) if d.get("history_json") else []
    d.pop("universe_json", None)
    d.pop("base_cfg_json", None)
    d.pop("current_cfg_json", None)
    d.pop("history_json", None)
    return d


def create_loop(req: dict, path: Optional[str] = None) -> str:
    """生成 loop_id + 写 IDLE 行。初始 current_cfg = base_cfg（重置基准 = 提交时初始 cfg）。

    物理意图：每轮人审改的是 current_cfg（下一轮回测要用的参数），base_cfg 永久保留
    作为"回到原点"的基准——用户想放弃累积改动重置时，current_cfg ← base_cfg 即可。
    history 初始 '[]'，append_history 读改写追加。
    """
    path = _resolve(path)
    loop_id = uuid.uuid4().hex
    base_cfg = req.get("base_cfg") or {}
    created_at = _now_iso()
    with _connect(path) as conn:
        conn.execute(
            """INSERT INTO training_loops
               (loop_id, created_at, status, current_round, max_rounds, start, end,
                universe_json, base_cfg_json, current_cfg_json, history_json)
               VALUES (?, ?, 'IDLE', 0, ?, ?, ?, ?, ?, ?, '[]')""",
            (loop_id, created_at, req.get("max_rounds"), req.get("start"), req.get("end"),
             json.dumps(req.get("universe"), ensure_ascii=False),
             json.dumps(base_cfg, ensure_ascii=False),
             json.dumps(base_cfg, ensure_ascii=False)),
        )
    return loop_id


def get_loop(loop_id: str, path: Optional[str] = None) -> Optional[dict]:
    path = _resolve(path)
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT * FROM training_loops WHERE loop_id=?", (loop_id,)).fetchone()
    return _row_to_dict(row) if row else None


def list_loops(status: Optional[str] = None, limit: int = 100,
               path: Optional[str] = None) -> list[dict]:
    """按 created_at 降序；status=None 全量（仿 replay_tasks_db.list_tasks）。"""
    path = _resolve(path)
    with _connect(path) as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM training_loops WHERE status=? ORDER BY created_at DESC LIMIT ?",
                (status, limit)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM training_loops ORDER BY created_at DESC LIMIT ?",
                (limit,)).fetchall()
    return [_row_to_dict(r) for r in rows]


def list_active_loops(path: Optional[str] = None) -> list[dict]:
    """活跃 loop 列表（concurrency=1 守卫用；正常应 ≤1）。

    物理意图：编排器启动新 loop 前先查此列表，非空即拒绝（避免两个 loop 并发回测
    撞算力/撞库）。AWAITING_REVIEW 也算活跃——人审关卡期间不允许开新 loop。
    """
    path = _resolve(path)
    with _connect(path) as conn:
        placeholders = ",".join("?" * len(_ACTIVE_STATUSES))
        rows = conn.execute(
            f"SELECT * FROM training_loops WHERE status IN ({placeholders})",
            _ACTIVE_STATUSES).fetchall()
    return [_row_to_dict(r) for r in rows]


def update_loop(loop_id: str, path: Optional[str] = None, **fields) -> None:
    """部分更新 loop 行。

    物理意图：编排器每跨一个状态（IDLE→RUNNING→ANALYZING→AWAITING_REVIEW→…）和每轮
    累积 cfg 调整，都通过本函数落库——状态机推进的单一写入点。

    字段映射规则（白名单防 SQL 注入——列名不参数化，只允许字面列名进 SQL）：
        - current_cfg → current_cfg_json 列（JSON 序列化；唯一需要列名映射的字段）
        - pending_review/error → 同名列，str 直存（已是 JSON 字符串或纯文本），
          非 str 才 JSON 序列化（dict 等结构化待审信息也兼容）
        - status/current_round/started_at/finished_at → 同名列直存（标量）
    """
    if not fields:
        return
    allowed = {"status", "current_round", "current_cfg", "pending_review",
               "error", "started_at", "finished_at"}
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"非法字段：{bad}")
    path = _resolve(path)
    cols = []
    params = []
    for k, v in fields.items():
        if k == "current_cfg":
            cols.append("current_cfg_json=?")
            params.append(json.dumps(v, ensure_ascii=False))
        elif k in ("pending_review", "error"):
            cols.append(f"{k}=?")
            params.append(json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v)
        else:
            cols.append(f"{k}=?")
            params.append(v)
    params.append(loop_id)
    with _connect(path) as conn:
        conn.execute(f"UPDATE training_loops SET {', '.join(cols)} WHERE loop_id=?", params)


def append_history(loop_id: str, round_summary: dict, path: Optional[str] = None) -> None:
    """追加一轮统计摘要到 history_json（读改写，主进程单点写无并发）。

    物理意图：history 每轮只存统计摘要（~6 字段：win_rate/avg_rr/max_dd/...），
    喂 GLM 做多轮趋势分析（"参数越调越激进，回撤在放大"），不带完整 trades 控量。
    单 loop 不存在跨进程并发写，故读改写足够（无需 CAS/事务锁）。
    """
    path = _resolve(path)
    loop = get_loop(loop_id, path)
    if loop is None:
        return
    history = loop["history"]
    history.append(round_summary)
    with _connect(path) as conn:
        conn.execute("UPDATE training_loops SET history_json=? WHERE loop_id=?",
                     (json.dumps(history, ensure_ascii=False), loop_id))


def reset_interrupted(path: Optional[str] = None) -> int:
    """重启恢复：RUNNING/ANALYZING 残留 → STOPPED + error（不自动续跑，仿 replay_tasks_db）。

    物理意图：uvicorn 重启时上一轮卡在 RUNNING（回测中）/ANALYZING（GLM 解析中）的 loop
    无法继续，标 STOPPED + 错因——避免无意识重复消耗算力或状态悬挂（与 replay_tasks_db
    reset_running_to_failed 同一语义，只是 loop 表用 STOPPED 表达"非正常终止"）。

    AWAITING_REVIEW 不重置——人审关卡可跨重启保留（你重启回来还能继续审核那一轮，
    不丢失已花掉的回测+解析算力）。返回被重置的行数。
    """
    path = _resolve(path)
    with _connect(path) as conn:
        cur = conn.execute(
            "UPDATE training_loops SET status='STOPPED', error='进程重启中断', "
            "finished_at=? WHERE status IN ('RUNNING','ANALYZING')",
            (_now_iso(),))
    return cur.rowcount
