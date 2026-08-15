# -*- coding: utf-8 -*-
"""C-7 V4：manage_ops_schtasks 加 --register-server / --unregister-discovery 单测。

物理意图（spec §3.4）：start_all.py 的「subprocess.Popen DETACHED uvicorn + 启动文件夹
ONLOGON」启动链收编为 schtasks ONSTART（QuanterServer → scripts/start_server.bat →
``python -m trading``），开机 session 0 后台起 server（不依赖用户登录/logoff，适配生产机
不 7x24）。discovery 从 schtasks DAILY 02:00 收编 lifespan APScheduler 后，旧
QuanterDiscoveryDaemon schtasks 必须退（防双触发——lifespan cron + 残留 schtasks 同时跑）。

测试范式：mock ``_schtasks`` 收集命令字（capture_output 子进程封装，单测不真起 schtasks）。
main flag 测试 mock register_server/unregister_discovery 断言派发。
"""
from __future__ import annotations

from unittest.mock import patch

from ops.manage_ops_schtasks import (
    main,
    register_guard,
    register_server,
    unregister_discovery,
)


# ============ register_server：QuanterServer ONSTART ============


class _FakeProc:
    def __init__(self, returncode: int = 0, stdout: str = ""):
        self.returncode = returncode
        self.stdout = stdout


def test_register_server_uses_boot_trigger_with_restart_on_failure():
    """A4: 主路径 = PowerShell Register-ScheduledTask（BootTrigger + RestartCount）。

    物理意图（spec §7.1-6 · 08-06 实测 LogonTrigger 无重启策略）：schtasks CLI 不支持
    RestartOnFailure，必须走 PowerShell ScheduledTasks；S4U 不存密码（本地 bat 无网络需求）。
    """
    calls: list[list[str]] = []
    with patch("ops.manage_ops_schtasks.subprocess.run",
               side_effect=lambda *a, **kw: calls.append(a[0]) or _FakeProc()):
        register_server(user="TestUser")
    ps_calls = [c for c in calls if "powershell" in c]
    assert len(ps_calls) == 1, f"期望恰好 1 条 PowerShell 注册，实际 {ps_calls}"
    script = " ".join(ps_calls[0])
    assert "Register-ScheduledTask" in script
    assert "AtStartup" in script                 # BootTrigger（ONSTART 语义）
    assert "RestartCount" in script and "RestartInterval" in script
    assert "start_server.bat" in script
    assert "QuanterServer" in script
    assert "TestUser" in script
    # 主路径成功时不走 schtasks /Create 回退
    assert not any("/Create" in c for c in calls)


def test_register_server_default_user_from_env(monkeypatch):
    """register_server() 无 user → PowerShell 命令取 os.environ USERNAME（缺省当前用户）。"""
    monkeypatch.setenv("USERNAME", "EnvUser")
    calls: list[list[str]] = []
    with patch("ops.manage_ops_schtasks.subprocess.run",
               side_effect=lambda *a, **kw: calls.append(a[0]) or _FakeProc()):
        register_server()
    ps_calls = [c for c in calls if "powershell" in c]
    assert len(ps_calls) == 1
    assert "EnvUser" in " ".join(ps_calls[0])


def test_register_server_falls_back_to_schtasks_when_powershell_fails():
    """A4: PowerShell 注册失败（rc≠0）→ 回退 schtasks /SC ONSTART 并保持入口不变。"""
    schtasks_cmds: list[list[str]] = []
    with patch("ops.manage_ops_schtasks.subprocess.run",
               side_effect=lambda *a, **kw: _FakeProc(returncode=1)):
        with patch("ops.manage_ops_schtasks._schtasks",
                   side_effect=lambda a: schtasks_cmds.append(a) or 0):
            register_server(user="TestUser")
    create_cmds = [c for c in schtasks_cmds if "/Create" in c]
    assert len(create_cmds) == 1
    cmd = " ".join(create_cmds[0])
    assert "/SC" in cmd and "ONSTART" in cmd
    assert "/TN" in cmd and "QuanterServer" in cmd
    assert "/TR" in cmd and "start_server.bat" in cmd
    assert "/RU" in cmd and "TestUser" in cmd


