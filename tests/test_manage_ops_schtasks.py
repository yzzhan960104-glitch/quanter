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

from ops.manage_ops_schtasks import main, register_server, unregister_discovery


# ============ register_server：QuanterServer ONSTART ============


def test_register_server_creates_onstart_task():
    """register_server(user)：schtasks /Create /SC ONSTART /TN QuanterServer /TR start_server.bat /RU <user>。

    物理意图（spec §3.4）：ONSTART 开机 session 0 后台起 server，替代 ONLOGON 启动文件夹。
    /TR 指向 scripts/start_server.bat（绝对路径，本测试用子串 ``start_server.bat`` 匹配）。
    """
    cmds: list[list[str]] = []
    with patch("ops.manage_ops_schtasks._schtasks",
               side_effect=lambda a: cmds.append(a) or 0):
        register_server(user="TestUser")
    # 找到 /Create 命令（register_server 仅发一条 /Create）
    create_cmds = [c for c in cmds if "/Create" in c]
    assert len(create_cmds) == 1, f"期望恰好 1 条 /Create，实际 {create_cmds}"
    cmd = " ".join(create_cmds[0])  # list→str：元素绝对路径需子串匹配，join 后统一子串断言
    assert "/SC" in cmd and "ONSTART" in cmd   # 开机触发（非 ONLOGON/DAILY）
    assert "/TN" in cmd and "QuanterServer" in cmd
    assert "/TR" in cmd and "start_server.bat" in cmd   # 入口 bat（绝对路径子串）
    assert "/RU" in cmd and "TestUser" in cmd           # 运行账户
    assert "/F" in cmd                                  # 强制覆盖（幂等重注册）


def test_register_server_default_user_from_env(monkeypatch):
    """register_server() 无 user → /RU 取 os.environ USERNAME（缺省当前用户）。"""
    monkeypatch.setenv("USERNAME", "EnvUser")
    cmds: list[list[str]] = []
    with patch("ops.manage_ops_schtasks._schtasks",
               side_effect=lambda a: cmds.append(a) or 0):
        register_server()  # 不传 user
    cmd = " ".join([c for c in cmds if "/Create" in c][0])
    assert "/RU" in cmd and "EnvUser" in cmd


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
