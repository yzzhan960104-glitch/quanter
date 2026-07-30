# -*- coding: utf-8 -*-
"""W2：shadow_gate 改返 bool（C-2 scheduling-orchestration Task 5）。

物理意图：``_shadow_gate`` 历史 ``sys.exit(2)`` 在独立进程模式可接受，但 engine 合并进
uvicorn server 进程后，``sys.exit`` 会杀掉整个 API server。本测试覆盖 W2 重构
（``_shadow_gate`` → ``check_shadow_gate() -> bool``）：
  - mode=dry_run → 返 True（不拦）
  - mode=live + 影子不足 → 返 False（不再 sys.exit）
  - mode=live + 影子充足 → 返 True

CRITICAL 钉钉告警行为不变（build_default_manager + fire_and_forget 仍走原通道），
仅把进程级 ``sys.exit`` 收敛到函数级布尔返回——独立 ``__main__`` 模式可继续
``if not check_shadow_gate(): sys.exit(2)``，uvicorn lifespan 则由调用方决定处置。
"""
import os
from trading.__main__ import check_shadow_gate


def test_dry_run_passes(monkeypatch):
    monkeypatch.setenv("AUTO_TRADE_MODE", "dry_run")
    assert check_shadow_gate() is True


def test_live_shadow_insufficient_returns_false(monkeypatch):
    monkeypatch.setenv("AUTO_TRADE_MODE", "live")
    monkeypatch.setenv("TRADE_SHADOW_MIN_DAYS", "5")
    from datetime import datetime, timedelta
    recent = (datetime.now() - timedelta(days=1)).isoformat()  # 只观测1天
    class FakeExp:
        activated_at = recent
    monkeypatch.setattr("trading.__main__.resolve_active", lambda: [FakeExp()])
    assert check_shadow_gate() is False  # 不再 sys.exit，返 False


def test_live_shadow_sufficient_passes(monkeypatch):
    monkeypatch.setenv("AUTO_TRADE_MODE", "live")
    monkeypatch.setenv("TRADE_SHADOW_MIN_DAYS", "5")
    from datetime import datetime, timedelta
    old = (datetime.now() - timedelta(days=10)).isoformat()
    class FakeExp:
        activated_at = old
    monkeypatch.setattr("trading.__main__.resolve_active", lambda: [FakeExp()])
    assert check_shadow_gate() is True
