# -*- coding: utf-8 -*-
"""discovery 夜跑 daemon 的 schtasks 注册（spec §10，Plan 4 Task 4）。

Why 包自包含调度（不依赖 ops/manage_ops_schtasks.py）：
  - discovery 是 L4 生产入口，调度配置必须跟随包走——scripts/ 后续废弃时 discovery
    夜跑不能断链。把 DAEMON_TASK_NAME/TIME/BAT 常量 + register/unregister 收进包内，
    `python -m discovery.schtasks --register` 一键自治。
  - 与 broadcast（scripts/manage_ops_schtasks）解耦：两者调度对象/时序无关
    （broadcast 是盘后播报，daemon 是 02:00 跑批），合并管理会引入虚假耦合。

幂等模式（先 /Delete /F 再 /Create /SC DAILY）复用 ops/manage_ops_schtasks.py
既有纪律：schtasks /Create 非 /F 遇到已存在会失败，先删保证重跑 register 不报错
（改时间 / 误触 / schtasks 多触发都安全）。

改夜跑时间 = 改本模块 DAEMON_TIME 常量 + `python -m discovery.schtasks --register`。
"""
from __future__ import annotations
import argparse
import subprocess
import sys
from pathlib import Path

# 项目根（discovery/ 的上级）——包内自治定位，不依赖运行时 cwd
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# schtasks 任务名（Windows 任务计划程序里的唯一标识，/TN 用）
DAEMON_TASK_NAME = "QuanterDiscoveryDaemon"
# 每夜 02:00 触发（spec §5.2/§10——盘后数据落湖 + broadcast 播报完成后，
# 避开 17:00-18:30 的 data_check/daily_incremental 时序，选低负载深夜跑批）
DAEMON_TIME = "02:00"
# bat 路径（包内 discovery/run_daemon.bat——包自包含的物理体现）
DAEMON_BAT = str(ROOT / "discovery" / "run_daemon.bat")


def build_register_commands() -> list[dict]:
    """生成 daemon 注册命令参数（纯函数，不执行）。

    Why 拆纯函数：让单测能在不触发 subprocess（不污染 Windows 任务计划程序）的
    前提下，验证"任务名 → 时间 → bat 路径"三段映射的回归（见 test_schtasks.py）。

    Returns:
      list[dict]：每元素含 task/time/bat/bot 四键（bot 字段保留以对齐
      ops/manage_ops_schtasks.py 的结构，便于一键 report 工具复用）。
    """
    return [{"task": DAEMON_TASK_NAME, "time": DAEMON_TIME, "bat": DAEMON_BAT, "bot": "discovery"}]


def _schtasks(args: list[str]) -> int:
    """封装 schtasks 子进程调用。

    capture_output=True 避免中文乱码打屏（Windows schtasks /Query 在非 UTF-8 cmd
    下会输出 GBK 杂字），text=True 直接拿 str 返回码供 register 判 OK/FAIL。
    """
    return subprocess.run(["schtasks"] + args, capture_output=True, text=True).returncode


def register() -> None:
    """幂等注册：先 /Delete /F（不存在也返 0）再 /Create /F 覆盖。

    幂等序保证：
      - 首次注册：/Delete 对不存在的任务返 0（schtasks 对 /TN 不存在的 /Delete /F
        不报错），紧接 /Create 落任务。
      - 重跑注册（改时间后）：先 /Delete 删旧任务，再 /Create 覆盖——避免 /Create
        非 /F 模式遇到已存在报"任务已存在"错。
    """
    for c in build_register_commands():
        _schtasks(["/Delete", "/TN", c["task"], "/F"])   # 幂等：先删（不存在也返 0）
        rc = _schtasks(["/Create", "/SC", "DAILY", "/TN", c["task"],
                        "/TR", c["bat"], "/ST", c["time"], "/F"])
        print(f"{'OK' if rc == 0 else 'FAIL'} {c['task']} @ {c['time']} → {c['bat']}")


def unregister() -> None:
    """一键清退（删除幂等：任务不存在 /Delete /F 也返 0，不报错）。"""
    _schtasks(["/Delete", "/TN", DAEMON_TASK_NAME, "/F"])
    print(f"deleted {DAEMON_TASK_NAME}")


def list_tasks() -> None:
    """查 daemon 任务当前状态（schtasks /Query /TN 直出，给人眼看时间/上次结果）。"""
    subprocess.run(["schtasks", "/Query", "/TN", DAEMON_TASK_NAME], check=False)


def main(argv=None) -> int:
    """cli 入口：--register/--unregister/--list 三选一（互斥）。

    argv=None 走 sys.argv（`python -m discovery.schtasks --register`）。
    """
    p = argparse.ArgumentParser(description="discovery daemon schtasks 管理（包自包含）")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--register", action="store_true", help="幂等注册夜跑任务（先删后建）")
    g.add_argument("--unregister", action="store_true", help="清退夜跑任务")
    g.add_argument("--list", action="store_true", help="查询任务当前状态")
    args = p.parse_args(argv)
    if args.register:
        register()
    elif args.unregister:
        unregister()
    elif args.list:
        list_tasks()
    return 0


if __name__ == "__main__":
    sys.exit(main())
