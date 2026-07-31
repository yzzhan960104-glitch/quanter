# -*- coding: utf-8 -*-
"""T6 ≥5 天影子期硬闸测试（Plan 4 L5 / spec §5.3 真·闸）。

物理意图：spec §5.3 把 ``trading/__main__.py`` 旧 WARNING 误称"≥5天硬闸"，实为提醒
（切 LIVE 只需改 env，无任何拦单逻辑）。本测试覆盖把 WARNING 升级为 fail-closed 真
闸后的 6 种边界。

W2 改造后（C-2 scheduling-orchestration Task 5）：``_shadow_gate`` → ``check_shadow_gate``
返 bool（``False`` 而非 ``sys.exit(2)``）。CRITICAL 钉钉告警行为不变。本测试已同步
切到 bool 契约；主路径（dry_run/insufficient/sufficient）的 bool 行为由 test_shadow_gate.py
覆盖，本文件保留 D3/D8/D9 风控红线的边缘用例（activated_at 缺失 / 异常 fail-closed /
空列表放行）。

  1. mode=live + 影子不足（activated_at < 5天）         → 返 False（D3 fail-closed）
  2. mode=live + 所有影子充足（≥5天）                    → 放行返 True
  3. mode=live + activated_at=None（缺失）               → 保守拒绝返 False（D9 宁可误杀）
  4. mode=live + resolve_active 抛异常                    → fail-closed 返 False（不降级放行）
  5. mode=dry_run                                          → 跳过（不查 resolve_active）返 True
  6. mode=live + resolve_active 返空列表                   → 放行返 True（D8 合法清场）

Mock 策略：monkeypatch trading.__main__.resolve_active 为 fake（不真连 SQLite），
setenv AUTO_TRADE_MODE / TRADE_SHADOW_MIN_DAYS 控制闸门条件。
"""
import sys
import types
from datetime import datetime, timedelta


def _set_active(monkeypatch, experiments):
    """mock resolve_active 返回预设实验列表（每项带 activated_at）。"""
    import types
    def _fake(): return experiments
    mod = sys.modules["trading.__main__"]
    monkeypatch.setattr(mod, "resolve_active", _fake, raising=False)


def _set_mode(monkeypatch, mode):
    monkeypatch.setenv("AUTO_TRADE_MODE", mode)
    monkeypatch.setenv("TRADE_SHADOW_MIN_DAYS", "5")


def test_live_blocked_when_shadow_insufficient(monkeypatch, tmp_path):
    """mode=live + activated_at < 5天 → 返 False（fail-closed，不再 sys.exit）。"""
    import trading.__main__ as m
    _set_mode(monkeypatch, "live")
    recent = datetime.now().isoformat(timespec="seconds")
    _set_active(monkeypatch, [types.SimpleNamespace(activated_at=recent, experiment_id="e1")])
    assert m.check_shadow_gate() is False


def test_live_allowed_when_shadow_sufficient(monkeypatch):
    """mode=live + 所有 activated_at ≥ 5天 → 放行（返 True）。"""
    import trading.__main__ as m
    _set_mode(monkeypatch, "live")
    old = (datetime.now() - timedelta(days=10)).isoformat(timespec="seconds")
    _set_active(monkeypatch, [types.SimpleNamespace(activated_at=old, experiment_id="e1")])
    assert m.check_shadow_gate() is True


def test_live_blocked_when_activated_at_missing(monkeypatch):
    """activated_at=None → 保守归入 fresh 拒绝返 False（D9，宁可误杀）。"""
    import trading.__main__ as m
    _set_mode(monkeypatch, "live")
    _set_active(monkeypatch, [types.SimpleNamespace(activated_at=None, experiment_id="e1")])
    assert m.check_shadow_gate() is False


def test_resolve_active_failure_blocks_live(monkeypatch):
    """resolve_active 抛异常 → fail-closed 返 False（不降级放行 LIVE）。"""
    import trading.__main__ as m
    _set_mode(monkeypatch, "live")
    def _boom(): raise RuntimeError("db locked")
    monkeypatch.setattr(m, "resolve_active", _boom, raising=False)
    assert m.check_shadow_gate() is False


def test_dry_run_skips_gate(monkeypatch):
    """mode=dry_run → 不查不拦（返 True，硬闸仅 LIVE 触发）。"""
    import trading.__main__ as m
    _set_mode(monkeypatch, "dry_run")
    called = {"n": 0}
    def _fake():
        called["n"] += 1; return []
    monkeypatch.setattr(m, "resolve_active", _fake, raising=False)
    assert m.check_shadow_gate() is True
    assert called["n"] == 0   # dry_run 不查 resolve_active


def test_live_allowed_when_no_active(monkeypatch):
    """mode=live + resolve_active 返空列表 → 放行（D8，合法清场非查询失败）。"""
    import trading.__main__ as m
    _set_mode(monkeypatch, "live")
    _set_active(monkeypatch, [])
    assert m.check_shadow_gate() is True
