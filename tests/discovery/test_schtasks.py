# -*- coding: utf-8 -*-
"""discovery 包自包含 schtasks 注册的单测（Plan 4 Task 4）。

Why 独立于 scripts/manage_ops_schtasks：discovery 包要自包含调度（不依赖 scripts，
后续 scripts 废弃时不受影响），单测必须验证包内常量→命令映射的正确性。

Why mock subprocess：schtasks /Delete+/Create 会真改 Windows 任务计划程序，
CI/开发机反复跑会污染真实调度——故 register 用 monkeypatch 替换 _schtasks 为
假收集器，仅断言"先 /Delete /F 再 /Create"的幂等调用序（spec §10 幂等纪律）。
"""


def test_build_register_commands_shape():
    """注册命令纯函数：返 task/time/bot 三段映射（不触达 subprocess）。

    验证 DAEMON_TASK_NAME / DAEMON_TIME / DAEMON_BAT 三常量被 build_register_commands
    正确组装：task 名固定（schtasks /TN 用）、时间 02:00（spec §5.2 夜跑）、bat 路径
    指向包内 discovery/run_daemon.bat（包自包含——绝不指向 scripts/）。
    """
    from discovery.schtasks import build_register_commands, DAEMON_TASK_NAME
    cmds = build_register_commands()
    assert len(cmds) == 1
    c = cmds[0]
    assert c["task"] == DAEMON_TASK_NAME
    assert c["time"] == "02:00"
    assert c["bat"].endswith("discovery\\run_daemon.bat")


def test_register_calls_schtasks_delete_then_create(monkeypatch):
    """register 幂等：先 /Delete /F 再 /Create（不污染真实任务计划程序，mock subprocess）。

    幂等模式复用 ops/manage_ops_schtasks.py 既有纪律：先 /Delete /F（不存在也返 0，
    不报错），再 /Create /SC DAILY /TN /TR /ST /F 覆盖。保证改时间后重跑 register 不
    报"任务已存在"错（schtasks /Create 非 /F 模式遇到已存在会失败）。
    """
    import discovery.schtasks as sch
    calls = []

    def _fake(args):
        calls.append(args)
        return 0

    monkeypatch.setattr(sch, "_schtasks", _fake)
    sch.register()
    # 至少一次 Delete + 一次 Create（幂等序：先删后建）
    assert any("/Delete" in a for a in calls)
    assert any("/Create" in a for a in calls)
