# -*- coding: utf-8 -*-
"""观测层 schtasks 配置化管理（方案 C · 数据链 + 播报 supervisor）。

⚠️ C-2 scheduling-orchestration Task 9 收口（Final Fix 强化）：
  QuanterDataPipeline + QuanterBrief 的职责已由 engine 的 ``pipeline_then_eod`` cron
  事件链接管（在 uvicorn lifespan 内跑，采集→校验→eod→brief 一条链）。这两个 schtasks
  已**退役**——``register()`` **绝不重建它们**（否则与新事件链重复触发：采集跑两遍 /
  brief 推两份）。``RETIRED_TASKS`` 名单用于 ``--unregister-pipeline-brief`` /
  ``--unregister`` / ``--register`` 三处清退残留（幂等）。

  ``PIPELINE_TASKS`` 仅作为历史元数据保留，供 ``--list`` / ``--rerun`` 对**历史已注册
  环境**做清查/手工补跑；``register()`` 不再迭代它创建任务（Final Fix 修正：原实现
  会复活两个退役任务，与 ``--unregister-pipeline-brief`` 互相打架）。

  历史路径（方案 C · 7→3 收敛）的 ``QuanterDiscoveryDaemon @02:00`` 仍由
  ``discovery/schtasks.py`` 独立注册，本脚本不管。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# ⚠️ C-2 Task 9：以下两个 schtasks 已退役，职责收进 uvicorn 的 pipeline_then_eod cron
#   事件链。register()/unregister()/unregister_pipeline_brief() 清退时用这份名单删
#   （幂等 /Delete /F，不存在不报错）。绝不可在 register() 里 /Create 它们。
RETIRED_TASKS = ["QuanterDataPipeline", "QuanterBrief"]

# 方案 C 历史的 2 个 supervisor 任务定义（时间 + bat 路径）。
# ⚠️ Final Fix：``register()`` 不再迭代本表创建任务（两个任务已退役，重建=与新事件链
#   重复触发）。本表仅保留为历史元数据，供 ``--list`` / ``--rerun`` 对已注册环境清查。
PIPELINE_TASKS = [
    # (任务名, 时间, bat 相对路径) — 已退役，勿在 register() 中 /Create
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
    """幂等清退：删除历史 6 个零散任务 + 2 个退役 schtasks（防残留）。

    ⚠️ Final Fix（C-2 Task 9）：原实现在此 /Create 两个 supervisor，但这两个任务已退役
    （职责收进 uvicorn 的 ``pipeline_then_eod`` cron 事件链）。重建它们会造成双重触发：
    采集@17:00(schtasks) + 事件链采集(uvicron)、brief 推两份。故本函数现在只**清退**、
    **不创建**任何 schtasks。新部署由 ``start_all.py`` 调 ``--unregister-pipeline-brief``
    兜底清退残留。
    """
    # 清退历史 6 个零散任务（幂等，不存在不报错）
    for task in LEGACY_TASKS:
        _schtasks(["/Delete", "/TN", task, "/F"])
    print(f"清退 {len(LEGACY_TASKS)} 个历史零散任务")
    # 清退 2 个退役 schtasks（防残留——已收编进 uvicorn 事件链，绝不重建）
    for task in RETIRED_TASKS:
        _schtasks(["/Delete", "/TN", task, "/F"])
    print(f"清退 {len(RETIRED_TASKS)} 个退役 schtasks（已收编进 uvicorn，未重建）")


def unregister() -> None:
    """一键清退全部（2 个退役 schtasks + 历史 6 个零散任务，删除幂等）。"""
    for task in RETIRED_TASKS:
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
    Why 只删 RETIRED_TASKS（不动 LEGACY_TASKS）：LEGACY 已由 ``--register`` /
        ``--unregister`` 覆盖；本子命令聚焦「Task 9 收编的两个」，语义清晰供 start_all
        单独调用。
    """
    for task in RETIRED_TASKS:
        rc = _schtasks(["/Delete", "/TN", task, "/F"])
        # rc=0 删成；非零多半是任务不存在（已清退环境），均视为成功（幂等语义）。
        print(f"{'deleted' if rc == 0 else 'skip(not exists)'} {task}")


def list_tasks() -> None:
    """逐个 /Query 历史注册的 2 个 supervisor 任务（供运维清查残留，未必仍存在）。"""
    for task, _, _ in PIPELINE_TASKS:
        subprocess.run(["schtasks", "/Query", "/TN", task], check=False)


def rerun(task_key: str) -> None:
    """手工触发某历史 supervisor（不等时间到，立即跑）：data / brief。

    ⚠️ 两个 supervisor 已退役（职责在 uvicorn 事件链内），此处仅保留供对**历史已注册
    环境**手工补跑/排查；新部署不应再依赖此入口。
    """
    mapping = {"data": "QuanterDataPipeline", "brief": "QuanterBrief"}
    task = mapping.get(task_key)
    if not task:
        print(f"未知 task={task_key}，支持：{list(mapping)}")
        sys.exit(1)
    subprocess.run(["schtasks", "/Run", "/TN", task], check=False)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="观测层 schtasks 管理（Final Fix · supervisor 已退役，register 仅清退）"
    )
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--list", action="store_true")
    g.add_argument("--register", action="store_true",
                   help="清退历史零散 + 退役 schtasks（不再创建；Task 9 收口）")
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
