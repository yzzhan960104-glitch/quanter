# 蔡森 AI 人审训练 Loop 实现计划（参数训练平台 · Spec 3）

> **For agentic workers:** REQUIRED SUB-SKILL: 用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现。步骤用 `- [ ]` 复选框跟踪。

**Goal:** 在参数训练平台中实现「人审闭环训练」——提交训练任务后，caisen 后台自动连续跑「回测→AI 分析→钉钉推报告→手机审核→据审核调参续跑下一轮」，每轮卡一个钉钉人审关卡，N 轮或人工喊停为止。

**Architecture:** 全寄生 uvicorn 主进程。新增 4 个组件：(1) `training_loops_db`——loop 级状态表（复用 `data/replay_tasks.db` 同库新建 `training_loops` 表）；(2) `training_analyzer`——AI 分析/解析（复用 `review_service._call_glm`，零新依赖）；(3) `training_loop`——状态机编排器（daemon 线程，concurrency=1，每轮通过 `replay_tasks_db.create_task` 提交回测 + 轮询 `get_task` 等终态）；(4) `training_dingtalk`——参数审查机器人（stream ChatbotHandler 收审核 + access_token batch API 主动推报告）。钉钉审核回调进程内唤醒 loop（不走外部 HTTP）。

**Tech Stack:** Python 3（uvicorn/FastAPI）、SQLite（WAL）、智谱 GLM（urllib 调用）、钉钉 dingtalk-stream SDK + 开放平台 batch send API、pytest。

---

## ⚠️ 全局约束（spec §7 落地细化 · 每个任务隐含遵守）

1. **钉钉推送机制（spec §7 落地；用户 2026-07-15 提供独立凭证后微调）**：`dingtalk-stream` 的 `ChatbotHandler.reply_text` **只能 @回复 incoming 消息，不能主动推送**（web search 核实）。用户为 spec3 建了**独立第二企业内部应用**（`dingbabujxcelmssmdpn`）+ **独立群自定义机器人 webhook**（`575d...`，与 bridge 物理隔离）。落地方案为**双通道**：
   - **收审核**：`dingtalk-stream` `ChatbotHandler`，用 `REVIEW_APP_KEY/SECRET` 建独立 stream（仿 `bridge/stream_client.py` 的 `BridgeHandler`），白名单 `REVIEW_ALLOWED_STAFF_IDS`（复用 bridge 同一 staffId）。
   - **主动推报告/回显**：用 `REVIEW_WEBHOOK`（群自定义机器人 webhook，`oapi.dingtalk.com/robot/send`）POST markdown。比 batch send 更简——webhook 自带 access_token 无需换取；加签复用 `core/notifier.py:DingTalkChannel._sign`（`REVIEW_WEBHOOK_SECRET` 非空时），errcode 业务态校验复用 `DingTalkChannel._validate_response`（HTTP 200 + errcode≠0 才是真失败）。
   - **凭证隔离**：`REVIEW_APP_KEY/SECRET`（stream 收）+ `REVIEW_WEBHOOK`/`REVIEW_WEBHOOK_SECRET`（webhook 推）+ `REVIEW_ALLOWED_STAFF_IDS`（白名单），全部与 bridge 的 `DINGTALK_*` 物理隔离。已配进 `.env`（git 忽略）。
   - **加签 secret 待补**：用户 webhook 未带 secret，`REVIEW_WEBHOOK_SECRET` 暂留空。Task4 推送实现「secret 非空才加签」；裸发时推送 body 固定含【训练】关键词（防机器人开了「关键词」安全设置）。若联调报 `errcode:310000 sign not match` → 补 secret；`300001 keywords` → 调整关键词。
   - **plan Task4 原文写的是 batch send**（早期方案），**实际实现改 webhook 推**（派发 Task4 时按本条纠偏）。

2. **语言红线**：所有对话/注释/文档全中文；代码注释讲「为什么」不只讲「是什么」。

3. **极简红线**：AI 调用用 `urllib`（复用 `review_service._call_glm`），钉钉 HTTP 也用 `urllib`，**零新 HTTP 依赖**；不引 openai/langchain 黑盒。

4. **回测复用红线**：loop 编排器**不自己跑回测**，每轮通过 `replay_tasks_db.create_task({...})` 写 PENDING 行 + 轮询 `replay_tasks_db.get_task(task_id)` 等终态，复用 Spec1 的 `ReplayScheduler` + `ProcessPoolExecutor`（concurrency=1 串行，loop 与普通 lab 任务共享同一 worker 队列）。

5. **concurrency=1**：同一时刻只允许一个活跃 loop（状态 ∈ RUNNING/ANALYZING/AWAITING_REVIEW/CONFIRMING）。提交第二个时 `/training/start` 返 422。

6. **重启恢复**：uvicorn 启动时把 `training_loops` 中 `status IN ('RUNNING','ANALYZING')` 的残留 loop 标 `STOPPED` + `error='进程重启中断'`（不自动续跑，仿 `replay_tasks_db.reset_running_to_failed`）。

---

## 文件结构

**新建：**

| 文件 | 职责 |
|---|---|
| `caisen/training_loops_db.py` | `training_loops` 表 CRUD + 重启恢复（仿 `caisen/replay_tasks_db.py`，复用其 `_DEFAULT_DB_PATH` 同库） |
| `caisen/training_analyzer.py` | `analyze_round(report, cfg, history) -> str` + `parse_review(text, cfg) -> dict`（复用 `review_service._call_glm`，三级降级） |
| `caisen/training_loop.py` | `TrainingLoopOrchestrator`——状态机 daemon 线程 + 回测复用 + 注入 notifier 接口 |
| `caisen/training_dingtalk.py` | `DingTalkNotifier`（access_token batch send 主动推）+ `ReviewChatbotHandler`（stream 收审核回调 orchestrator）+ `start_review_bot(app)`（lifespan 装配入口） |
| `server/api/v1/training.py` | 4 端点 router（`/training/start`、`/training/{loop_id}`、`/training/{loop_id}/stop`、`/training`） |
| `server/schemas/training.py` | `TrainingStartRequest`、`TrainingLoopState`、`RoundSummary` Pydantic 契约 |
| `tests/caisen/test_training_loops_db.py` | 表 CRUD + 重启恢复单测 |
| `tests/caisen/test_training_analyzer.py` | 分析/解析 + 降级 + 值域护栏单测（mock `_call_glm`） |
| `tests/caisen/test_training_loop.py` | 状态机全路径 + 回测复用 + cfg 累积单测（mock 回测/分析/notifier） |
| `tests/caisen/test_training_dingtalk.py` | 推送/接收/白名单单测（mock urllib + SDK） |
| `tests/test_training_api.py` | 4 端点集成测试（TestClient） |

**修改：**

| 文件 | 改动 |
|---|---|
| `server/main.py` | lifespan 起 `start_review_bot(app)`（async task）+ `app.include_router(training_router, ...)`（仿 `caisen_router` 注册行 `main.py:218`）+ 启动恢复 `training_loops` |

---

## Task 1: training_loops_db（表 + CRUD + 重启恢复）

**Files:**
- Create: `caisen/training_loops_db.py`
- Test: `tests/caisen/test_training_loops_db.py`

**Interfaces:**
- Consumes: `caisen.replay_tasks_db._DEFAULT_DB_PATH`（同库不同表）、`caisen.replay_tasks_db._connect`/`_now_iso`（复用连接范式）
- Produces: `create_loop(req:dict)->str`、`get_loop(loop_id)->dict|None`、`list_loops(status=None,limit=100)->list[dict]`、`list_active_loops()->list[dict]`、`update_loop(loop_id, **fields)->None`、`append_history(loop_id, round_summary:dict)->None`、`reset_interrupted()->int`

- [ ] **Step 1: 写失败测试（建表 + 基本 CRUD）**

`tests/caisen/test_training_loops_db.py`：
```python
# -*- coding: utf-8 -*-
"""training_loops 表 CRUD + 重启恢复单测。

用 tmp_path 隔离 DB（monkeypatch _DEFAULT_DB_PATH），不污染生产 data/replay_tasks.db。
"""
import json
from pathlib import Path

from caisen import training_loops_db


def _use_tmp_db(monkeypatch, tmp_path):
    """把模块级默认 DB 路径指向 tmp，生产 DB 不受影响。"""
    db = str(tmp_path / "test_loops.db")
    monkeypatch.setattr(training_loops_db, "_DEFAULT_DB_PATH", db)
    # _resolve 优先参数 path；None 时才回退模块级常量，故 monkeypatch 模块常量即可
    return db


def test_create_and_get_loop(monkeypatch, tmp_path):
    _use_tmp_db(monkeypatch, tmp_path)
    training_loops_db.init_db()
    loop_id = training_loops_db.create_loop({
        "start": "2020-01-01", "end": "2024-12-31",
        "universe": ["000001.SZ"], "base_cfg": {"min_rr_ratio": 1.5},
        "max_rounds": 5,
    })
    loop = training_loops_db.get_loop(loop_id)
    assert loop is not None
    assert loop["status"] == "IDLE"
    assert loop["max_rounds"] == 5
    assert loop["current_round"] == 0
    assert loop["base_cfg"] == {"min_rr_ratio": 1.5}
    assert loop["current_cfg"] == {"min_rr_ratio": 1.5}   # 初始 current = base
    assert loop["history"] == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/caisen/test_training_loops_db.py::test_create_and_get_loop -x`
Expected: FAIL（`ModuleNotFoundError: No module named 'caisen.training_loops_db'`）

- [ ] **Step 3: 实现 training_loops_db.py（建表 + 基本 CRUD）**

`caisen/training_loops_db.py`：
```python
# -*- coding: utf-8 -*-
"""caisen.training_loops_db 训练 loop 级状态表（Spec 3）。

物理定位：与 replay_tasks 同库（data/replay_tasks.db）不同表——loop 编排器每轮
把单轮回测当作 replay_tasks 的一行（提交+轮询+读 report），loop 自身的状态/累积
cfg/历史统计摘要存 training_loops 表。两表经 task_id/loop_id 解耦。

复用 replay_tasks_db 的连接范式（_connect WAL + Row 工厂）与时间戳工具，零重复。
"""
from __future__ import annotations

import json
import os
import uuid
from typing import Optional

from caisen.replay_tasks_db import _DEFAULT_DB_PATH, _connect, _now_iso


def _resolve(path: Optional[str]) -> str:
    """path=None → 读模块级 _DEFAULT_DB_PATH（monkeypatch 隔离生效，仿 replay_tasks_db）。"""
    return _DEFAULT_DB_PATH if path is None else path


def init_db(path: Optional[str] = None) -> None:
    """建 training_loops 表 + 索引（幂等）。与 replay_tasks 同库（init_db 两者各自幂等）。"""
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


# 活跃 loop 状态集合（concurrency=1 守卫据此判定是否已有活跃 loop）
_ACTIVE_STATUSES = ("RUNNING", "ANALYZING", "AWAITING_REVIEW", "CONFIRMING")


def _row_to_dict(row) -> dict:
    """行字典化 + 反序列化 universe/cfg/history（None 透传，仿 replay_tasks_db._row_to_dict）。"""
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
    """生成 loop_id + 写 IDLE 行。初始 current_cfg = base_cfg（重置基准 = 提交时初始 cfg）。"""
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
    """活跃 loop 列表（concurrency=1 守卫用；正常应 ≤1）。"""
    path = _resolve(path)
    with _connect(path) as conn:
        placeholders = ",".join("?" * len(_ACTIVE_STATUSES))
        rows = conn.execute(
            f"SELECT * FROM training_loops WHERE status IN ({placeholders})",
            _ACTIVE_STATUSES).fetchall()
    return [_row_to_dict(r) for r in rows]
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/caisen/test_training_loops_db.py::test_create_and_get_loop -x`
Expected: PASS

