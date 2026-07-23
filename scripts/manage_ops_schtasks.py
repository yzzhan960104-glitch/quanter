# -*- coding: utf-8 -*-
"""观测层播报 schtasks 配置化管理（一期）。

读 .env 的 *_BRIEF_TIME，幂等注册/列出/删除 3 个每日播报任务。
改时间 = 改 .env + python manage_ops_schtasks.py --register（先删后建，幂等）。

第二期交易引擎引入 APScheduler 后，播报调度可迁移进程内，本脚本留作 fallback。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# bot → schtasks 任务名
TASK_NAMES = {
    "trading": "QuanterTradingBrief",
    "strategy": "QuanterStrategyBrief",
    "data": "QuanterDataBrief",
}
# bot → .env 时间变量名 + 默认时间
BOT_TIME_ENV = {
    "trading": ("TRADING_BRIEF_TIME", "15:30"),
    "strategy": ("STRATEGY_BRIEF_TIME", "16:00"),
    "data": ("DATA_BRIEF_TIME", "17:00"),
}

# 数据检查点任务（auto-trading-rehearsal Task 4）
# Why 独立 list 不复用 bot dict：检查点 bat 命名 run_data_check_t1/t2.bat（非 run_{bot}_brief.bat
# 套路），且时间硬编码不读 .env（17:00 查T-1 / 18:30 查T 是 brainstorm 钉死的双检查点时序，
# 调度漂移会破坏"盘前 T-1 告警 → 盘后 T 重采熔断"语义，故不做 env 化）。
DATA_CHECK_TASKS = [
    # (任务名, 时间, bat 相对路径)
    ("QuanterDataCheckT1", "17:00", "scripts\\run_data_check_t1.bat"),
    ("QuanterDataCheckT2", "18:30", "scripts\\run_data_check_t2.bat"),
]


def _load_env() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except ImportError:
        pass


def build_register_commands() -> list[dict]:
    """生成 3 个 schtasks 注册命令参数（不执行）。先 /Delete 再 /Create 保证幂等。

    Why 拆纯函数：让单测能在不触发 subprocess（不污染 Windows 任务计划程序）的
    前提下，验证"读 .env 时间 → 任务名 → bat 路径"三段映射的回归。
    """
    _load_env()
    out = []
    for bot, task in TASK_NAMES.items():
        env_key, default = BOT_TIME_ENV[bot]
        time = os.getenv(env_key, default)
        bat = str(ROOT / "scripts" / f"run_{bot}_brief.bat")
        out.append({"task": task, "time": time, "bat": bat, "bot": bot})
    return out


def _schtasks(args: list[str]) -> int:
    """封装 schtasks 子进程调用。capture_output 避免乱码打屏，text=True 直接拿 str。"""
    return subprocess.run(["schtasks"] + args, capture_output=True, text=True).returncode


def register() -> None:
    """幂等注册：先 /Delete /F（不存在也返回 0，不报错）再 /Create /F 覆盖。

    覆盖两类任务：bot 播报（3 个，读 .env 时间）+ 数据检查点（2 个，硬编码时间）。
    """
    # bot 播报任务
    for c in build_register_commands():
        _schtasks(["/Delete", "/TN", c["task"], "/F"])  # 幂等：先删
        rc = _schtasks(["/Create", "/SC", "DAILY", "/TN", c["task"],
                        "/TR", c["bat"], "/ST", c["time"], "/F"])
        print(f"{'OK' if rc == 0 else 'FAIL'} {c['task']} @ {c['time']} → {c['bat']}")
    # 数据检查点任务
    for task, t, bat_rel in DATA_CHECK_TASKS:
        bat = str(ROOT / bat_rel)
        _schtasks(["/Delete", "/TN", task, "/F"])  # 幂等：先删
        rc = _schtasks(["/Create", "/SC", "DAILY", "/TN", task,
                        "/TR", bat, "/ST", t, "/F"])
        print(f"{'OK' if rc == 0 else 'FAIL'} {task} @ {t} → {bat}")


def unregister() -> None:
    """一键清退全部任务（删除是幂等的，不存在不报错）。"""
    for task in TASK_NAMES.values():
        _schtasks(["/Delete", "/TN", task, "/F"])
        print(f"deleted {task}")
    for task, _, _ in DATA_CHECK_TASKS:
        _schtasks(["/Delete", "/TN", task, "/F"])
        print(f"deleted {task}")


def list_tasks() -> None:
    """逐个 /Query：schtasks 没有"按前缀过滤"原生能力，逐个查最直白。"""
    subprocess.run(["schtasks", "/Query", "/TN", "QuanterTradingBrief"], check=False)
    subprocess.run(["schtasks", "/Query", "/TN", "QuanterStrategyBrief"], check=False)
    subprocess.run(["schtasks", "/Query", "/TN", "QuanterDataBrief"], check=False)
    for task, _, _ in DATA_CHECK_TASKS:
        subprocess.run(["schtasks", "/Query", "/TN", task], check=False)


def rerun(bot: str) -> None:
    """手工触发某个 bot 的播报（不等时间到，立即跑一次 bat）。"""
    task = TASK_NAMES.get(bot)
    if not task:
        print(f"未知 bot={bot}，支持：{list(TASK_NAMES)}")
        sys.exit(1)
    subprocess.run(["schtasks", "/Run", "/TN", task], check=False)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="观测层播报 schtasks 管理")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--list", action="store_true")
    g.add_argument("--register", action="store_true")
    g.add_argument("--unregister", action="store_true")
    g.add_argument("--rerun", metavar="BOT")
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
