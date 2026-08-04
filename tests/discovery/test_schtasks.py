# -*- coding: utf-8 -*-
"""discovery 包自包含 schtasks 注册的单测（Plan 4 Task 4）。

Why 独立于 scripts/manage_ops_schtasks：discovery 包要自包含调度（不依赖 scripts，
后续 scripts 废弃时不受影响），单测必须验证包内常量→命令映射的正确性。

Why mock subprocess：schtasks /Delete+/Create 会真改 Windows 任务计划程序，
CI/开发机反复跑会污染真实调度——故 register 用 monkeypatch 替换 _schtasks 为
假收集器，仅断言命令调用序。

⚠️ C-9 A3 / T8 语义翻转：QuanterDiscoveryDaemon 已收编进 uvicorn lifespan
（engine.sched cron 02:00 + 启动补跑，见 presentation/server/main.py C-7 V2）。
``register()`` 从幂等"先删后建"改为**拒绝重建**（防双跑：lifespan cron + 残留
schtasks 同一晚双触发 daemon，浪费 4h budget × 2 且有 db 锁竞争）。``main --register``
必须返非零。原 test_register_calls_schtasks_delete_then_create 的 /Delete+/Create
断言已翻转为本测试文件里的"拒绝"语义。
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


def test_register_refused_no_schtasks_create(monkeypatch):
    """register 拒绝重建（C-9 A3 语义翻转）：绝不 /Create /Delete 任何 schtasks。

    物理意图（C-7 V2 + T8）：QuanterDiscoveryDaemon 已收编 uvicorn lifespan 的
    engine.sched cron 02:00。若 register 仍 /Create，旧 schtasks 残留会与 lifespan
    cron 同一晚双触发 daemon（双进程读 discovery_trials.db 锁竞争 + 4h budget × 2）。
    register() 改为只打印退役提示，不发任何 schtasks 子进程命令。

    既有测试翻转（Plan Step 4 "若既有 register 用例断言创建，按新语义更新为断言拒绝"）：
    原 test_register_calls_schtasks_delete_then_create 断言 /Delete+/Create，已替换为
    本用例断言「零 schtasks 调用」——不删除测试，仅翻转为新语义。
    """
    import discovery.schtasks as sch
    calls = []

    def _fake(args):
        calls.append(args)
        return 0

    monkeypatch.setattr(sch, "_schtasks", _fake)
    sch.register()  # 拒绝：不应触发任何 schtasks 子进程
    # 拒绝语义红线：不发 /Create、不发 /Delete（register 仅打印退役提示）
    assert not any("/Create" in a for a in calls), f"退役任务禁止 /Create：{calls}"
    assert not any("/Delete" in a for a in calls), f"退役 register 不再 /Delete：{calls}"


def test_register_refused_after_retirement(monkeypatch):
    """QuanterDiscoveryDaemon 已收编 lifespan（C-7 V2），--register 必须拒绝，防双跑。

    brief Step 1 用例：main(["--register"]) 返回非 0，且 _schtasks 不被 /Create 调用。
    """
    from discovery.schtasks import main
    calls: list[list[str]] = []
    monkeypatch.setattr("discovery.schtasks._schtasks", lambda a: calls.append(a) or 0)
    rc = main(["--register"])
    assert rc != 0
    assert all("/Create" not in c for c in calls), f"不得重建退役任务：{calls}"