- [ ] **Step 5: 写失败测试（update_loop + append_history + 重启恢复 + 并发守卫）**

追加到 `tests/caisen/test_training_loops_db.py`：
```python
def test_update_loop_and_append_history(monkeypatch, tmp_path):
    _use_tmp_db(monkeypatch, tmp_path)
    training_loops_db.init_db()
    loop_id = training_loops_db.create_loop(
        {"start": "2020-01-01", "end": "2024-12-31", "universe": None,
         "base_cfg": {}, "max_rounds": 3})
    # update_loop：改状态/轮次/当前 cfg/待审信息
    training_loops_db.update_loop(loop_id, status="RUNNING", current_round=1,
                                  current_cfg={"min_rr_ratio": 2.0})
    training_loops_db.append_history(loop_id, {"round": 1, "n_hits": 10, "win_rate": 0.6,
                                               "avg_rr": 1.8, "max_dd": -0.12,
                                               "annualized": 0.25})
    loop = training_loops_db.get_loop(loop_id)
    assert loop["status"] == "RUNNING"
    assert loop["current_round"] == 1
    assert loop["current_cfg"] == {"min_rr_ratio": 2.0}
    assert len(loop["history"]) == 1
    assert loop["history"][0]["round"] == 1


def test_list_active_loops_concurrency_guard(monkeypatch, tmp_path):
    _use_tmp_db(monkeypatch, tmp_path)
    training_loops_db.init_db()
    a = training_loops_db.create_loop(
        {"start": "2020-01-01", "end": "2024-12-31", "base_cfg": {}, "max_rounds": 3})
    b = training_loops_db.create_loop(
        {"start": "2020-01-01", "end": "2024-12-31", "base_cfg": {}, "max_rounds": 3})
    assert training_loops_db.list_active_loops() == []   # 两个都 IDLE
    training_loops_db.update_loop(a, status="AWAITING_REVIEW")
    active = training_loops_db.list_active_loops()
    assert len(active) == 1 and active[0]["loop_id"] == a


def test_reset_interrupted(monkeypatch, tmp_path):
    """重启恢复：RUNNING/ANALYZING 残留 → STOPPED；AWAITING_REVIEW/STOPPED 不动。"""
    _use_tmp_db(monkeypatch, tmp_path)
    training_loops_db.init_db()
    running = training_loops_db.create_loop(
        {"start": "2020-01-01", "end": "2024-12-31", "base_cfg": {}, "max_rounds": 3})
    analyzing = training_loops_db.create_loop(
        {"start": "2020-01-01", "end": "2024-12-31", "base_cfg": {}, "max_rounds": 3})
    awaiting = training_loops_db.create_loop(
        {"start": "2020-01-01", "end": "2024-12-31", "base_cfg": {}, "max_rounds": 3})
    training_loops_db.update_loop(running, status="RUNNING")
    training_loops_db.update_loop(analyzing, status="ANALYZING")
    training_loops_db.update_loop(awaiting, status="AWAITING_REVIEW")
    n = training_loops_db.reset_interrupted()
    assert n == 2   # 仅 RUNNING/ANALYZING 被重置
    assert training_loops_db.get_loop(running)["status"] == "STOPPED"
    assert training_loops_db.get_loop(running)["error"] == "进程重启中断"
    assert training_loops_db.get_loop(awaiting)["status"] == "AWAITING_REVIEW"  # 不动
```

- [ ] **Step 6: 跑测试确认失败**

Run: `python -m pytest tests/caisen/test_training_loops_db.py -x`
Expected: FAIL（`update_loop`/`append_history`/`reset_interrupted` 未定义）

- [ ] **Step 7: 实现 update_loop / append_history / reset_interrupted**

追加到 `caisen/training_loops_db.py`：
```python
# 字段名 → 列名映射（current_cfg/history 需 JSON 序列化，其余直存）
_JSON_FIELDS = {"current_cfg", "pending_review", "error"}


def update_loop(loop_id: str, path: Optional[str] = None, **fields) -> None:
    """部分更新 loop 行。current_cfg 走 JSON 序列化；status/current_round 等直存。

    仅允许更新白名单字段（防 SQL 注入——列名不参数化）。
    """
    if not fields:
        return
    allowed = {"status", "current_round", "current_cfg", "pending_review",
               "error", "started_at", "finished_at"}
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"非法字段：{bad}")
    path = _resolve(path)
    sets, vals = [], []
    for k, v in fields.items():
        if k in _JSON_FIELDS and v is not None:
            sets.append(f"{k}_json=?") if False else None   # current_cfg_json 列名特例
    # current_cfg → current_cfg_json 列（唯一需要映射的 JSON 字段）
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

    history 每轮只存统计摘要（~6 字段），喂 GLM 做趋势分析，不带完整 trades。
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

    AWAITING_REVIEW 不重置——人审关卡可跨重启保留（你回来还能审核）。
    """
    path = _resolve(path)
    with _connect(path) as conn:
        cur = conn.execute(
            "UPDATE training_loops SET status='STOPPED', error='进程重启中断', "
            "finished_at=? WHERE status IN ('RUNNING','ANALYZING')",
            (_now_iso(),))
    return cur.rowcount
```

> 注：`update_loop` 内 `sets/vals` 两行是早期草稿残留，实现时删掉那两行 `sets=[]` 无用赋值（保留 `cols/params` 逻辑）——实现者按上面 `cols/params` 分支写，删除 `_JSON_FIELDS` 与 `sets/vals` 死代码。

- [ ] **Step 8: 跑全部测试确认通过**

Run: `python -m pytest tests/caisen/test_training_loops_db.py -v`
Expected: 4 PASS

- [ ] **Step 9: Commit**

```bash
git add caisen/training_loops_db.py tests/caisen/test_training_loops_db.py
git commit -m "feat(training): Task1 training_loops 表 CRUD+重启恢复(复用 replay_tasks 同库)"
```

---

## Task 2: training_analyzer（AI 分析 + 解析 + 值域护栏）

**Files:**
- Create: `caisen/training_analyzer.py`
- Test: `tests/caisen/test_training_analyzer.py`

**Interfaces:**
- Consumes: `server.services.review_service._call_glm(prompt, api_key, model, timeout)`（直接 import 复用，零新依赖）、`caisen.config.StrategyConfig`（字段 schema 护栏）
- Produces: `analyze_round(report:dict, cfg:dict, history:list[dict]) -> str`（Markdown 报告）、`parse_review(text:str, cfg:dict) -> dict`（`{cfg_override:dict, action:"rerun"|"stop"|"reset"}`）、`ParseError`（解析失败/非法字段异常）

- [ ] **Step 1: 写失败测试（analyze_round：mock _call_glm + 降级）**

`tests/caisen/test_training_analyzer.py`：
```python
# -*- coding: utf-8 -*-
"""training_analyzer 分析/解析单测。mock _call_glm 不真调 GLM。"""
from unittest.mock import patch

from caisen import training_analyzer


_REPORT = {"n_hits": 12, "win_rate": 0.58, "avg_rr": 1.7, "max_drawdown": -0.14,
           "annualized_return": 0.22, "pattern_dist": {"w_bottom": 8}}
_CFG = {"min_rr_ratio": 1.5, "max_holding_bars": 15}


def test_analyze_round_assembles_prompt_and_returns_report():
    """正常路径：_call_glm 被调一次，入参含当前轮统计+历史，返回模型文本。"""
    with patch.object(training_analyzer, "_call_glm", return_value="## 第1轮报告\n表现尚可") as m:
        report = training_analyzer.analyze_round(_REPORT, _CFG, [])
    assert "第1轮报告" in report
    m.assert_called_once()
    prompt = m.call_args.args[0]
    assert "12" in prompt          # 当前轮 n_hits 进 prompt
    assert "min_rr_ratio" in prompt  # 当前 cfg 进 prompt


def test_analyze_round_degrades_without_glm_key(monkeypatch):
    """缺 GLM 凭证 → 降级返回带统计摘要的提示文本（不抛异常）。"""
    monkeypatch.delenv("GLM_API_KEY", raising=False)
    monkeypatch.delenv("ZHIPU_API_KEY", raising=False)
    out = training_analyzer.analyze_round(_REPORT, _CFG, [])
    assert "降级" in out or "AI 不可用" in out
    assert "12" in out   # 仍附原始统计供人手判断
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/caisen/test_training_analyzer.py::test_analyze_round_assembles_prompt_and_returns_report -x`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现 analyze_round（复用 review_service._call_glm + 三级降级）**

`caisen/training_analyzer.py`：
```python
# -*- coding: utf-8 -*-
"""caisen.training_analyzer 训练 loop 的 AI 分析/解析（Spec 3 §6）。

零新依赖：复用 server.services.review_service._call_glm（urllib 调 GLM，三级降级范式）。
- analyze_round：当前轮统计 + 当前 cfg + 历史几轮摘要 → GLM → 自然语言 Markdown 报告。
- parse_review：你的审核文本 + 当前 cfg + 字段 schema → GLM → {cfg_override, action}。
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List

# 复用 review_service 的 GLM 调用（urllib，零新依赖）——同进程 import 安全（无循环）。
from server.services.review_service import _call_glm

logger = logging.getLogger(__name__)

_LLM_TIMEOUT = 60


def _stats_block(report: Dict[str, Any]) -> str:
    """从 ReplayReport dict 抽关键统计摘要（喂 GLM 看趋势，不带完整 trades 撑爆 context）。"""
    fields = ("n_hits", "win_rate", "avg_rr", "max_drawdown", "annualized_return",
              "avg_holding_bars", "pattern_dist")
    return json.dumps({k: report.get(k) for k in fields if k in report},
                      ensure_ascii=False, default=str)


def analyze_round(report: Dict[str, Any], cfg: Dict[str, Any],
                  history: List[Dict[str, Any]]) -> str:
    """分析单轮回测 → Markdown 报告（表现评估/问题诊断/调参建议）。

    LLM 不可用（缺凭证/调用失败）→ 降级返回「AI 不可用 + 附原始统计」文本，不抛异常。
    """
    stats = _stats_block(report)
    cfg_str = json.dumps(cfg, ensure_ascii=False, default=str)
    history_str = json.dumps(history[-5:], ensure_ascii=False, default=str)  # 最近 5 轮看趋势
    prompt = f"""你是一位资深量化策略研究员。请基于蔡森形态学策略本轮回测结果与历史趋势，输出 Markdown 训练报告。

## 当前轮回测统计
{stats}

## 当前生效参数（cfg）
{cfg_str}

## 历史轮次统计摘要（看趋势，最近 5 轮）
{history_str}

## 输出要求（严格 Markdown 三段）
### 1. 本轮表现评估
（胜率/盈亏比/回撤是否健康，对比历史趋势是改善还是退化）

### 2. 问题诊断
（亏损来源：哪种形态/哪个参数导致？样本是否足够？）

### 3. 下轮调参建议
（给出具体字段+数值方向，如「min_rr_ratio 提到 2.0」「max_holding_bars 放宽到 20」，但不要给死命令，由人审决定）

请直接输出报告正文。"""
    api_key = os.getenv("GLM_API_KEY") or os.getenv("ZHIPU_API_KEY")
    if not api_key:
        logger.info("GLM 凭证未配置，analyze_round 走降级（附原始统计）")
        return (f"## ⚠️ AI 分析降级（GLM 凭证未配置）\n\n附本轮原始统计供人手判断：\n\n```\n{stats}\n```")
    try:
        return _call_glm(prompt, api_key, os.getenv("GLM_MODEL", "glm-4"), _LLM_TIMEOUT)
    except Exception as exc:
        logger.warning("analyze_round GLM 调用失败，降级：%s", exc)
        return (f"## ⚠️ AI 分析降级（GLM 调用失败：{type(exc).__name__}）\n\n"
                f"附本轮原始统计供人手判断：\n\n```\n{stats}\n```")
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/caisen/test_training_analyzer.py -k analyze_round -v`
Expected: 2 PASS