def test_register_guard_creates_minute_task():
    """B2-4: QuanterMiniQmtGuard = /SC MINUTE /MO 5 + venv python miniqmt_guard.py --once。"""
    cmds: list[list[str]] = []
    with patch("ops.manage_ops_schtasks._schtasks",
               side_effect=lambda a: cmds.append(a) or 0):
        register_guard()
    create_cmds = [c for c in cmds if "/Create" in c]
    assert len(create_cmds) == 1
    cmd = " ".join(create_cmds[0])
    assert "/SC" in cmd and "MINUTE" in cmd and "/MO" in cmd and "5" in cmd
    assert "QuanterMiniQmtGuard" in cmd
    assert "miniqmt_guard.py" in cmd and "--once" in cmd


# ============ unregister_discovery：退 QuanterDiscoveryDaemon ============


def test_unregister_discovery_deletes_task():
    """unregister_discovery：schtasks /Delete /TN QuanterDiscoveryDaemon /F（幂等）。

    物理意图（spec §3.4 · R5）：discovery 收编 lifespan cron 02:00 后，旧 schtasks
    QuanterDiscoveryDaemon 残留会与 lifespan 双触发（同一晚跑两遍 daemon）。/Delete /F
    幂等——不存在不报错（rc 非零但本函数不抛）。
    """
    cmds: list[list[str]] = []
    with patch("ops.manage_ops_schtasks._schtasks",
               side_effect=lambda a: cmds.append(a) or 0):
        unregister_discovery()
    delete_cmds = [c for c in cmds if "/Delete" in c]
    assert len(delete_cmds) == 1, f"期望恰好 1 条 /Delete，实际 {delete_cmds}"
    cmd = delete_cmds[0]
    assert "/TN" in cmd and "QuanterDiscoveryDaemon" in cmd
    assert "/F" in cmd  # 强制删（不停确认）


# ============ main：argparse 派发 ============


def test_main_register_server_flag():
    """main(--register-server --user X) → 派发 register_server(user=X)。"""
    with patch("ops.manage_ops_schtasks.register_server") as rs:
        rc = main(["--register-server", "--user", "TestUser"])
    assert rc == 0
    rs.assert_called_once()
    # user 参数透传
    assert rs.call_args.kwargs.get("user") == "TestUser" or "TestUser" in (rs.call_args.args or ())


def test_main_unregister_discovery_flag():
    """main(--unregister-discovery) → 派发 unregister_discovery。"""
    with patch("ops.manage_ops_schtasks.unregister_discovery") as ud:
        rc = main(["--unregister-discovery"])
    assert rc == 0
    ud.assert_called_once()


# ============ T8 / C-9 A3：RETIRED_TASKS 补 QuanterDailyBrief ============


def test_retired_tasks_contains_daily_brief():
    """QuanterDailyBrief 已从系统删除；若被重建，--unregister-pipeline-brief 必须能清。

    物理意图（C-8 + T8）：``QuanterDailyBrief`` 在 C-8 后已删系统 schtasks，但
    ``RETIRED_TASKS`` 之前只列 ``QuanterDataPipeline`` / ``QuanterBrief`` 两个，
    缺 ``QuanterDailyBrief`` —— 升级环境若残留该任务（或被误重建），现有
    --unregister-pipeline-brief / --unregister / --register 三处清退都查
    RETIRED_TASKS，漏列会导致该任务不被幂等清退、残留双触发。本测试锁定名单完整性。
    """
    from ops.manage_ops_schtasks import RETIRED_TASKS
    assert "QuanterDailyBrief" in RETIRED_TASKS


