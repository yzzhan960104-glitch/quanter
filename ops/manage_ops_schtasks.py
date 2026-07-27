# -*- coding: utf-8 -*-
"""观测层 schtasks 配置化管理（方案 C · 数据链 + 播报 supervisor）。

schtasks 从 7 个收敛到 3 个（本脚本管 2 个 + discovery/schtasks.py 独立管 1 个）：
  - QuanterDataPipeline @17:00（supervisor：T1→采集→T2 串行，ops/data_pipeline.py）
  - QuanterBrief        @18:00（supervisor：Trading+Strategy+Data，ops/brief_all.py；
    在 pipeline 之后，修正历史 DataBrief@17:00 在采集@17:30 前的顺序 bug）
  - QuanterDiscoveryDaemon @02:00（独立，由 discovery/schtasks.py 注册，本脚本不管）

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
    g.add_argument("--rerun", metavar="TASK", help="data | brief")
    args = p.parse_args(argv)
    if args.register:
        register()
    elif args.unregister:
        unregister()
    elif args.list:
        list_tasks()
    elif args.rerun:
        rerun(args.rerun)
    return 0


if __name__ == "__main__":
    sys.exit(main())
