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

  C-7 start-all 收编：``QuanterDiscoveryDaemon @02:00`` 已从 schtasks 收编进 uvicorn
  lifespan 的 engine.sched cron 02:00（见 presentation/server/main.py）。本脚本新增
  ``--unregister-discovery`` 退订残留 QuanterDiscoveryDaemon（防 lifespan + schtasks
  双触发），``--register-server`` 注册 QuanterServer ONSTART（替代已删的 start_all.py
  启动链，开机 session 0 后台起 ``python -m trading``）。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# ⚠️ C-2 Task 9 / C-8 / T8：以下 schtasks 已退役，职责收进 uvicorn 的 pipeline_then_eod
#   cron 事件链（QuanterDataPipeline / QuanterBrief）或已从系统删除（QuanterDailyBrief）。
#   register()/unregister()/unregister_pipeline_brief() 清退时用这份名单删（幂等
#   /Delete /F，不存在不报错）。绝不可在 register() 里 /Create 它们。
# T8 补 QuanterDailyBrief：C-8 后该系统 schtasks 已删，加入名单幂等清退防被误重建后残留。
RETIRED_TASKS = ["QuanterDataPipeline", "QuanterBrief", "QuanterDailyBrief"]

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
    **不创建**任何 schtasks。C-7 后新部署由 ``QuanterServer`` schtasks ONSTART 起
    ``python -m trading``（lifespan 装 engine/connect/discovery），残留清退由运维调
    ``--unregister-pipeline-brief`` / ``--unregister-discovery``。
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
        都调一次，已删环境再调无副作用（C-7 前 start_all.py 每次启动跑一遍防残留；
        start_all.py 已删，改由运维或新部署脚本按需调用）。
    Why 只删 RETIRED_TASKS（不动 LEGACY_TASKS）：LEGACY 已由 ``--register`` /
        ``--unregister`` 覆盖；本子命令聚焦「Task 9 收编的两个」，语义清晰供运维单独调用。
    """
    for task in RETIRED_TASKS:
        rc = _schtasks(["/Delete", "/TN", task, "/F"])
        # rc=0 删成；非零多半是任务不存在（已清退环境），均视为成功（幂等语义）。
        print(f"{'deleted' if rc == 0 else 'skip(not exists)'} {task}")


def register_server(user: str | None = None) -> None:
    """A4：注册 QuanterServer = BootTrigger(ONSTART) + RestartOnFailure。

    物理意图（spec §7.1-6 · 08-06 实测 LogonTrigger 且无重启策略）：ONSTART 开机 session 0
    后台起引擎（不依赖登录），RestartOnFailure 崩溃自愈（3 次/1 分钟）。schtasks CLI 不支持
    RestartOnFailure，改走 PowerShell ScheduledTasks（S4U 登录类型：本地 bat 无网络需求，
    不存密码）；失败回退 schtasks /SC ONSTART 并打印手动补配提示。
    """
    user = user or os.environ.get("USERNAME", "")
    rc = _register_with_powershell(user)
    if rc == 0:
        print(f"OK QuanterServer @ BootTrigger+RestartOnFailure → "
              f"start_server.bat (user={user})")
        return
    # 回退：schtasks /SC ONSTART（不带 RestartOnFailure），打印手动 XML 提示
    rc = _schtasks(["/Create", "/SC", "ONSTART", "/TN", "QuanterServer",
                    "/TR", str(ROOT / "scripts" / "start_server.bat"),
                    "/RU", user, "/F"])
    print(f"{'OK' if rc == 0 else 'FAIL(需 /RP 密码?)'} QuanterServer @ ONSTART"
          f"（回退，无 RestartOnFailure）→ start_server.bat (user={user})")
    if rc != 0:
        # schtasks ONSTART 在无密码时 /Create 会失败（session 0 需用户凭证）。提示运维
        # 手动带 /RP 跑一次（密码不进代码，避免硬编码 + git 泄露）。
        print("[!] schtasks ONSTART 需用户密码，手动跑：\n"
              "   schtasks /Create /SC ONSTART /TN QuanterServer "
              f"/TR \"{ROOT / 'scripts' / 'start_server.bat'}\" /RU {user} /RP <密码> /F")
    else:
        print("[!] 已回退注册（无 RestartOnFailure）。如需崩溃自愈，用任务计划程序补：\n"
              "    设置 → 如果任务失败，按以下频率重新启动: 1 分钟 / 尝试重启次数: 3")


def _register_with_powershell(user: str) -> int:
    """用 PowerShell ScheduledTasks 注册 BootTrigger + RestartOnFailure。

    Why S4U 登录类型：ONSTART + 本地 bat（无网络资源需求）可用 S4U——不存储密码，
    避免把 /RP 密码写进代码/命令历史；若 PowerShell 注册失败（rc≠0）由 register_server
    回退 schtasks 并提示人工。
    """
    script = (
        "$action = New-ScheduledTaskAction -Execute "
        f"'{ROOT / 'scripts' / 'start_server.bat'}';"
        "$trigger = New-ScheduledTaskTrigger -AtStartup;"
        "$settings = New-ScheduledTaskSettingsSet -RestartCount 3 "
        "-RestartInterval (New-TimeSpan -Minutes 1);"
        f"$principal = New-ScheduledTaskPrincipal -UserId '{user}' -LogonType S4U;"
        "Register-ScheduledTask -TaskName 'QuanterServer' -Action $action "
        "-Trigger $trigger -Settings $settings -Principal $principal -Force"
    )
    return subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        capture_output=True, text=True, timeout=30).returncode


def register_guard() -> None:
    """B2-4：注册 QuanterMiniQmtGuard（每 5 分钟一轮，独立于引擎生命周期）。

    物理意图（spec §4.3）：miniQMT 客户端保活不能寄生在引擎里（引擎崩了 guard 还在）；
    用 schtasks /SC MINUTE /MO 5 独立调度，与引擎解耦。G3 裁定。
    """
    tr = (f'"{ROOT / ".venv310" / "Scripts" / "python.exe"}" '
          f'"{ROOT / "ops" / "miniqmt_guard.py"}" --once')
    rc = _schtasks(["/Create", "/SC", "MINUTE", "/MO", "5",
                    "/TN", "QuanterMiniQmtGuard", "/TR", tr, "/F"])
    print(f"{'OK' if rc == 0 else 'FAIL(权限/语法)'} QuanterMiniQmtGuard @ 每5分钟 → "
          f"miniqmt_guard.py --once")
    if rc != 0:
        print("[!] 手动注册：\n"
              f"   schtasks /Create /SC MINUTE /MO 5 /TN QuanterMiniQmtGuard "
              f"/TR \"{tr}\" /F")


def register_audit() -> None:
    """CR-7：注册 QuanterAudit（每日 16:05 跑 ops/run_audit.bat → scripts/audit_ssot.py）。

    物理意图（tech-debt CR-7）：audit_ssot 的 7 项 SSOT 巡检（fill↔position 对账/
    引擎单例/护栏回归等）此前**无任何调度入口**——巡检层 fail-open，漏采/敞口偏差/
    护栏回归无人盯。挂 DAILY schtasks 后每天自动核账一次。

    Why 16:05：post_close 15:30 收盘流程走完、当日成交/持仓已落库，此时核账对的是
    完整当日数据；又避开 17:00 起的数据管道/brief 时序（见 discovery/schtasks.py
    的时序避让说明），错峰 5 分钟。

    Why 退出码可消费：audit_ssot errs→exit 1 / 全绿 exit 0，任务计划程序
    「上次运行结果」直接反映巡检红绿，运维一眼可见（不需要翻 stdout 日志）。

    ⚠️ 红线：QuanterAudit 是**活跃**任务，绝不加入 RETIRED_TASKS/LEGACY_TASKS
    清退清单（register()/unregister() 兜底迭代这两份名单，误列入会删成静默裸奔）。
    """
    tr = f'"{ROOT / "ops" / "run_audit.bat"}"'
    rc = _schtasks(["/Create", "/SC", "DAILY", "/MO", "1", "/ST", "16:05",
                    "/TN", "QuanterAudit", "/TR", tr, "/F"])
    print(f"{'OK' if rc == 0 else 'FAIL(权限/语法)'} QuanterAudit @ 每日16:05 → "
          f"ops/run_audit.bat（audit_ssot 巡检，errs→exit1）")
    if rc != 0:
        print("[!] 手动注册：\n"
              f"   schtasks /Create /SC DAILY /MO 1 /ST 16:05 /TN QuanterAudit "
              f"/TR \"{tr}\" /F")


def unregister_discovery() -> None:
    """C-7 V4：退 discovery ``QuanterDiscoveryDaemon`` schtasks（收编 lifespan 后防双触发）。

    物理意图（spec §3.4 · 风险 R5）：discovery 从 schtasks DAILY 02:00 收编到 uvicorn
    lifespan 的 engine.sched cron 02:00（C-7 V2）。旧 schtasks ``QuanterDiscoveryDaemon``
    若不退，会与 lifespan cron 同一晚双触发——daemon 跑两遍（虽 run_daemon_cycle 早退去重，
    但双进程同时读 discovery_trials.db 有锁竞争风险，且浪费 4h budget × 2）。

    Why 本函数独立（不并入 ``register()`` 兜底）：discovery 退订是 C-7 收编的独立语义，
    需单独入口供 V5 smoke 清单显式调用（语义清晰、可单独重跑）；``register()`` 仍只清退
    C-2 的 DataPipeline/Brief + 历史 6 零散任务（职责不混）。

    幂等：/Delete /F 对不存在任务返非零但不抛，本函数对 rc 不敏感（已删环境再调无副作用）。
    """
    rc = _schtasks(["/Delete", "/TN", "QuanterDiscoveryDaemon", "/F"])
    print(f"{'deleted' if rc == 0 else 'skip(not exists)'} QuanterDiscoveryDaemon")


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
        description="观测层 schtasks 管理（C-7：加 register-server ONSTART + unregister-discovery）"
    )
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--list", action="store_true")
    g.add_argument("--register", action="store_true",
                   help="清退历史零散 + 退役 schtasks（不再创建；Task 9 收口）")
    g.add_argument("--register-server", action="store_true",
                   help="C-7：注册 QuanterServer ONSTART（开机 session 0 后台起 python -m trading）")
    g.add_argument("--register-guard", action="store_true",
                   help="B2-4：注册 QuanterMiniQmtGuard（每 5 分钟客户端看门狗）")
    g.add_argument("--register-audit", action="store_true",
                   help="CR-7：注册 QuanterAudit（每日 16:05 跑 audit_ssot 巡检，errs→exit1）")
    g.add_argument("--unregister", action="store_true")
    g.add_argument("--unregister-pipeline-brief", action="store_true",
                   help="清退已收编进 uvicorn 的 DataPipeline/Brief（幂等，Task 9）")
    g.add_argument("--unregister-discovery", action="store_true",
                   help="C-7：退 QuanterDiscoveryDaemon（discovery 收编 lifespan cron 02:00 防双触发）")
    g.add_argument("--rerun", metavar="TASK", help="data | brief")
    p.add_argument("--user", default=None,
                   help="schtasks /RU 运行账户（--register-server 用；缺省 %USERNAME%）")
    args = p.parse_args(argv)
    if args.register:
        register()
    elif args.register_server:
        register_server(user=args.user)
    elif args.register_guard:
        register_guard()
    elif args.register_audit:
        register_audit()
    elif args.unregister:
        unregister()
    elif args.unregister_pipeline_brief:
        unregister_pipeline_brief()
    elif args.unregister_discovery:
        unregister_discovery()
    elif args.list:
        list_tasks()
    elif args.rerun:
        rerun(args.rerun)
    return 0


if __name__ == "__main__":
    sys.exit(main())