# ============ CR-7：QuanterAudit 每日巡检 schtasks ============
# Why 调度挂载：audit_ssot 7 项巡检此前无任何调度入口（巡检层 fail-open——漏采/
# 敞口偏差/护栏回归无人盯）。挂 DAILY 16:05（post_close 15:30 收盘后，当日数据
# 已落湖即可核账）由退出码驱动：errs→exit 1 供任务计划程序「上次运行结果」非零可见。


def test_register_audit_creates_daily_task():
    """CR-7: register_audit = /SC DAILY /MO 1 /ST 16:05 → ops/run_audit.bat（幂等 /F）。"""
    from ops.manage_ops_schtasks import register_audit
    cmds: list[list[str]] = []
    with patch("ops.manage_ops_schtasks._schtasks",
               side_effect=lambda a: cmds.append(a) or 0):
        register_audit()
    create_cmds = [c for c in cmds if "/Create" in c]
    assert len(create_cmds) == 1, f"期望恰好 1 条 /Create，实际 {create_cmds}"
    cmd = create_cmds[0]
    assert "/SC" in cmd and "DAILY" in cmd          # 每日一跑（非 MINUTE 轮询）
    assert "/MO" in cmd and "1" in cmd              # 间隔 1 天
    assert "/ST" in cmd and "16:05" in cmd          # post_close 15:30 之后
    assert "/TN" in cmd and "QuanterAudit" in cmd
    # /TR 必须指向 bat（bat 内固定 cd /d E:\quanter + PYTHONUTF8 + 日志重定向）
    tr = cmd[cmd.index("/TR") + 1]
    assert "run_audit.bat" in tr
    assert "/F" in cmd                              # 幂等：重复注册强制覆盖


def test_register_audit_not_in_cleanup_lists():
    """CR-7 红线：QuanterAudit 是活跃任务，绝不可进 RETIRED/LEGACY 清退清单——
    register()/unregister() 兜底清退迭代这两份名单，误列入会把巡检调度删成静默裸奔。"""
    from ops.manage_ops_schtasks import RETIRED_TASKS, LEGACY_TASKS
    assert "QuanterAudit" not in RETIRED_TASKS
    assert "QuanterAudit" not in LEGACY_TASKS


def test_main_register_audit_flag():
    """main(--register-audit) → 派发 register_audit()（独立 flag，不混入 --register 清退语义）。"""
    from ops.manage_ops_schtasks import register_audit
    with patch("ops.manage_ops_schtasks.register_audit") as ra:
        rc = main(["--register-audit"])
    assert rc == 0
    ra.assert_called_once()


# ============ Low ⑪（N5 · 2026-08-16）：_schtasks 子进程解码容错 ============


def test_schtasks_passes_utf8_replace_decode_kwargs():
    """_schtasks 显式 encoding=utf-8 / errors=replace（GBK 输出不炸 reader 线程）。

    物理意图：全链 PYTHONUTF8=1 下 text=True 缺省按 UTF-8 strict 解码，而中文
    Windows 的 schtasks 输出 GBK 字节——GBK 字节流过 UTF-8 strict 解码在
    communicate 的 reader 线程抛 UnicodeDecodeError，注册已成功却以炸栈收场
    （stdout 丢失）。本测试锁定两个解码 kwarg 必须显式在位（缺省回归即红）。
    """
    import ops.manage_ops_schtasks as mos

    captured: dict = {}

    class _Proc:
        returncode = 0

    with patch("ops.manage_ops_schtasks.subprocess.run",
               side_effect=lambda *a, **kw: captured.update(kw) or _Proc()):
        mos._schtasks(["/Query", "/TN", "AnyTask"])
    assert captured.get("encoding") == "utf-8", "必须显式 utf-8 解码（locale 漂移防线）"
    assert captured.get("errors") == "replace", "必须 replace 容错（解码永不抛是硬契约）"
