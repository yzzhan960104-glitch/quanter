# -*- coding: utf-8 -*-
"""research digest 周期推送 cron 注册单测（2026-08-03 · 钉钉周期同步）。

物理意图：长周期 Agent 观察环的推送腿——lifespan 里注册每日盘后 cron，
触发 DETACHED 子进程 ``python -m research.digest --push``（零事件循环阻塞，
与 discovery cron 同范式），钉钉收到"实盘 vs 回测期望"研究摘要。
"""
import presentation.server.main as main


def test_run_research_digest_push_spawns_detached_subprocess(monkeypatch):
    """_run_research_digest_push 必须用 DETACHED 子进程跑 research.digest --push。"""
    calls = []

    class _FakePopen:
        def __init__(self, args, **kw):
            calls.append({"args": args, **kw})

    monkeypatch.setattr(main._subprocess, "Popen", _FakePopen)
    main._run_research_digest_push()
    assert len(calls) == 1
    cmd = calls[0]["args"]
    assert cmd[1:3] == ["-m", "research.digest"]
    assert "--push" in cmd and "--proposals" in cmd
    assert calls[0]["creationflags"] & main._DETACHED_PROCESS
