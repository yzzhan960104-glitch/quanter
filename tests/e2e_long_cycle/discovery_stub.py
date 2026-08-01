# -*- coding: utf-8 -*-
"""组件6 discovery 触发层：cron 注册真 + daemon mock + 补跑判定（spec §3.2 V2 + §3.3 V3）。

物理意图：discovery 从 schtasks 收编 lifespan（C-7 V2），E2E 验证触发机制（cron 02:00 注册 +
offline 补跑判定），daemon 执行体 mock（22 次 × 4h 不可行；discovery e2e 已有 plan3/plan4）。
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from unittest.mock import patch


def missed_last_run() -> bool:
    """复用 presentation.server.main._discovery_missed_last_run（C-7 V3）。

    E2E 侧 re-export 便于测试 import。读 search_run 最新 started_at vs 昨日 02:00。
    """
    from presentation.server.main import _discovery_missed_last_run
    return _discovery_missed_last_run()


class _DiscoveryStub:
    """discovery触发 mock：attach 期间 _run_discovery_subprocess 被 mock。"""

    # 静态转发到模块级 missed_last_run：测试通过 discovery_stub.missed_last_run() 调用
    # （实例属性访问），模块级函数无法被实例直接调用，故在此挂静态方法转发。保持
    # 模块级函数（brief 显式定义）+ 实例访问两入口同源（DRY，单源 _discovery_missed_last_run）。
    missed_last_run = staticmethod(missed_last_run)

    @contextmanager
    def attach(self, eng, run_daemon_mock=None):
        """patch presentation.server.main._run_discovery_subprocess（daemon 不真跑）+
        注册 engine.sched cron 02:00（C-7 V2 范式）。

        eng: TradingEngine 实例（eng.sched.add_job 验证 cron 注册）。
        run_daemon_mock: 自定义 mock（默认 MagicMock）。
        """
        from apscheduler.triggers.cron import CronTrigger
        run_mock = run_daemon_mock or __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
        # 注册 cron 02:00（与 C-7 V2 同范式）
        try:
            eng.sched.add_job(
                run_mock, CronTrigger(hour=2, minute=0),
                id="discovery_daemon", replace_existing=True)
        except Exception:
            pass  # eng.sched 未启动也允许（仅验 add_job 调用）
        with patch("presentation.server.main._run_discovery_subprocess", run_mock):
            yield run_mock


discovery_stub = _DiscoveryStub()