- [ ] **Step 5: 写失败测试（parse_review + 值域护栏）**

追加到 `tests/caisen/test_training_analyzer.py`：
```python
def test_parse_review_extracts_cfg_override_and_action():
    """正常：GLM 返回合法 JSON → 解析出 cfg_override + action=rerun。"""
    glm_out = json.dumps({"cfg_override": {"min_rr_ratio": 2.0}, "action": "rerun"})
    with patch.object(training_analyzer, "_call_glm", return_value=glm_out):
        result = training_analyzer.parse_review("min_rr 提到2.0 重跑", _CFG)
    assert result["action"] == "rerun"
    assert result["cfg_override"] == {"min_rr_ratio": 2.0}


def test_parse_review_rejects_invalid_field():
    """值域护栏：cfg_override 含非法字段名 → 抛 ParseError（防 GLM 幻觉改不存在的字段）。"""
    glm_out = json.dumps({"cfg_override": {"not_a_real_field": 1.0}, "action": "rerun"})
    with patch.object(training_analyzer, "_call_glm", return_value=glm_out):
        try:
            training_analyzer.parse_review("改某字段", _CFG)
            assert False, "应抛 ParseError"
        except training_analyzer.ParseError as e:
            assert "not_a_real_field" in str(e) or "非法" in str(e)


def test_parse_review_rejects_out_of_range():
    """值域护栏：min_rr_ratio 超出 schema 约束 → 抛 ParseError（model_copy 校验）。"""
    # min_rr_ratio 无 ge/le 约束，换有约束的字段：neckline_height_multiple ge=1 le=4
    glm_out = json.dumps({"cfg_override": {"neckline_height_multiple": 99}, "action": "rerun"})
    with patch.object(training_analyzer, "_call_glm", return_value=glm_out):
        try:
            training_analyzer.parse_review("级数改99", _CFG)
            assert False, "应抛 ParseError（超 le=4）"
        except training_analyzer.ParseError:
            pass


def test_parse_review_degrades_on_bad_json():
    """GLM 返回非 JSON → 降级抛 ParseError（loop 据此回显「没听懂」回 AWAITING_REVIEW）。"""
    with patch.object(training_analyzer, "_call_glm", return_value="这不是JSON"):
        try:
            training_analyzer.parse_review("说点啥", _CFG)
            assert False
        except training_analyzer.ParseError:
            pass
```

- [ ] **Step 6: 跑测试确认失败**

Run: `python -m pytest tests/caisen/test_training_analyzer.py -k parse_review -x`
Expected: FAIL（`parse_review`/`ParseError` 未定义）

- [ ] **Step 7: 实现 parse_review + 值域护栏（model_copy 校验）**

追加到 `caisen/training_analyzer.py`：
```python
from pydantic import ValidationError

from caisen.config import StrategyConfig


class ParseError(Exception):
    """审核文本解析失败（GLM 返回非 JSON / 字段非法 / 超值域）。

    loop 据此回显报错并回 AWAITING_REVIEW 重等你审核。
    """


# 合法 action 白名单（防 GLM 幻觉造动作）
_ACTIONS = ("rerun", "stop", "reset")


def parse_review(text: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """解析你的审核文本 → {cfg_override, action}。

    值域护栏：cfg_override 经 StrategyConfig.model_copy(update=...) 校验——
    非法字段名/超 ge/le 抛 ParseError（防 GLM 改不存在的字段或给越界值）。
    GLM 不可用 → 降级抛 ParseError（message 含「请按 改 字段=值 重跑 格式」提示）。
    """
    prompt = f"""你是参数解析器。把用户的中文审核意图解析为严格 JSON。

## 当前生效参数（cfg，含所有合法字段名与当前值）
{json.dumps(cfg, ensure_ascii=False, default=str)}

## 合法字段名清单（只能改这些字段）
{', '.join(StrategyConfig.model_fields.keys())}

## 用户审核文本
{text}

## 输出要求（只输出 JSON，不要任何解释）
{{"cfg_override": {{字段名: 新值}}, "action": "rerun"}}

规则：
- cfg_override 只能含上面合法字段名；不改的字段不要出现在 cfg_override 里。
- action 只能是 "rerun"（改参重跑）、"stop"（停止训练）、"reset"（重置回基准 cfg 重跑）。
- 若用户只说停止，cfg_override 给空 {{}}。"""
    api_key = os.getenv("GLM_API_KEY") or os.getenv("ZHIPU_API_KEY")
    if not api_key:
        raise ParseError("GLM 凭证未配置，请按 `改 字段=值 重跑` 格式手动说明。")
    try:
        raw = _call_glm(prompt, api_key, os.getenv("GLM_MODEL", "glm-4"), _LLM_TIMEOUT)
    except Exception as exc:
        raise ParseError(f"GLM 调用失败：{type(exc).__name__}") from exc

    # 1) 解析 JSON（容错：剥可能的 ```json 代码块围栏）
    raw_stripped = raw.strip().strip("`")
    if raw_stripped.lower().startswith("json"):
        raw_stripped = raw_stripped[4:].strip()
    try:
        parsed = json.loads(raw_stripped)
    except json.JSONDecodeError as exc:
        raise ParseError(f"GLM 未返回合法 JSON（{exc.msg}），请重新说明审核意图。") from exc

    action = parsed.get("action", "rerun")
    if action not in _ACTIONS:
        raise ParseError(f"非法 action={action}（合法：rerun/stop/reset）。")

    cfg_override = parsed.get("cfg_override") or {}
    if not isinstance(cfg_override, dict):
        raise ParseError("cfg_override 必须是字段对象。")

    # 2) 值域护栏：经 model_copy 校验（非法字段名/超 ge/le → ValidationError → ParseError）。
    #    先校验字段名是否属于 StrategyConfig（防幻觉改不存在的字段）。
    valid_fields = set(StrategyConfig.model_fields.keys())
    illegal = set(cfg_override) - valid_fields
    if illegal:
        raise ParseError(f"非法字段（不存在于 StrategyConfig）：{illegal}")
    try:
        StrategyConfig(**{**cfg, **cfg_override})   # 合并后整体校验（含 ge/le）
    except ValidationError as exc:
        raise ParseError(f"cfg_override 值域非法：{exc}") from exc

    return {"cfg_override": cfg_override, "action": action}
```

- [ ] **Step 8: 跑全部测试确认通过**

Run: `python -m pytest tests/caisen/test_training_analyzer.py -v`
Expected: 6 PASS

- [ ] **Step 9: Commit**

```bash
git add caisen/training_analyzer.py tests/caisen/test_training_analyzer.py
git commit -m "feat(training): Task2 training_analyzer 分析/解析+值域护栏(复用 _call_glm)"
```

---

## Task 3: training_loop 状态机（纯逻辑 + 回测复用 + 注入 notifier）

**Files:**
- Create: `caisen/training_loop.py`
- Test: `tests/caisen/test_training_loop.py`

**Interfaces:**
- Consumes: `training_loops_db`（状态持久化）、`replay_tasks_db`（提交回测 + 轮询等终态）、`training_analyzer.analyze_round`/`parse_review`、`TrainingNotifier`（注入的推送接口，Task 4 实现）
- Produces: `TrainingNotifier`（Protocol 抽象：`push(loop_id, text)`、`reply_and_record(...)`）、`TrainingLoopOrchestrator`（`start(req)->loop_id`、`stop(loop_id)`、`submit_review(loop_id, text)`、`start_daemon()`/`stop_daemon()`）

**设计要点（拷问三连已处置）：**
- **回测复用**：`_run_one_round()` 调 `replay_tasks_db.create_task({start,end,universe,cfg_override=current_cfg})` 写 PENDING → 轮询 `get_task` 等终态（带 stop 检查，避免卡死）。**不碰 ProcessPoolExecutor**——Spec1 的 `ReplayScheduler` 会 poll 到该 PENDING 并派发。
- **notifier 注入**：状态机只依赖 `TrainingNotifier` Protocol（`push`/`reply`），Task 4 实现钉钉版，Task 3 测试注入 fake notifier。解耦状态机与网络。
- **concurrency=1**：`start()` 前查 `list_active_loops()`，非空则抛 `LoopBusyError`（→ API 422）。
- **审核唤醒**：`submit_review(loop_id, text)` 由钉钉 handler 调用，驱动 CONFIRMING 流程。用 `threading.Event` 解除 AWAITING_REVIEW 阻塞。

- [ ] **Step 1: 写失败测试（IDLE→RUNNING→ANALYZING→AWAITING_REVIEW，回测复用 + notifier 推送）**

`tests/caisen/test_training_loop.py`：
```python
# -*- coding: utf-8 -*-
"""training_loop 状态机单测。mock 回测 DB + analyzer + notifier，不碰真网络/真回测。"""
import json
from unittest.mock import patch

import pytest

from caisen import training_loop
from caisen.training_loop import TrainingLoopOrchestrator, LoopBusyError


class FakeNotifier:
    """记录推送/回显，便于断言 loop 在正确时机调了 notifier。"""
    def __init__(self):
        self.pushed = []   # [(loop_id, text)]
    def push(self, loop_id, text):
        self.pushed.append((loop_id, text))


@pytest.fixture
def orch(monkeypatch, tmp_path):
    """装配一个用 tmp DB + fake notifier 的编排器。"""
    db = str(tmp_path / "loops.db")
    monkeypatch.setattr(training_loop.training_loops_db, "_DEFAULT_DB_PATH", db)
    training_loop.training_loops_db.init_db()
    monkeypatch.setattr(training_loop.replay_tasks_db, "_DEFAULT_DB_PATH",
                        str(tmp_path / "replay.db"))
    training_loop.replay_tasks_db.init_db()
    notifier = FakeNotifier()
    o = TrainingLoopOrchestrator(notifier)
    return o, notifier


