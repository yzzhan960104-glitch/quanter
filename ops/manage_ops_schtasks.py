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
    """C-7 V4：注册 QuanterServer schtasks ONSTART（开机 session 0 后台起 ``python -m trading``）。

    物理意图（spec §3.4）：替代 ``ops/start_all.py`` 的「subprocess.Popen DETACHED uvicorn
    + 启动文件夹 ONLOGON」启动链。ONSTART 开机即跑（session 0 后台，不依赖用户登录/RDP
    会话——logoff 不会杀进程），适配生产机不 7x24（会关机/断电，重启即自动恢复服务）。

    Why ONSTART 而非 ONLOGON：ONLOGON 仅在用户登录的会话里跑，断 RDP / logoff 则 server
    被 terminating（旧 start_all.bat 自安快捷式的痛点）；ONSTART 在 session 0 服务上下文
    跑，与交互登录解耦。

    /TR 指向 scripts/start_server.bat（cd /d F:\\quanter + venv python -m trading），
    由 schtasks 包裹成后台进程（无需 start_all.py 的 DETACHED 代码）。

    /RU：运行账户（user 参数；缺省 %USERNAME%）。/RP 密码由调用方交互输入——schtasks
    ONSTART 需用户密码（session 0 需凭证），密码不进代码（安全 + 不硬编码）。若本函数
    不带 /RP 导致 /Create 失败（rc≠0），打印手动命令提示由运维补密码重跑。

    幂等：/F 强制覆盖（重复注册不报错，更新即有任务）。
    """
    user = user or os.environ.get("USERNAME", "")
    rc = _schtasks(["/Create", "/SC", "ONSTART", "/TN", "QuanterServer",
                    "/TR", str(ROOT / "scripts" / "start_server.bat"),
                    "/RU", user, "/F"])
    print(f"{'OK' if rc == 0 else 'FAIL(需 /RP 密码?)'} QuanterServer @ ONSTART → "
          f"start_server.bat (user={user})")
    if rc != 0:
        # schtasks ONSTART 在无密码时 /Create 会失败（session 0 需用户凭证）。提示运维
        # 手动带 /RP 跑一次（密码不进代码，避免硬编码 + git 泄露）。
        print("⚠️ schtasks ONSTART 需用户密码，手动跑：\n"
              "   schtasks /Create /SC ONSTART /TN QuanterServer "
              f"/TR \"{ROOT / 'scripts' / 'start_server.bat'}\" /RU {user} /RP <密码> /F")


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
