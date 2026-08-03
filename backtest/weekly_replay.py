# -*- coding: utf-8 -*-
"""周度「近期回测」自动提交（2026-08-03 新增，修复策略播报回测半个月不更新）。

背景（病灶）：策略机器人「近期回测」读的是老 JSON 归档（replay_runs/index.json），
而当前回测链路（Spec 1）已迁到 SQLite（data/replay_tasks.db，worker 结果写
report_json）——归档 07-14 后再无新条目，且没有任何周期性生产端提交回测任务
（前端写按钮 08-02 撤除），播报只能显示半月前的旧数据。

本模块提供唯一职责：**当最近一次回测任务距今超过阈值时，自动提交一个新任务**。
任务参数：
    - strategy_name = neckline；
    - cfg_override = logs/param_iter_state.json 的冠军参数（best）；
    - universe = None（全市场，与历史近期回测口径一致）；
    - 窗口 = 最近 3 个月 → 今天。

幂等：以 replay_tasks 最近任务 created_at 为闸——阈值内不重复提交；存在未终态
（PENDING/RUNNING）任务也不重复提交（避免调度器堆积双跑）。
提交动作本身只 INSERT 一行 PENDING，实际回测由 ReplayScheduler（uvicorn 内
daemon）异步派发 worker 执行——本函数绝不阻塞、绝不跑重活。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from backtest import tasks_db as replay_tasks_db

logger = logging.getLogger(__name__)

_STATE_FILE = "logs/param_iter_state.json"
_WINDOW_DAYS = 90      # 回测窗口：最近 3 个月
_MIN_INTERVAL_DAYS = 7  # 距上次任务 ≥7 天才提交下一次


def _champion_cfg_override() -> dict:
    """读参数迭代冠军参数（logs/param_iter_state.json 的 best）；失败返 {}。"""
    try:
        with open(_STATE_FILE, "r", encoding="utf-8") as f:
            payload = json.load(f)
        best = payload.get("best") if isinstance(payload, dict) else None
        return best if isinstance(best, dict) else {}
    except Exception:
        logger.warning("读 %s 冠军参数失败，周度回测用默认参数", _STATE_FILE, exc_info=True)
        return {}


def maybe_enqueue_weekly_replay(now: datetime | None = None,
                                db_path: str | None = None) -> str | None:
    """检查最近回测任务是否过期；过期则提交一个新任务。

    Args:
        now: 当前时间（测试注入；缺省 datetime.now()）。
        db_path: replay_tasks.db 路径（测试注入；None 走 tasks_db 默认）。

    Returns:
        新任务 task_id；无需提交（间隔内 / 已有未终态任务 / 任何异常）返 None。
    """
    now = now or datetime.now()
    try:
        replay_tasks_db.init_db(db_path)
        tasks = replay_tasks_db.list_tasks(limit=100, path=db_path)
    except Exception:
        logger.exception("周度回测检查失败（跳过本次提交）")
        return None

    # 存在未终态任务（PENDING/RUNNING）→ 不重复提交（等调度器消化）
    if any(t.get("status") in ("PENDING", "RUNNING") for t in tasks):
        return None

    # 最近一次任务距今 < 阈值 → 不提交（保持每周一条的节奏）
    if tasks:
        latest_created = tasks[0].get("created_at")  # list_tasks 按 created_at 降序
        try:
            latest_dt = datetime.fromisoformat(latest_created)
        except (TypeError, ValueError):
            latest_dt = None
        if latest_dt is not None and (now - latest_dt).total_seconds() < \
                _MIN_INTERVAL_DAYS * 86400:
            return None

    # 提交：全市场 × 冠军参数 × 最近 3 个月
    end = now.date().isoformat()
    start = (now.date() - timedelta(days=_WINDOW_DAYS)).isoformat()
    try:
        task_id = replay_tasks_db.create_task({
            "strategy_name": "neckline",
            "start": start,
            "end": end,
            "universe": None,                      # 全市场（universe_n=-1）
            "cfg_override": _champion_cfg_override(),
        }, path=db_path)
    except Exception:
        logger.exception("提交周度回测任务失败")
        return None
    logger.info("周度近期回测已提交 task=%s 窗口=%s~%s", task_id, start, end)
    return task_id