def test_start_runs_round_then_awaits_review(orch, monkeypatch):
    """核心动线：start → 提交回测 → 轮询到 SUCCESS → analyze → AWAITING_REVIEW + 推报告。

    用 _step_once 手动推进状态机（不起 daemon 线程），可控可测。
    """
    o, notifier = orch
    loop_id = o.start({"start": "2020-01-01", "end": "2024-12-31", "universe": None,
                       "base_cfg": {"min_rr_ratio": 1.5}, "max_rounds": 3})
    # mock：提交回测后立刻把它标 SUCCESS + 写 report
    def fake_get_task(task_id, path=None):
        return {"task_id": task_id, "status": "SUCCESS",
                "report": {"n_hits": 10, "win_rate": 0.6, "avg_rr": 1.8,
                           "max_drawdown": -0.1, "annualized_return": 0.2,
                           "pattern_dist": {}, "trades": []}}
    monkeypatch.setattr(training_loop.replay_tasks_db, "get_task", fake_get_task)
    monkeypatch.setattr(training_loop.training_analyzer, "analyze_round",
                        lambda r, c, h: "## 报告：表现尚可")

    o._step_once(loop_id)   # RUNNING：提交回测 + 轮询到 SUCCESS → ANALYZING → AWAITING_REVIEW

    loop = training_loop.training_loops_db.get_loop(loop_id)
    assert loop["status"] == "AWAITING_REVIEW"
    assert loop["current_round"] == 1
    assert len(loop["history"]) == 1              # 第1轮统计已入 history
    assert notifier.pushed                         # 报告已推
    assert "报告" in notifier.pushed[-1][1]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/caisen/test_training_loop.py::test_start_runs_round_then_awaits_review -x`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现 TrainingNotifier Protocol + Orchestrator 骨架 + _step_once（RUNNING→ANALYZING→AWAITING_REVIEW）**

`caisen/training_loop.py`：
```python
# -*- coding: utf-8 -*-
"""caisen.training_loop 训练 loop 状态机编排器（Spec 3 §4）。

物理定位：uvicorn 进程内 daemon 线程，concurrency=1（同时只一个活跃 loop）。
每轮：提交一个回测 task（复用 replay_tasks_db.create_task，由 Spec1 ReplayScheduler
派发）→ 轮询 get_task 等终态 → analyze_round 产报告 → AWAITING_REVIEW 推钉钉等你审核
→ submit_review(钉钉调) 驱动 CONFIRMING（parse_review + 回显确认）→ 下一轮或 STOPPED。

解耦：状态机只依赖 TrainingNotifier Protocol（push/reply）；钉钉实现在 training_dingtalk。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional, Protocol

from caisen import replay_tasks_db, training_analyzer, training_loops_db

logger = logging.getLogger(__name__)

_POLL_INTERVAL = 3.0          # 轮询回测终态间隔（秒）


class LoopBusyError(Exception):
    """已有活跃 loop（concurrency=1 守卫）→ API 层转 422。"""


class TrainingNotifier(Protocol):
    """推送接口抽象（钉钉实现见 training_dingtalk；测试注入 fake）。"""
    def push(self, loop_id: str, text: str) -> None: ...


class TrainingLoopOrchestrator:
    """训练 loop 编排器（daemon 线程跑状态机 + submit_review 驱动人审关卡）。

    线程模型：单 daemon 线程串行推进活跃 loop（concurrency=1）。人审关卡用
    per-loop threading.Event 解除阻塞——submit_review set event，daemon 继续。
    """

    def __init__(self, notifier: TrainingNotifier, clock=time.monotonic):
        self._notifier = notifier
        self._clock = clock
        self._thread: Optional[threading.Thread] = None
        self._stop_flag = threading.Event()
        # loop_id → 审核事件 + 待审文本（AWAITING_REVIEW 时 submit_review 唤醒）
        self._review_events: Dict[str, dict] = {}
        self._lock = threading.Lock()

    # ---- 公开 API ----
    def start_daemon(self) -> None:
        """启动 daemon 推进线程（lifespan 调；幂等）。"""
        if self._thread and self._thread.is_alive():
            return
        self._stop_flag.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="training-loop")
        self._thread.start()

    def stop_daemon(self) -> None:
        self._stop_flag.set()

    def start(self, req: dict) -> str:
        """提交训练 loop（concurrency=1 守卫：已有活跃 loop → LoopBusyError）。"""
        with self._lock:
            if training_loops_db.list_active_loops():
                raise LoopBusyError("已有活跃训练 loop（一次只允许一个）")
        training_loops_db.init_db()
        replay_tasks_db.init_db()
        loop_id = training_loops_db.create_loop(req)
        # 起一个审核 event 供首轮 AWAITING_REVIEW 用
        self._review_events[loop_id] = {"event": threading.Event(), "text": None}
        return loop_id

    def stop(self, loop_id: str) -> None:
        """人工喊停（钉钉 action=stop 或 API /stop）→ STOPPED + 解除阻塞。"""
        training_loops_db.update_loop(loop_id, status="STOPPED", finished_at=_now_iso())
        self._wake(loop_id, "__STOP__")

    def submit_review(self, loop_id: str, text: str) -> None:
        """钉钉 handler 收到你的审核 → 唤醒 AWAITING_REVIEW（CONFIRMING 流程由 daemon 处理）。"""
        self._wake(loop_id, text)

    # ---- daemon 主循环 ----
    def _loop(self) -> None:
        while not self._stop_flag.is_set():
            try:
                for loop in training_loops_db.list_active_loops():
                    self._step_once(loop["loop_id"])
            except Exception:
                logger.exception("training-loop daemon 循环异常（吞掉继续）")
            self._stop_flag.wait(_POLL_INTERVAL)

    def _step_once(self, loop_id: str) -> None:
        """推进单个 loop 一个状态转移（daemon 每轮调一次；测试也可直调）。

        状态机（Spec §4）：
          IDLE/RUNNING     → 提交回测 + 轮询终态 → SUCCESS 转 ANALYZING / FAILED 转 AWAITING_REVIEW
          ANALYZING        → analyze_round → AWAITING_REVIEW（推报告）
          AWAITING_REVIEW  → 阻塞等 submit_review → CONFIRMING（parse+回显）→ 确认则 RUNNING/STOPPED
        本方法按当前 status 分派，每次只推进一档（daemon 周期重入完成多档）。
        """
        loop = training_loops_db.get_loop(loop_id)
        if loop is None:
            return
        status = loop["status"]

        if status == "RUNNING":
            self._handle_running(loop)
        elif status == "ANALYZING":
            self._handle_analyzing(loop)
        elif status == "AWAITING_REVIEW":
            self._handle_awaiting_review(loop)
        # CONFIRMING 在 _handle_awaiting_review 内联完成（parse+回显+等确认一气呵成）
        # IDLE：由 start() 后首轮 daemon 进入 RUNNING（下面 _prime_if_idle）

    # ---- 状态处理 ----
    def _handle_running(self, loop: dict) -> None:
        """提交当轮回测 + 轮询终态 → ANALYZING 或 AWAITING_REVIEW(失败)。"""
        loop_id = loop["loop_id"]
        round_n = loop["current_round"] + 1
        training_loops_db.update_loop(loop_id, current_round=round_n)
        task_id = replay_tasks_db.create_task({
            "start": loop["start"], "end": loop["end"],
            "universe": loop["universe"],
            "cfg_override": loop["current_cfg"],
        })
        # 轮询等终态（带 stop 检查，避免你 stop 后还死等）
        while not self._stop_flag.is_set():
            task = replay_tasks_db.get_task(task_id)
            if task is None:
                training_loops_db.update_loop(loop_id, status="AWAITING_REVIEW",
                    pending_review="回测任务丢失，请重试/改/停",
                    error="replay task not found")
                self._notifier.push(loop_id, f"⚠️ 第{round_n}轮回测任务丢失，请回复「重跑/改…/停」。")
                return
            if task["status"] == "SUCCESS":
                self._on_round_success(loop_id, round_n, task["report"])
                return
            if task["status"] in ("FAILED", "CANCELLED"):
                training_loops_db.update_loop(loop_id, status="AWAITING_REVIEW",
                    pending_review=f"第{round_n}轮回测失败：{task.get('error','')}")
                self._notifier.push(loop_id,
                    f"⚠️ 第{round_n}轮回测失败：{task.get('error','')}\n请回复「重跑」或「改 字段=值 重跑」或「停」。")
                return
            self._stop_flag.wait(_POLL_INTERVAL)

    def _on_round_success(self, loop_id: str, round_n: int, report: dict) -> None:
        """回测 SUCCESS → 记历史摘要 → ANALYZING。"""
        summary = {
            "round": round_n,
            "n_hits": report.get("n_hits", 0),
            "win_rate": report.get("win_rate", 0),
            "avg_rr": report.get("avg_rr", 0),
            "max_dd": report.get("max_drawdown", 0),
            "annualized": report.get("annualized_return", 0),
        }
        training_loops_db.append_history(loop_id, summary)
        training_loops_db.update_loop(loop_id, status="ANALYZING")

    def _handle_analyzing(self, loop: dict) -> None:
        """ANALYZING → analyze_round → AWAITING_REVIEW（推报告）。"""
        loop_id = loop["loop_id"]
        loop = training_loops_db.get_loop(loop_id)   # 取最新 history
        last_report = self._last_report_summary(loop)  # 简化：用 history 末轮统计当 report
        md = training_analyzer.analyze_round(
            last_report, loop["current_cfg"], loop["history"])
        training_loops_db.update_loop(loop_id, status="AWAITING_REVIEW",
                                      pending_review=md)
        header = f"## 第{loop['current_round']}轮训练报告\n\n{md}\n\n---\n请回复你的审核（如「min_rr 改2.0 重跑」/「停」/「重置」）。"
        self._notifier.push(loop_id, header)

    def _handle_awaiting_review(self, loop: dict) -> None:
        """AWAITING_REVIEW → 阻塞等 submit_review → CONFIRMING（parse+回显+等确认）。"""
        loop_id = loop["loop_id"]
        ev = self._review_events.get(loop_id)
        if ev is None:
            ev = {"event": threading.Event(), "text": None}
            self._review_events[loop_id] = ev
        # 阻塞等审核（带周期 stop 检查；超时由 AWAITING_REVIEW 自身处理，见 §9）
        while not self._stop_flag.is_set():
            if ev["event"].wait(timeout=_POLL_INTERVAL):
                break
            # 你 stop 了 → 解除
            cur = training_loops_db.get_loop(loop_id)
            if cur and cur["status"] == "STOPPED":
                return
        text = ev["text"]
        ev["event"].clear()
        ev["text"] = None
        if text == "__STOP__":
            return   # stop() 已置 STOPPED
        self._confirm(loop_id, text)

    def _confirm(self, loop_id: str, text: str) -> None:
        """CONFIRMING：parse_review + 回显 + 等确认 → 下一轮/STOPPED/重等。"""
        loop = training_loops_db.get_loop(loop_id)
        try:
            parsed = training_analyzer.parse_review(text, loop["current_cfg"])
        except training_analyzer.ParseError as e:
            # 解析失败/非法 → 回显报错，回 AWAITING_REVIEW 重等
            training_loops_db.update_loop(loop_id, status="AWAITING_REVIEW")
            self._notifier.push(loop_id, f"❌ 没听懂：{e}\n请重新说明审核意图。")
            return
        # 回显草稿（推钉钉，等你回「确认」）
        draft = self._render_confirm(loop, parsed)
        training_loops_db.update_loop(loop_id, status="CONFIRMING", pending_review=draft)
        self._notifier.push(loop_id, draft)

        # 等你确认/否认
        ev = self._review_events[loop_id]
        while not self._stop_flag.is_set():
            if ev["event"].wait(timeout=_POLL_INTERVAL):
                break
            cur = training_loops_db.get_loop(loop_id)
            if cur and cur["status"] == "STOPPED":
                return
        confirm_text = (ev["text"] or "").strip()
        ev["event"].clear()
        ev["text"] = None
        if confirm_text == "__STOP__":
            return
        if "不" in confirm_text or "重新" in confirm_text:
            # 你说「不对/重新说」→ 回 AWAITING_REVIEW 重等审核
            training_loops_db.update_loop(loop_id, status="AWAITING_REVIEW")
            self._notifier.push(loop_id, "好的，请重新说明你的审核意图。")
            return
        # 确认 → 应用 action
        self._apply_confirmed(loop_id, parsed)

    def _apply_confirmed(self, loop_id: str, parsed: dict) -> None:
        loop = training_loops_db.get_loop(loop_id)
        action = parsed["action"]
        round_n = loop["current_round"]
        if action == "reset":
            new_cfg = loop["base_cfg"]
        elif action == "stop":
            training_loops_db.update_loop(loop_id, status="STOPPED", finished_at=_now_iso())
            self._notifier.push(loop_id, f"🛑 训练已停止（共 {round_n} 轮）。")
            return
        else:   # rerun
            new_cfg = {**loop["current_cfg"], **parsed["cfg_override"]}
        if round_n >= loop["max_rounds"]:
            training_loops_db.update_loop(loop_id, status="STOPPED", finished_at=_now_iso())
            self._notifier.push(loop_id, f"✅ 已达 max_rounds={loop['max_rounds']}，训练结束（共 {round_n} 轮）。")
            return
        training_loops_db.update_loop(loop_id, status="RUNNING", current_cfg=new_cfg,
                                      pending_review=None)

    # ---- 辅助 ----
    def _render_confirm(self, loop: dict, parsed: dict) -> str:
        """回显：上轮 cfg → 本轮改动 → 本轮完整 cfg + 动作。"""
        action_zh = {"rerun": "改参重跑", "stop": "停止", "reset": "重置回基准"}[parsed["action"]]
        changes = "\n".join(f"- {k}: {loop['current_cfg'].get(k)} → {v}"
                            for k, v in parsed["cfg_override"].items()) or "- （无改动）"
        new_cfg = ({**loop["current_cfg"], **parsed["cfg_override"]}
                   if parsed["action"] == "rerun" else loop["base_cfg"])
        return (f"## 请确认第{loop['current_round']+1}轮\n\n"
                f"**动作**：{action_zh}\n\n**改动**：\n{changes}\n\n"
                f"**下轮完整 cfg**：\n```\n{json.dumps(new_cfg, ensure_ascii=False)}\n```\n\n"
                f"回复「确认」执行，或「不对」重新说明。")

    def _last_report_summary(self, loop: dict) -> dict:
        """把 history 末轮摘要还原成近似 report dict 喂 analyze_round。"""
        if not loop["history"]:
            return {}
        h = loop["history"][-1]
        return {"n_hits": h.get("n_hits"), "win_rate": h.get("win_rate"),
                "avg_rr": h.get("avg_rr"), "max_drawdown": h.get("max_dd"),
                "annualized_return": h.get("annualized"), "pattern_dist": {}}

    def _wake(self, loop_id: str, text: str) -> None:
        ev = self._review_events.get(loop_id)
        if ev is not None:
            ev["text"] = text
            ev["event"].set()
```

> **实现者注意**：`_now_iso` 在本模块未定义——顶部加 `from caisen.replay_tasks_db import _now_iso`（复用，零重复）。`import json` 也要加（`_render_confirm` 用）。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/caisen/test_training_loop.py::test_start_runs_round_then_awaits_review -x`
Expected: PASS

- [ ] **Step 5: 写失败测试（concurrency 守卫 + CONFIRMING 确认续跑 + cfg 累积）**

追加到 `tests/caisen/test_training_loop.py`：
```python
def test_start_rejects_second_active_loop(orch):
    o, _ = orch
    o.start({"start": "2020-01-01", "end": "2024-12-31", "base_cfg": {}, "max_rounds": 3})
    # 手动把第一个标活跃
    from caisen import training_loops_db
    lid = training_loops_db.list_loops()[0]["loop_id"]
    training_loops_db.update_loop(lid, status="RUNNING")
    with pytest.raises(LoopBusyError):
        o.start({"start": "2020-01-01", "end": "2024-12-31", "base_cfg": {}, "max_rounds": 3})


def test_confirm_rerun_accumulates_cfg(orch, monkeypatch):
    """CONFIRMING：parse→回显→「确认」→ 下一轮 cfg 累积 + 状态回 RUNNING。"""
    o, notifier = orch
    loop_id = o.start({"start": "2020-01-01", "end": "2024-12-31", "base_cfg": {"min_rr_ratio": 1.5},
                       "max_rounds": 3})
    # 直接置 AWAITING_REVIEW 模拟已到人审关卡
    from caisen import training_loops_db
    training_loops_db.update_loop(loop_id, status="AWAITING_REVIEW", current_round=1)
    monkeypatch.setattr(training_loop.training_analyzer, "parse_review",
                        lambda t, c: {"cfg_override": {"max_holding_bars": 20}, "action": "rerun"})

    # 在 _confirm 阻塞等确认前，先起一个线程模拟你「确认」
    import threading
    def confirm_later():
        import time as _t; _t.sleep(0.2)
        o.submit_review(loop_id, "确认")
    threading.Thread(target=confirm_later).start()
    o._step_once(loop_id)   # AWAITING_REVIEW → CONFIRMING → 等「确认」 → RUNNING

    loop = training_loops_db.get_loop(loop_id)
    assert loop["status"] == "RUNNING"
    assert loop["current_round"] == 1   # RUNNING 等 _handle_running 才 +1
    assert loop["current_cfg"] == {"min_rr_ratio": 1.5, "max_holding_bars": 20}  # 累积
```

- [ ] **Step 6: 跑测试确认失败 → 修补 → 通过**

Run: `python -m pytest tests/caisen/test_training_loop.py -x`
Expected: 先 FAIL（如缺 import），补 `from caisen.replay_tasks_db import _now_iso` 与 `import json` 后 PASS。

- [ ] **Step 7: 跑全部状态机测试**

Run: `python -m pytest tests/caisen/test_training_loop.py -v`
Expected: 3 PASS

- [ ] **Step 8: Commit**

```bash
git add caisen/training_loop.py tests/caisen/test_training_loop.py
git commit -m "feat(training): Task3 loop 状态机+回测复用+notifier注入(concurrency=1)"
```

---

## Task 4: training_dingtalk（钉钉推送 + stream 收审核适配层）

**Files:**
- Create: `caisen/training_dingtalk.py`
- Test: `tests/caisen/test_training_dingtalk.py`

**Interfaces:**
- Consumes: `review_chatbot_handler` 复用 `bridge` 的 stream 范式、`urllib`（access_token + batch send）
- Produces: `DingTalkNotifier`（实现 `TrainingNotifier` Protocol）、`ReviewChatbotHandler`（继承 `dingtalk_stream.ChatbotHandler`）、`start_review_bot(app)`（lifespan 装配，返 async task）、`ReviewBotConfig.from_env()`

**关键实现（全局约束 §1）：**
- **access_token**：`POST https://api.dingtalk.com/v1.0/oauth2/accessToken` body `{appKey, appSecret}` → 缓存 `accessToken` + 过期时间（`expireIn` 提前 60s 刷新）。
- **主动推单聊**：`POST https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend`，header `Authorization: Bearer {token}`，body `{robotCode: app_key, userIds: [staff_id], msgKey: "sampleMarkdown", msgParam: json.dumps({title, text})}`。
- **收审核**：`ReviewChatbotHandler.process` 解析 @消息 → 白名单校验（`REVIEW_ALLOWED_STAFF_IDS`）→ 调 `orchestrator.submit_review(active_loop_id, text)`。
- **Markdown 清洗**：复用 `bridge.replier.clean_markdown_for_dingtalk`（钉钉 Markdown 限制多）。
- **白名单/凭证缺失 → 不装配**（与 bridge 一致，软降级不阻断 uvicorn 启动）。

- [ ] **Step 1: 写失败测试（DingTalkNotifier.push：mock urllib access_token + batch send）**

`tests/caisen/test_training_dingtalk.py`：
```python
# -*- coding: utf-8 -*-
"""training_dingtalk 推送/接收单测。mock urllib 不真发钉钉。"""
import json
from unittest.mock import MagicMock, patch

from caisen import training_dingtalk


def _fake_urlopen(token_resp=None, send_resp=None):
    """构造一个按 url 分派返回的 urlopen 替身（access_token vs batch send）。"""
    def fake(req, timeout=None):
        url = req.full_url
        resp = MagicMock()
        resp.read.return_value = json.dumps(
            token_resp if "oauth2/accessToken" in url else send_resp
        ).encode("utf-8")
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=resp)
        ctx.__exit__ = MagicMock(return_value=False)
        return ctx
    return fake


def test_notifier_push_fetches_token_then_sends_batch(monkeypatch):
    """push：先换 access_token，再调 batch send（robotCode=app_key, userIds=白名单）。"""
    monkeypatch.setenv("REVIEW_APP_KEY", "ak123")
    monkeypatch.setenv("REVIEW_APP_SECRET", "sk456")
    monkeypatch.setenv("REVIEW_ALLOWED_STAFF_IDS", "staffA")
    cfg = training_dingtalk.ReviewBotConfig.from_env()

    captured = {}
    def fake(req, timeout=None):
        url = req.full_url
        body = json.loads(req.data.decode("utf-8"))
        resp = MagicMock(); resp.read.return_value = json.dumps(
            {"accessToken": "TOK", "expireIn": 7200} if "oauth2" in url else {"sendTaskId": "x"}
        ).encode()
        captured["url"] = url; captured["body"] = body; captured["header"] = req.headers
        ctx = MagicMock(); ctx.__enter__ = MagicMock(return_value=resp)
        ctx.__exit__ = MagicMock(return_value=False); return ctx

    monkeypatch.setattr(training_dingtalk.urllib.request, "urlopen", fake)
    n = training_dingtalk.DingTalkNotifier(cfg)
    n.push("loop1", "## 报告\n内容")

    assert "oauth2/accessToken" in captured["body"]  # 第一次换 token
    assert captured["header"].get("Authorization") == "Bearer TOK"
    assert captured["body"]["robotCode"] == "ak123"
    assert captured["body"]["userIds"] == ["staffA"]
    assert captured["body"]["msgKey"] == "sampleMarkdown"


def test_notifier_caches_token(monkeypatch):
    """第二次 push 复用 token，不再换（仅 1 次 oauth2 调用）。"""
    monkeypatch.setenv("REVIEW_APP_KEY", "ak"); monkeypatch.setenv("REVIEW_APP_SECRET", "sk")
    monkeypatch.setenv("REVIEW_ALLOWED_STAFF_IDS", "s1")
    cfg = training_dingtalk.ReviewBotConfig.from_env()
    calls = {"token": 0}
    def fake(req, timeout=None):
        url = req.full_url
        if "oauth2" in url: calls["token"] += 1
        resp = MagicMock(); resp.read.return_value = json.dumps(
            {"accessToken": "T", "expireIn": 7200} if "oauth2" in url else {"sendTaskId": "y"}
        ).encode()
        ctx = MagicMock(); ctx.__enter__ = MagicMock(return_value=resp)
        ctx.__exit__ = MagicMock(return_value=False); return ctx
    monkeypatch.setattr(training_dingtalk.urllib.request, "urlopen", fake)
    n = training_dingtalk.DingTalkNotifier(cfg)
    n.push("l", "a"); n.push("l", "b")
    assert calls["token"] == 1
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/caisen/test_training_dingtalk.py::test_notifier_push_fetches_token_then_sends_batch -x`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现 ReviewBotConfig + DingTalkNotifier（urllib access_token + batch send）**

`caisen/training_dingtalk.py`：
```python
# -*- coding: utf-8 -*-
"""caisen.training_dingtalk 参数审查机器人（Spec 3 §7）。

