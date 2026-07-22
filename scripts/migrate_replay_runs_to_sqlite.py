# -*- coding: utf-8 -*-
"""replay_runs JSON → SQLite 一次性迁移（Spec 1 · Task 8）。

物理定位：把老 replay_runs/<run_id>.json 历史回测归档导入 replay_tasks.db（SUCCESS 行），
让 SQLite 成为历史回测的统一查询源（为后续 list/get 切 SQLite 铺垫）。

源 JSON 结构（对齐 caisen.replay_runs.save_run 落盘格式）：
    {run_id, created_at, request:{start,end,universe,cfg_override,save}, summary, report}
迁移映射：
    run_id     → task_id
    created_at → created_at（同时作 finished_at 近似——落盘时刻≈完成时刻）
    request    → start / end / universe_json / cfg_override
    report     → report_json（完整 ReplayReport，内嵌 SUCCESS 行）
    status=SUCCESS, progress=100

幂等：已存在的 task_id 跳过（防重复迁移）。损坏 JSON 跳过不阻断整批。
老 JSON 目录保留只读归档（不删——同步 /replay 链路仍在用，Task 8 不删 replay_runs.py）。
"""
import glob
import json
import os
import sys

# 脚本在 scripts/，项目根在上一级——加入 sys.path 让 `from caisen import` 可达。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backtest import tasks_db as replay_tasks_db  # noqa: E402（sys.path 调整在 import 前，故禁用告警）


def migrate(runs_dir: str = "replay_runs", db_path: str = "data/replay_tasks.db") -> int:
    """遍历 replay_runs/*.json → 每条 INSERT 为 SUCCESS 行。返回实际迁移条数。

    参数：
        runs_dir: replay_runs 目录（默认生产路径）。
        db_path:  SQLite 任务表路径（默认 data/replay_tasks.db）。
    """
    replay_tasks_db.init_db(db_path)
    n = 0
    for fp in glob.glob(os.path.join(runs_dir, "*.json")):
        if os.path.basename(fp) == "index.json":
            continue   # index 是摘要列表（非单次 run 记录），跳过
        try:
            with open(fp, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue   # 损坏 JSON 跳过（不阻断整批迁移）
        if not isinstance(data, dict):
            continue
        tid = data.get("run_id") or os.path.basename(fp)[:-5]
        if replay_tasks_db.get_task(tid, db_path) is not None:
            continue   # 幂等：已迁移过的 task_id 跳过
        req = data.get("request") or {}
        report = data.get("report") or {}
        universe = req.get("universe")
        universe_n = -1 if universe is None else len(universe)
        created = data.get("created_at") or replay_tasks_db._now_iso()
        with replay_tasks_db._connect(db_path) as conn:
            conn.execute(
                """INSERT INTO replay_tasks
                   (task_id, created_at, status, progress, start, end, universe_n,
                    universe_json, cfg_override, report_json, finished_at)
                   VALUES (?, ?, 'SUCCESS', 100, ?, ?, ?, ?, ?, ?, ?)""",
                (tid, created,
                 req.get("start"), req.get("end"), universe_n,
                 json.dumps(universe),
                 json.dumps(req.get("cfg_override") or {}, ensure_ascii=False),
                 json.dumps(report, ensure_ascii=False),
                 created))
        n += 1
    print(f"迁移完成：{n} 条 replay_runs → {db_path}")
    return n


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="replay_runs JSON → SQLite 一次性迁移")
    ap.add_argument("--runs-dir", default="replay_runs", help="replay_runs 目录")
    ap.add_argument("--db", default="data/replay_tasks.db", help="目标 SQLite 路径")
    a = ap.parse_args()
    migrate(a.runs_dir, a.db)
