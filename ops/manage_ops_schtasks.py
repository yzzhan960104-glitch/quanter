# -*- coding: utf-8 -*-
"""观测层 schtasks 配置化管理（方案 C · 数据链 + 播报 supervisor）。

schtasks 从 7 个收敛到 3 个（本脚本管 2 个 + discovery/schtasks.py 独立管 1 个）：
  - QuanterDataPipeline @17:00（supervisor：T1→采集→T2 串行，ops/data_pipeline.py）
  - QuanterBrief        @18:00（supervisor：Trading+Strategy+Data，ops/brief_all.py；
    在 pipeline 之后，修正历史 DataBrief@17:00 在采集@17:30 前的顺序 bug）
  - QuanterDiscoveryDaemon @02:00（独立，由 discovery/schtasks.py 注册，本脚本不管）

⚠️ C-2 scheduling-orchestration Task 9 收口：
  QuanterDataPipeline + QuanterBrief 的职责已由 engine 的 ``pipeline_then_eod`` cron
  事件链接管（在 uvicorn lifespan 内跑，采集→校验→eod→brief 一条链）。这两个 schtasks
  不再注册——下方 PIPELINE_TASKS 列表保留是为了 ``--unregister`` / ``--list`` /
  ``--rerun`` 等子命令对历史已注册环境的清理能力，新部署由 ``--unregister-pipeline-brief``
  幂等清退残留（start_all.py 每次启动自动调一次）。

改时间 = 改下方 PIPELINE_TASKS + python manage_ops_schtasks.py --register（先删后建，幂等）。
--register 同时清退历史 6 个零散任务（QuanterTradingBrief/StrategyBrief/DataBrief/
DataCheckT1/DailyIncremental/DataCheckT2），防残留。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 方案 C：2 个 supervisor 任务（原 6 个零散任务合并）
# 时序：DataPipeline @17:00 须早于 Brief @18:00（Brief 的 DataBrief 反映采集后状态）
# ⚠️ C-2 Task 9：这两个任务的职责已收进 uvicorn 的 pipeline_then_eod cron 事件链，
#   不再注册新实例（列表保留供 --unregister/--list/--rerun 清理历史环境）。
PIPELINE_TASKS = [
    # (任务名, 时间, bat 相对路径)
    ("QuanterDataPipeline", "17:00", "scripts\\run_data_pipeline.bat"),
    ("QuanterBrief",        "18:00", "scripts\\run_brief_all.bat"),
]

# 历史 6 个零散任务（--register / --unregister 时清理，防残留）
LEGACY_TASKS = [
    "QuanterTradingBrief",
    "QuanterStrategyBrief",
    "QuanterDataBrief",
    "QuanterDataCheckT1",
    "QuanterDailyIncremental",
    "QuanterDataCheckT2",
]


def _schtasks(args: list[str]) -> int:
    """封装 schtasks 子进程调用。capture_output 避免乱码打屏，text=True 直接拿 str。"""
    return subprocess.run(["schtasks"] + args, capture_output=True, text=True).returncode


def register() -> None:
    """幂等注册：先清退历史 6 个零散任务 + 重建 2 个 supervisor（先 /Delete 再 /Create）。"""
    # 清退历史任务（幂等，不存在不报错）
    for task in LEGACY_TASKS:
        _schtasks(["/Delete", "/TN", task, "/F"])
    print(f"清退 {len(LEGACY_TASKS)} 个历史零散任务")
    # 注册 2 个 supervisor
    for task, t, bat_rel in PIPELINE_TASKS:
        bat = str(ROOT / bat_rel)
        _schtasks(["/Delete", "/TN", task, "/F"])  # 幂等先删
        rc = _schtasks(["/Create", "/SC", "DAILY", "/TN", task,
                        "/TR", bat, "/ST", t, "/F"])
        print(f"{'OK' if rc == 0 else 'FAIL'} {task} @ {t} → {bat}")


def unregister() -> None:
    """一键清退全部（2 个 supervisor + 历史 6 个，删除幂等）。"""
    for task, _, _ in PIPELINE_TASKS:
        _schtasks(["/Delete", "/TN", task, "/F"])
        print(f"deleted {task}")
    for task in LEGACY_TASKS:
        _schtasks(["/Delete", "/TN", task, "/F"])
        print(f"deleted (legacy) {task}")


def unregister_pipeline_brief() -> None:
    """清退已收编进 uvicorn 的 DataPipeline/Brief schtasks（幂等，防残留）。

    物理意图（C-2 scheduling-orchestration Task 9）：
        QuanterDataPipeline + QuanterBrief 的职责已由 engine 的 ``pipeline_then_eod``
        cron 事件链接管（在 uvicorn lifespan 内跑）。升级后的环境若不清退这两个旧
        schtasks，它们会与新事件链重复触发（采集跑两遍 / brief 推两份）。

    Why 幂等：``schtasks /Delete /F`` 对不存在的任务返非零但不抛，本函数对每个任务
        都调一次，已删环境再调无副作用（start_all.py 每次启动都跑一遍防残留）。
    Why 只删这两个（不动 LEGACY_TASKS）：LEGACY 已由 ``--register`` / ``--unregister``
        覆盖；本子命令聚焦「Task 9 收编的两个」，语义清晰供 start_all 单独调用。
    """
    for task in ("QuanterDataPipeline", "QuanterBrief"):
        rc = _schtasks(["/Delete", "/TN", task, "/F"])
        # rc=0 删成；非零多半是任务不存在（已清退环境），均视为成功（幂等语义）。
        print(f"{'deleted' if rc == 0 else 'skip(not exists)'} {task}")


def list_tasks() -> None:
    """逐个 /Query 当前 2 个 supervisor 任务。"""
    for task, _, _ in PIPELINE_TASKS:
        subprocess.run(["schtasks", "/Query", "/TN", task], check=False)


def rerun(task_key: str) -> None:
    """手工触发某 supervisor（不等时间到，立即跑）：data / brief。"""
    mapping = {"data": "QuanterDataPipeline", "brief": "QuanterBrief"}
    task = mapping.get(task_key)
    if not task:
        print(f"未知 task={task_key}，支持：{list(mapping)}")
        sys.exit(1)
    subprocess.run(["schtasks", "/Run", "/TN", task], check=False)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="观测层 schtasks 管理（方案 C · 2 个 supervisor）"
    )
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--list", action="store_true")
    g.add_argument("--register", action="store_true")
    g.add_argument("--unregister", action="store_true")
    g.add_argument("--unregister-pipeline-brief", action="store_true",
                   help="清退已收编进 uvicorn 的 DataPipeline/Brief（幂等，Task 9）")
    g.add_argument("--rerun", metavar="TASK", help="data | brief")
    args = p.parse_args(argv)
    if args.register:
        register()
    elif args.unregister:
        unregister()
    elif args.unregister_pipeline_brief:
        unregister_pipeline_brief()
    elif args.list:
        list_tasks()
    elif args.rerun:
        rerun(args.rerun)
    return 0


if __name__ == "__main__":
    sys.exit(main())