双通道（单一企业内部应用，凭证隔离于 bridge）：
- 主动推报告/回显：access_token + 「企业机器人发单聊消息」batch send API（urllib 极简）。
- 收审核：dingtalk-stream ChatbotHandler（双向 stream），收到 @消息 → 白名单校验 →
  调 orchestrator.submit_review 唤醒当前活跃 loop。

Why 主动推用 batch send 而非 stream：dingtalk-stream 的 ChatbotHandler.reply_text 只能
@回复 incoming 消息，loop 后台触发推报告时无 incoming msg，必须用 batch send 主动发。
（全局约束 §1 已述，细化 spec §7）
"""
from __future__ import annotations

import json
import logging
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from typing import Any, List, Optional

from bridge.replier import clean_markdown_for_dingtalk

logger = logging.getLogger(__name__)

_OAUTH_URL = "https://api.dingtalk.com/v1.0/oauth2/accessToken"
_SEND_URL = "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend"
_HTTP_TIMEOUT = 10


@dataclass(frozen=True)
class ReviewBotConfig:
    """参数审查机器人配置（环境变量装配，凭证绝不硬编码）。"""
    app_key: str
    app_secret: str
    allowed_staff_ids: tuple   # 白名单 staffId（防他人触发训练消耗算力）

    @classmethod
    def from_env(cls) -> Optional["ReviewBotConfig"]:
        """从 REVIEW_* 环境变量装配。三值缺一 → 返 None（软降级，不阻断 uvicorn）。"""
        import os
        app_key = os.getenv("REVIEW_APP_KEY", "").strip()
        app_secret = os.getenv("REVIEW_APP_SECRET", "").strip()
        raw = os.getenv("REVIEW_ALLOWED_STAFF_IDS", "")
        staff = tuple(s.strip() for s in raw.split(",") if s.strip())
        if not app_key or not app_secret or not staff:
            logger.info("REVIEW_APP_KEY/SECRET/STAFF_IDS 未完整配置，参数审查机器人不装配（软降级）")
            return None
        return cls(app_key=app_key, app_secret=app_secret, allowed_staff_ids=staff)


class DingTalkNotifier:
    """实现 TrainingNotifier Protocol：access_token 缓存 + batch send 主动推单聊。"""

    def __init__(self, cfg: ReviewBotConfig, clock=time.time):
        self._cfg = cfg
        self._clock = clock
        self._token: Optional[str] = None
        self._token_expire: float = 0.0

    def _get_token(self) -> str:
        """换取并缓存 access_token（提前 60s 刷新，避免边界过期）。"""
        if self._token and self._clock() < self._token_expire - 60:
            return self._token
        body = json.dumps({"appKey": self._cfg.app_key,
                           "appSecret": self._cfg.app_secret}).encode("utf-8")
        req = urllib.request.Request(_OAUTH_URL, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        self._token = data["accessToken"]
        self._token_expire = self._clock() + int(data.get("expireIn", 7200))
        return self._token

    def push(self, loop_id: str, text: str) -> None:
        """主动推 Markdown 单聊（loop_id 仅用于日志，消息发给全部白名单 staff）。

        失败仅记日志（推送是附属通道，不应反拖垮 loop 主流程）。
        """
        try:
            token = self._get_token()
            cleaned = clean_markdown_for_dingtalk(text)
            title = cleaned.split("\n")[0].lstrip("# ").strip()[:40] or "训练报告"
            msg_param = json.dumps({"title": title, "text": cleaned}, ensure_ascii=False)
            body = json.dumps({
                "robotCode": self._cfg.app_key,          # 钉钉事实：robotCode = appKey
                "userIds": list(self._cfg.allowed_staff_ids),
                "msgKey": "sampleMarkdown",
                "msgParam": msg_param,
            }).encode("utf-8")
            req = urllib.request.Request(_SEND_URL, data=body, method="POST")
            req.add_header("Authorization", f"Bearer {token}")
            req.add_header("Content-Type", "application/json")
            with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
                resp.read()
            logger.info("钉钉审查机器人推送成功 loop=%s title=%s", loop_id, title)
        except Exception as exc:
            logger.warning("钉钉审查机器人推送失败 loop=%s：%s", loop_id, exc)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/caisen/test_training_dingtalk.py -k notifier -v`
Expected: 2 PASS

- [ ] **Step 5: 写失败测试（ReviewChatbotHandler：白名单 + 唤醒 orchestrator）**

追加到 `tests/caisen/test_training_dingtalk.py`：
```python
def test_chatbot_handler_whitelist_and_wake(monkeypatch):
    """@机器人审核：白名单内 → submit_review 唤醒活跃 loop；非白名单 → 静默丢弃。"""
    monkeypatch.setenv("REVIEW_APP_KEY", "ak"); monkeypatch.setenv("REVIEW_APP_SECRET", "sk")
    monkeypatch.setenv("REVIEW_ALLOWED_STAFF_IDS", "staffA")
    cfg = training_dingtalk.ReviewBotConfig.from_env()

    submitted = []
    orch = MagicMock()
    orch.active_loop_id = "loop1"
    orch.submit_review = lambda lid, text: submitted.append((lid, text))

    h = training_dingtalk.ReviewChatbotHandler(cfg, orch)

    # 构造一条 incoming msg（白名单内）
    msg = MagicMock()
    msg.text.content = "min_rr 改2.0 重跑"
    msg.sender_staff_id = "staffA"
    msg.conversation_id = "c1"
    msg.message_id = "m1"
    h._dispatch(msg)
    assert submitted == [("loop1", "min_rr 改2.0 重跑")]

    # 非白名单 → 不唤醒
    submitted.clear()
    msg.sender_staff_id = "intruder"
    h._dispatch(msg)
    assert submitted == []
```

- [ ] **Step 6: 跑测试确认失败**

Run: `python -m pytest tests/caisen/test_training_dingtalk.py::test_chatbot_handler_whitelist_and_wake -x`
Expected: FAIL（`ReviewChatbotHandler` 未定义）

- [ ] **Step 7: 实现 ReviewChatbotHandler + start_review_bot（lifespan 装配）**

追加到 `caisen/training_dingtalk.py`：
```python
import asyncio
import dingtalk_stream
from dingtalk_stream import AckMessage, ChatbotMessage


class ReviewChatbotHandler(dingtalk_stream.ChatbotHandler):
    """参数审查机器人消息入口：所有 @此机器人的消息 = 当前活跃 loop 的审核。

    专门审核（spec §7）：不路由分流。白名单外的 @消息静默丢弃（防他人触发训练）。
    无活跃 loop 时回执提示「当前无进行中的训练」。
    """

    def __init__(self, cfg: ReviewBotConfig, orchestrator):
        super().__init__()
        self._cfg = cfg
        self._orch = orchestrator

    async def process(self, callback):  # type: ignore[override]
        """ChatbotMessage 回调：立即 ACK + 异步派发（不阻塞 SDK 主循环，仿 BridgeHandler）。"""
        try:
            msg = ChatbotMessage.from_dict(callback.data)
        except Exception:
            logger.exception("审查机器人消息解析失败，ACK 丢弃")
            return AckMessage.STATUS_OK, "ok"
        asyncio.create_task(self._safe_dispatch(msg))
        return AckMessage.STATUS_OK, "ok"

    async def _safe_dispatch(self, msg):
        try:
            self._dispatch(msg)
        except Exception:
            logger.exception("审查机器人派发异常")

    def _dispatch(self, msg) -> None:
        """白名单 → 唤醒活跃 loop（测试直调本方法，跳过 SDK ACK 细节）。"""
        text = (getattr(msg.text, "content", "") or "").strip()
        # 去 @机器人 前缀（同 BridgeHandler._dispatch）
        if text.startswith("@"):
            text = text.split(maxsplit=1)[1] if " " in text else ""
            text = text.strip()
        sender = getattr(msg, "sender_staff_id", "") or getattr(msg, "sender_id", "")
        if sender not in self._cfg.allowed_staff_ids:
            logger.info("审查机器人拒绝非白名单消息：sender=%s", sender)
            return
        loop_id = getattr(self._orch, "active_loop_id", None)
        if not loop_id:
            logger.info("审查机器人收到审核但无活跃 loop，忽略")
            return
        self._orch.submit_review(loop_id, text)


async def _run_stream(cfg: ReviewBotConfig, orchestrator) -> None:
    """阻塞协程：起 dingtalk-stream 连接收审核（独立 app 凭证，与 bridge 物理隔离）。

    直接 await client.start()（SDK 阻塞协程，内置 while True 重连），不用 start_forever()
    （会在 running loop 内 asyncio.run 报错，bridge 已踩过坑）。
    """
    credential = dingtalk_stream.Credential(cfg.app_key, cfg.app_secret)
    client = dingtalk_stream.DingTalkStreamClient(credential)
    handler = ReviewChatbotHandler(cfg, orchestrator)
    client.register_callback_handler(ChatbotMessage.TOPIC, handler)
    logger.info("参数审查机器人 stream 启动（独立 app_key=%s…）", cfg.app_key[:6])
    await client.start()


def start_review_bot(app, orchestrator) -> Any:
    """lifespan 装配入口：凭证齐 → 起 stream 后台 async task；不齐 → 返 None 软降级。

    返回 asyncio.Task（lifespan shutdown 时 cancel）。task 持有到 app.state.review_bot_task。
    """
    cfg = ReviewBotConfig.from_env()
    if cfg is None:
        return None
    task = asyncio.create_task(_run_stream(cfg, orchestrator), name="review-bot-stream")
    logger.info("参数审查机器人后台 task 已起")
    return task
```

> **实现者注意**：`orchestrator.active_loop_id` 是 orchestrator 暴露的「当前活跃 loop_id」属性——Task 3 的 `TrainingLoopOrchestrator` 需补一个 `@property active_loop_id`，从 `list_active_loops()[0]` 取。Task 5 接线时补。

- [ ] **Step 8: 跑全部钉钉测试**

Run: `python -m pytest tests/caisen/test_training_dingtalk.py -v`
Expected: 3 PASS

- [ ] **Step 9: Commit**

```bash
git add caisen/training_dingtalk.py tests/caisen/test_training_dingtalk.py
git commit -m "feat(training): Task4 钉钉审查机器人 access_token 主动推+stream 收审核"
```

---

## Task 5: loop × dingtalk 接线 + active_loop_id + 重启恢复闭环

**Files:**
- Modify: `caisen/training_loop.py`（补 `active_loop_id` 属性）
- Test: `tests/caisen/test_training_loop.py`（补接线测试）

**目标：** 补 `active_loop_id` property（Task 4 handler 依赖）；验证「loop 到 AWAITING_REVIEW → notifier.push 被调 → 收审核 submit_review → CONFIRMING → 确认续跑」整条链路在注入真实 `DingTalkNotifier`（mock urllib）下端到端跑通；验证重启恢复 `reset_interrupted`。

- [ ] **Step 1: 补 active_loop_id property**

在 `caisen/training_loop.py` 的 `TrainingLoopOrchestrator` 内补：
```python
    @property
    def active_loop_id(self) -> Optional[str]:
        """当前活跃 loop_id（供 ReviewChatbotHandler 把 @消息路由到正确 loop）。concurrency=1 取首个。"""
        active = training_loops_db.list_active_loops()
        return active[0]["loop_id"] if active else None
```

- [ ] **Step 2: 写接线测试（DingTalkNotifier mock urllib，端到端状态机一轮）**

追加到 `tests/caisen/test_training_loop.py`：
```python
def test_full_roundtrip_with_dingtalk_notifier(monkeypatch, tmp_path):
    """端到端：loop→回测SUCCESS→analyze→push 报告→收审核→确认→下一轮 RUNNING。

    用真实 DingTalkNotifier（mock urllib），验证 notifier Protocol 与 loop 正确接线。
    """
    import json
    db = str(tmp_path / "loops.db")
    monkeypatch.setattr(training_loop.training_loops_db, "_DEFAULT_DB_PATH", db)
    training_loop.training_loops_db.init_db()
    monkeypatch.setattr(training_loop.replay_tasks_db, "_DEFAULT_DB_PATH",
                        str(tmp_path / "replay.db"))
    training_loop.replay_tasks_db.init_db()
    monkeypatch.setenv("REVIEW_APP_KEY", "ak"); monkeypatch.setenv("REVIEW_APP_SECRET", "sk")
    monkeypatch.setenv("REVIEW_ALLOWED_STAFF_IDS", "s1")

    # mock urllib（access_token + batch send）
    def fake(req, timeout=None):
        resp = MagicMock(); resp.read.return_value = json.dumps(
            {"accessToken": "T", "expireIn": 7200} if "oauth2" in req.full_url
            else {"sendTaskId": "z"}).encode()
        ctx = MagicMock(); ctx.__enter__ = MagicMock(return_value=resp)
        ctx.__exit__ = MagicMock(return_value=False); return ctx
    monkeypatch.setattr(training_loop.training_dingtalk.urllib.request, "urlopen", fake)

    from caisen.training_dingtalk import DingTalkNotifier, ReviewBotConfig
    notifier = DingTalkNotifier(ReviewBotConfig.from_env())
    o = training_loop.TrainingLoopOrchestrator(notifier)

    monkeypatch.setattr(training_loop.replay_tasks_db, "get_task",
                        lambda tid, path=None: {"status": "SUCCESS",
                            "report": {"n_hits": 5, "win_rate": 0.5, "avg_rr": 1.0,
                                       "max_drawdown": -0.1, "annualized_return": 0.1,
                                       "pattern_dist": {}}})
    monkeypatch.setattr(training_loop.training_analyzer, "analyze_round",
                        lambda r, c, h: "报告")
    monkeypatch.setattr(training_loop.training_analyzer, "parse_review",
                        lambda t, c: {"cfg_override": {"min_rr_ratio": 2.0}, "action": "rerun"})

    loop_id = o.start({"start": "2020-01-01", "end": "2024-12-31", "base_cfg": {"min_rr_ratio": 1.5},
                       "max_rounds": 3})
    o._step_once(loop_id)   # RUNNING→ANALYZING→AWAITING_REVIEW
    assert o.active_loop_id == loop_id
    # 收审核（CONFIRMING），起线程模拟「确认」
    import threading, time as _t
    threading.Thread(target=lambda: (_t.sleep(0.2), o.submit_review(loop_id, "确认"))).start()
    o._step_once(loop_id)   # AWAITING_REVIEW → CONFIRMING → 确认 → RUNNING(下一轮)
    loop = training_loop.training_loops_db.get_loop(loop_id)
    assert loop["status"] == "RUNNING"
    assert loop["current_cfg"]["min_rr_ratio"] == 2.0
```

- [ ] **Step 3: 跑测试确认通过**

Run: `python -m pytest tests/caisen/test_training_loop.py::test_full_roundtrip_with_dingtalk_notifier -x`
Expected: PASS（如失败，按失败信息调 `_step_once` 在 CONFIRMING 等待逻辑 / `_wake` 时序）

- [ ] **Step 4: 跑全部 loop 测试**

Run: `python -m pytest tests/caisen/test_training_loop.py -v`
Expected: 4 PASS

- [ ] **Step 5: Commit**

```bash
git add caisen/training_loop.py tests/caisen/test_training_loop.py
git commit -m "feat(training): Task5 loop×dingtalk 接线+active_loop_id+端到端一轮"
```

---

## Task 6: server API 端点 + schemas

**Files:**
- Create: `server/schemas/training.py`
- Create: `server/api/v1/training.py`
- Test: `tests/test_training_api.py`

**Interfaces:**
- Consumes: 全局单例 orchestrator（由 lifespan 装配到 `app.state.training_orchestrator`，端点经 `request.app.state` 取）
- Produces: `POST /api/v1/training/start`、`GET /api/v1/training/{loop_id}`、`POST /api/v1/training/{loop_id}/stop`、`GET /api/v1/training`

- [ ] **Step 1: 写失败测试（4 端点 TestClient）**

`tests/test_training_api.py`：
```python
# -*- coding: utf-8 -*-
"""training API 端点集成测试。注入 fake orchestrator，不依赖真状态机/钉钉。"""
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.api.v1 import training as training_api


def _app_with_fake_orch(orch):
    app = FastAPI()
    app.state.training_orchestrator = orch
    app.include_router(training_api.router, prefix="/api/v1")
    return TestClient(app)


def test_start_returns_loop_id():
    orch = MagicMock()
    orch.start.return_value = "loop-xyz"
    client = _app_with_fake_orch(orch)
    r = client.post("/api/v1/training/start", json={
        "start": "2020-01-01", "end": "2024-12-31", "base_cfg": {}, "max_rounds": 5})
    assert r.status_code == 200
    assert r.json()["loop_id"] == "loop-xyz"
    orch.start.assert_called_once()


def test_start_rejects_when_busy():
    """已有活跃 loop → LoopBusyError → 422。"""
    from caisen.training_loop import LoopBusyError
    orch = MagicMock()
    orch.start.side_effect = LoopBusyError("busy")
    client = _app_with_fake_orch(orch)
    r = client.post("/api/v1/training/start", json={
        "start": "2020-01-01", "end": "2024-12-31", "base_cfg": {}, "max_rounds": 5})
    assert r.status_code == 422


def test_get_loop_state():
    orch = MagicMock()
    orch.get_state.return_value = {"loop_id": "l1", "status": "AWAITING_REVIEW",
                                   "current_round": 1, "history": [], "current_cfg": {}}
    client = _app_with_fake_orch(orch)
    r = client.get("/api/v1/training/l1")
    assert r.status_code == 200
    assert r.json()["status"] == "AWAITING_REVIEW"


def test_stop_loop():
    orch = MagicMock()
    client = _app_with_fake_orch(orch)
    r = client.post("/api/v1/training/l1/stop")
    assert r.status_code == 200
    orch.stop.assert_called_once_with("l1")


def test_list_loops():
    orch = MagicMock()
    orch.list_loops.return_value = [{"loop_id": "l1", "status": "STOPPED"}]
    client = _app_with_fake_orch(orch)
    r = client.get("/api/v1/training")
    assert r.status_code == 200
    assert len(r.json()) == 1
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_training_api.py -x`
Expected: FAIL（`server.api.v1.training` 不存在）

- [ ] **Step 3: 实现 schemas/training.py**

`server/schemas/training.py`：
```python
# -*- coding: utf-8 -*-
"""训练 loop API 的 Pydantic 契约（Spec 3 §8）。"""
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class TrainingStartRequest(BaseModel):
    """POST /training/start：提交训练 loop。

    base_cfg_override=提交时初始 cfg（=重置基准）；缺省用 StrategyConfig 默认。
    start/end/universe=每轮回测固定区间与标的池（同 replay）。
    """
    start: str
    end: str
    universe: Optional[List[str]] = None
    base_cfg_override: Dict[str, Any] = Field(default_factory=dict)
    max_rounds: int = 20      # 默认 20（spec §11 拍板；可配）


class RoundSummary(BaseModel):
    round: int
    n_hits: int = 0
    win_rate: float = 0.0
    avg_rr: float = 0.0
    max_dd: float = 0.0
    annualized: float = 0.0


class TrainingLoopState(BaseModel):
    """GET /training/{loop_id}：loop 状态 + 历史轮次摘要。"""
    loop_id: str
    status: str
    current_round: int
    max_rounds: int
    start: Optional[str] = None
    end: Optional[str] = None
    base_cfg: Dict[str, Any] = Field(default_factory=dict)
    current_cfg: Dict[str, Any] = Field(default_factory=dict)
    history: List[RoundSummary] = Field(default_factory=list)
    pending_review: Optional[str] = None
    error: Optional[str] = None
```

- [ ] **Step 4: 实现 api/v1/training.py（4 端点）**

`server/api/v1/training.py`：
```python
# -*- coding: utf-8 -*-
"""训练 loop API 路由（Spec 3 §8）。

