# -*- coding: utf-8 -*-
"""discovery 夜跑 daemon 的 schtasks 管理（spec §10，Plan 4 Task 4 / T8 C-9 A3）。

Why 包自包含调度（不依赖 ops/manage_ops_schtasks.py）：
  - discovery 是 L4 生产入口，调度配置必须跟随包走——scripts/ 后续废弃时 discovery
    夜跑不能断链。把 DAEMON_TASK_NAME/TIME/BAT 常量 + unregister/list 收进包内，
    `python -m discovery.schtasks --unregister` 一键自治清退残留。
  - 与 broadcast（scripts/manage_ops_schtasks）解耦：两者调度对象/时序无关
    （broadcast 是盘后播报，daemon 是 02:00 跑批），合并管理会引入虚假耦合。

⚠️ T8 / C-9 A3 退役（C-7 V2 收编后）：``QuanterDiscoveryDaemon`` 的 schtasks DAILY 02:00
  已收编进 uvicorn lifespan 的 ``engine.sched`` cron 02:00（见 presentation/server/main.py），
  且 lifespan 启动补跑会读 search_run 最新 started_at 兜底漏跑。``register()`` **拒绝重建**
  （旧实现幂等"先 /Delete /F 再 /Create /SC DAILY"会复活该任务），否则 lifespan cron +
  schtasks 同一晚双触发——双进程读 discovery_trials.db 锁竞争 + 4h budget × 2 浪费。

  本模块仅保留 ``--unregister``（清残留）/ ``--list``（查状态）两个有效入口；
  ``--register`` 打印退役提示并返非零（main 的 argparse 仍接受该 flag 以保持向后兼容，
  避免旧脚本/cron 调 ``--register`` 时 argparse 报错中断）。
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
    """已退役（C-7 V2 收编 lifespan cron 02:00）：拒绝重建，防双跑。

    物理意图（T8 / C-9 A3）：``QuanterDiscoveryDaemon`` 的 schtasks DAILY 02:00 已收编进
    uvicorn lifespan 的 ``engine.sched`` cron 02:00（C-7 V2，见 presentation/server/main.py），
    且 lifespan 启动补跑会读 search_run 最新 started_at 兜底漏跑（C-8）。旧实现的幂等序
    （先 /Delete /F 再 /Create /SC DAILY）会**复活**该任务，导致同一晚 lifespan cron +
    schtasks 双触发——双进程同时读 discovery_trials.db 有 SQLite 锁竞争风险，且 4h budget
    被吃两份。故本函数改为只打印退役提示，不发任何 schtasks 子进程命令（不 /Create、不
    /Delete；``--unregister`` 子命令独立清残留）。

    Why 不删函数 / 不删 --register flag：保持向后兼容——旧脚本 / cron / 文档可能仍调
    ``python -m discovery.schtasks --register``，删 flag 会让 argparse 报错中断（隐性失败
    比显式拒绝危险）。改为打印退役信息 + main 返非零，运维一眼看到"已退役"提示。
    """
    print("QuanterDiscoveryDaemon 已退役：discovery 收编 uvicorn lifespan "
          "（engine.sched cron 02:00 + 启动补跑）。禁止重建，请用 --unregister 清残留。")


def unregister() -> None:
    """一键清退（删除幂等：任务不存在 /Delete /F 也返 0，不报错）。"""
    _schtasks(["/Delete", "/TN", DAEMON_TASK_NAME, "/F"])
    print(f"deleted {DAEMON_TASK_NAME}")


def list_tasks() -> None:
    """查 daemon 任务当前状态（schtasks /Query /TN 直出，给人眼看时间/上次结果）。"""
    subprocess.run(["schtasks", "/Query", "/TN", DAEMON_TASK_NAME], check=False)


def main(argv=None) -> int:
    """cli 入口：--register/--unregister/--list 三选一（互斥）。

    argv=None 走 sys.argv（``python -m discovery.schtasks --unregister``）。

    ⚠️ T8 / C-9 A3：``--register`` 已退役（收编 lifespan cron 02:00），调用时打印退役
    提示并返 ``1``（非零——供运维脚本/CI 判失败）。``--unregister`` / ``--list`` 仍返 0。

    Why 保留 ``--register`` flag：向后兼容旧脚本/cron（删 flag 会让 argparse 报错中断，
    隐性失败比显式拒绝危险）；改为打印 + 返非零，运维一眼看到"已退役"。
    """
    p = argparse.ArgumentParser(description="discovery daemon schtasks 管理（包自包含）")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--register", action="store_true",
                   help="已退役（C-7 V2 收编 lifespan）——打印提示并返非零")
    g.add_argument("--unregister", action="store_true", help="清退残留夜跑任务")
    g.add_argument("--list", action="store_true", help="查询任务当前状态")
    args = p.parse_args(argv)
    if args.register:
        register()  # 已退役：只打印提示
        return 1     # 非零：--register 不再是有效操作（防双跑）
    elif args.unregister:
        unregister()
    elif args.list:
        list_tasks()
    return 0


if __name__ == "__main__":
    sys.exit(main())