端点：
- POST /training/start        提交训练 {start,end,universe,base_cfg_override,max_rounds} → loop_id
- GET  /training/{loop_id}    loop 状态 + 历史轮次摘要
- POST /training/{loop_id}/stop  停止 loop
- GET  /training              loop 列表（降序）

钉钉审核回调进程内唤醒 loop（不走外部 HTTP），故无「提交审核」端点。
orchestrator 经 app.state.training_orchestrator 取（lifespan 装配）。
"""
import logging
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Request

from caisen import training_loops_db
from caisen.training_loop import LoopBusyError
from server.schemas.training import TrainingStartRequest

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/training", tags=["训练 loop"])


def _orch(request: Request):
    """从 app.state 取 orchestrator（lifespan 装配）；缺 → 503。"""
    orch = getattr(request.app.state, "training_orchestrator", None)
    if orch is None:
        raise HTTPException(503, "训练 loop 编排器未装配（lifespan 未起）")
    return orch


@router.post("/start", summary="提交训练 loop（连续 回测→AI分析→钉钉人审→调参续跑）")
def start_training(body: TrainingStartRequest, request: Request) -> Dict[str, Any]:
    try:
        loop_id = _orch(request).start({
            "start": body.start, "end": body.end, "universe": body.universe,
            "base_cfg": body.base_cfg_override, "max_rounds": body.max_rounds,
        })
    except LoopBusyError as exc:
        raise HTTPException(422, str(exc))
    return {"loop_id": loop_id}


@router.get("/{loop_id}", summary="loop 状态 + 历史轮次摘要")
def get_training(loop_id: str) -> Any:
    loop = training_loops_db.get_loop(loop_id)
    if loop is None:
        raise HTTPException(404, f"loop {loop_id} 不存在")
    return loop


@router.post("/{loop_id}/stop", summary="停止 loop")
def stop_training(loop_id: str, request: Request) -> Dict[str, Any]:
    _orch(request).stop(loop_id)
    return {"loop_id": loop_id, "status": "STOPPED"}


@router.get("", summary="loop 列表（降序）")
def list_trainings(limit: int = 100) -> List[Any]:
    return training_loops_db.list_loops(limit=limit)
```

> **实现者注意**：orchestrator 需补 `get_state(loop_id)`/`list_loops()` 转发方法（直接 return `training_loops_db.get_loop`/`list_loops`）——见 Step 5。或端点直接调 `training_loops_db`（如上 `get_training`/`list_trainings` 已直调 db，`get_state` 测试桩据此调整：把 test 的 `orch.get_state` 改为 monkeypatch `training_loops_db.get_loop`）。**推荐端点直调 db（`get_training`/`list_trainings` 已是），仅 `start`/`stop` 走 orchestrator**——这样 `get_state` 桩测试改为 monkeypatch db，更简单。据此调整 Step 1 测试：`test_get_loop_state`/`test_list_loops` 用 monkeypatch `training_loops_db.get_loop`/`list_loops` 而非 `orch.get_state`。

- [ ] **Step 5: 按注意调整 get/list 测试为 monkeypatch db**

把 `test_get_loop_state` 改为：
```python
def test_get_loop_state(monkeypatch):
    monkeypatch.setattr(training_api.training_loops_db, "get_loop",
                        lambda lid: {"loop_id": lid, "status": "AWAITING_REVIEW",
                                     "current_round": 1, "history": [], "current_cfg": {}})
    app = FastAPI(); app.state.training_orchestrator = MagicMock()
    app.include_router(training_api.router, prefix="/api/v1")
    r = TestClient(app).get("/api/v1/training/l1")
    assert r.status_code == 200 and r.json()["status"] == "AWAITING_REVIEW"
```
`test_list_loops` 同理 monkeypatch `training_loops_db.list_loops`。

- [ ] **Step 6: 跑全部 API 测试**

Run: `python -m pytest tests/test_training_api.py -v`
Expected: 5 PASS

- [ ] **Step 7: Commit**

```bash
git add server/schemas/training.py server/api/v1/training.py tests/test_training_api.py
git commit -m "feat(training): Task6 training 4 端点+schemas(/start /{id} /stop 列表)"
```

---

## Task 7: server/main.py lifespan 装配 + 端到端集成 + 重启恢复

**Files:**
- Modify: `server/main.py`（lifespan 起 orchestrator daemon + review bot stream；include training router；启动恢复）
- Test: 集成验证（跑既有测试套件不回归 + training 全套绿）

**目标：** 把 orchestrator + review bot 接进 uvicorn lifespan；注册 training router；启动时 `reset_interrupted()`；确保不回归既有 700+ 测试。

- [ ] **Step 1: main.py 装配 orchestrator + review bot（lifespan）**

在 `server/main.py` lifespan 内，紧跟 replay_scheduler 装配块（约 L82-94 之后）插入：
```python
    # 启动：训练 loop 编排器 + 参数审查钉钉机器人（Spec 3）
    # Why 寄生 uvicorn：合「零守护进程」哲学；orchestrator daemon 线程 + review bot stream
    # 均寄生主进程。review bot 凭证不齐 → 软降级（不阻断 uvicorn），loop 仍可经 API 跑
    # （只是无人审推送，状态卡 AWAITING_REVIEW 可经 GET 查看）。
    try:
        from caisen import training_loops_db
        from caisen.training_loop import TrainingLoopOrchestrator
        from caisen.training_dingtalk import ReviewBotConfig, DingTalkNotifier, start_review_bot
        training_loops_db.init_db()                      # 建 training_loops 表（幂等）
        training_loops_db.reset_interrupted()            # 重启恢复：残留 RUNNING/ANALYZING → STOPPED
        _review_cfg = ReviewBotConfig.from_env()
        _notifier = DingTalkNotifier(_review_cfg) if _review_cfg else _NoopNotifier()
        app.state.training_orchestrator = TrainingLoopOrchestrator(_notifier)
        app.state.training_orchestrator.start_daemon()
        # review bot stream（凭证齐才起；不齐 _NoopNotifier 下不起，软降级）
        if _review_cfg is not None:
            app.state.review_bot_task = start_review_bot(app, app.state.training_orchestrator)
        else:
            logging.getLogger(__name__).info("REVIEW_* 凭证未配，参数审查机器人软降级（loop 可跑但无人审推送）")
    except Exception:
        logging.getLogger(__name__).exception("lifespan 装配训练 loop 异常（已忽略）")
```

在 lifespan **shutdown 段**（`_pool.shutdown(wait=False)` 之后）追加：
```python
    # 销毁：训练 loop daemon + review bot stream（Spec 3）
    _orch = getattr(app.state, "training_orchestrator", None)
    if _orch is not None:
        _orch.stop_daemon()
    _rbtask = getattr(app.state, "review_bot_task", None)
    if _rbtask is not None:
        _rbtask.cancel()
```

在 `training_dingtalk.py` 补一个软降级用的 `_NoopNotifier`（凭证不齐时 orchestrator 用它，push 静默 no-op）：
```python
class _NoopNotifier:
    """凭证未配时的空 notifier（push 静默 no-op），让 orchestrator 仍能跑（仅无人审推送）。"""
    def push(self, loop_id: str, text: str) -> None:
        logger.debug("NoopNotifier push（凭证未配，loop=%s）：%s", loop_id, text[:60])
```

- [ ] **Step 2: main.py 注册 training router**

在 `server/main.py` import 段（仿 L43 `from server.api.v1.caisen import router as caisen_router`）追加：
```python
from server.api.v1.training import router as training_router
```
在 include_router 段（仿 L218 `app.include_router(caisen_router, ...)`）追加：
```python
app.include_router(training_router, prefix="/api/v1", dependencies=[Depends(require_write)])
```
（与 caisen/review 一致带 `require_write`——训练提交是写操作；钉钉审核进程内不走 HTTP 不受此限。）

- [ ] **Step 3: 跑 training 全套 + 不回归既有套件**

Run: `python -m pytest tests/caisen/test_training_loops_db.py tests/caisen/test_training_analyzer.py tests/caisen/test_training_loop.py tests/caisen/test_training_dingtalk.py tests/test_training_api.py -v`
Expected: 全绿（约 23 测试）

Run: `python -m pytest tests/ -q` （既有全套）
Expected: 既有测试不回归（关注 replay_tasks_db/review_service/bridge 相关是否受影响——新代码是纯新增 + main.py 装配块用 try/except 隔离，理论零回归）

- [ ] **Step 4: 冒烟验证 lifespan 装配不阻断启动**

Run: `python -c "from server.main import app; print('lifespan 装配 OK，routes:', [r.path for r in app.routes if '/training' in getattr(r,'path','')])"`
Expected: 打印 training 路由列表 + 无异常（凭证未配时走软降级分支）

- [ ] **Step 5: Commit**

```bash
git add server/main.py caisen/training_dingtalk.py
git commit -m "feat(training): Task7 lifespan 装配 orchestrator+review bot+router+重启恢复"
```

---

## Self-Review（plan 自检 · 对照 spec §1-§12）

**1. Spec 覆盖：**
- §2 决策表：人审闭环 ✅(Task3)；独立钉钉应用 ✅(Task4 `REVIEW_*` 隔离)；专门审核 ✅(Task4 handler)；stream 双向 ✅；loop 编排 daemon ✅(Task3)；方案 B 自由文本+GLM 解析+回显 ✅(Task3 `_confirm`)；N 轮上限默认 20 ✅(Task6 schema)；cfg 累积+重置 ✅(Task3 `_apply_confirmed`)；AI 带历史摘要 ✅(Task2 `analyze_round` history[-5:])；寄生 uvicorn ✅(Task7)。
- §4 状态机全路径 ✅(Task3 `_step_once` + 测试)。
- §5 存储模型 `training_loops` 表 ✅(Task1，字段对齐)。
- §6 AI 分析/解析/值域护栏/历史 context ✅(Task2)。
- §7 钉钉机器人：独立应用 ✅；寄生 uvicorn ✅(Task7)；专门审核 ✅；白名单 ✅(Task4)；Markdown 清洗复用 `clean_markdown_for_dingtalk` ✅。**主动推机制细化**：stream 不能主动发 → batch send（全局约束 §1 已述，Task4 实现）。
- §8 4 端点 ✅(Task6)。
- §9 边界：GLM 误解析回显兜底 ✅；GLM 不可用降级 ✅(Task2)；回测 FAILED 进 AWAITING_REVIEW ✅(Task3)；并发 loop=1 ✅(Task3/6)；审核超时 → **部分覆盖**（`_handle_awaiting_review` 阻塞等待，spec §9 的 24h 超时标 STOPPED 未显式实现，作为已知 gap 见下）。
- §10 测试策略：状态机全路径 ✅；analyze/parse mock ✅；回显确认 ✅；值域护栏 ✅；历史喂入 ✅；重启恢复 ✅；钉钉消息流 mock ✅(Task4)。
- §12 待用户提供：`REVIEW_APP_KEY/SECRET/STAFF_IDS`（已在全局约束 §1 + Task4 `from_env` 体现）。

**2. 已知 gap（实现时按需补，非阻塞）：**
- **审核超时 24h 标 STOPPED**（spec §9）：当前 `_handle_awaiting_review` 无限阻塞等审核（仅响应 stop）。补法：记录 loop 进 AWAITING_REVIEW 的时间戳，`_loop` daemon 周期检查超 24h（可配）→ STOPPED。建议作为 Task 3 的 follow-up 子步骤或单独小 task。
- **前端 `/training` 页面**：spec 未要求前端（训练是钉钉驱动动线），但 `GET /training` 端点已就绪供后续前端或钉钉状态查询。前端非本 Spec 范围。
- **GLM 真联调**：单测全 mock `_call_glm`；真凭证联调需用户提供 `GLM_API_KEY` + `REVIEW_*` 后手测（属部署验证，非单测）。

**3. 占位符扫描：** 无 TBD/TODO/"add error handling" 泛语；每步含真实代码或真实测试。Task 3 `_now_iso`/`json` import 在「实现者注意」显式标出（非占位符，是明确的补 import 指令）。Task 4 `active_loop_id` 在「注意」标出 Task 5 补（依赖链清晰）。

**4. 类型一致性：** `TrainingNotifier.push(loop_id, text)` 在 Task3 定义、Task4 `DingTalkNotifier.push` 实现、Task5 接线一致；`analyze_round(report, cfg, history)`/`parse_review(text, cfg)` 签名跨 Task2/3 一致；`submit_review(loop_id, text)`/`active_loop_id` 跨 Task3/4/5 一致；`/training/start` 请求体字段跨 Task6 schema 与 Task3 `start()` 入参一致。

---

## Execution Handoff

Plan 完成并存于 `docs/superpowers/plans/2026-07-15-caisen-ai-training-loop.md`。两种执行方式：

**1. Subagent-Driven（推荐）** — 每个 task 派一个全新 subagent 实现 + 两阶段 review，快速迭代（7 个 task 依赖链清晰，适合逐个派发）。

**2. Inline Execution** — 本会话内用 executing-plans 批量执行 + checkpoint review。
