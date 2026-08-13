# -*- coding: utf-8 -*-
"""熔断基线缺失 fail-closed 单测（DG-G3 · 2026-08-13）。

物理定位（spec G3 · audit-remediation-design §G3）：
    ``trading/compute/breaker.py:check_daily_loss_limit`` 在基线权益缺失
    （start_equity<=0 或 None）时，**原 fail-open 语义 ``return False``** 会让
    「account_daily 漏采 + T-1 close 也缺」的极端情形静默放行（日内 -3% 熔断失效）。
    DG-G3 裁决：基线链全部缺失 → 不再放行，改为**触发保护**——
        - dry_run（模拟盘）：返 True（C-1 当日停手）+ CRITICAL 告警；
        - live（实盘）：raise _CriticalHalt（engine._critical_guard 捕获 → _halt L1 停调度）。

    不选「仅告警不动作」（那是 P0-2 治之前的纯静默，G3 收 fail-open 语义尾巴）。

行为等价红线（spec G3）：
    有基线路径（start_equity>0）判定逻辑**零变更**——本测试只覆盖基线缺失的新分支，
    回归由既有 ``test_circuit_breaker.py`` 守护（已同步更新为 fail-closed 语义）。

Why monkeypatch ``trading.compute.breaker._mode`` / ``_alert_critical``：
    breaker 在 fail-closed 分支内调 ``_mode()`` / ``_alert_critical()``（DG-G3 副用收口）。
    单测不发真钉钉、用 monkeypatch 拦截 + 断言调用次数。 ``_mode`` 经模块全局名解析，
    patch 模块属性即命中（与 pre_open/post_close 既有的 ``monkeypatch critical._mode`` 同范式，
    但 breaker 模块是副用入口，patch ``trading.compute.breaker._mode`` 才命中本地引用）。
"""
from __future__ import annotations

import pytest

# breaker 真身（functional core · DG-G3 后副用收口，但 import 路径不变）
from trading.compute.breaker import check_daily_loss_limit
# _CriticalHalt 按类身份 catch（L1 致命异常 · engine._critical_guard 捕获停调度）
from trading.critical import _CriticalHalt


# ============================================================================
# 实盘模式：基线缺失 → raise _CriticalHalt（L1 停调度 · DG-G3 裁决）
# ============================================================================

def test_live_baseline_zero_raises_halt(monkeypatch):
    """live + start_equity=0 → raise _CriticalHalt（实盘基线链全失，拒继续下注）。

    物理意图（DG-G3）：live 模式基线缺失 = 日内 -3% 熔断失效 = 真金敞口失控红线，
    必须停调度等人工介入（与 pre_open DB 写失败同 _CriticalHalt 语义）。
    """
    monkeypatch.setattr("trading.compute.breaker._mode", lambda: "live")
    # 副用 _alert_critical 不真发钉钉（单测不发真告警，仅断言被调）
    monkeypatch.setattr("trading.compute.breaker._alert_critical", lambda msg: None)
    with pytest.raises(_CriticalHalt):
        check_daily_loss_limit(0, 965_000, limit=-0.03)


def test_live_baseline_none_raises_halt(monkeypatch):
    """live + start_equity=None → raise _CriticalHalt（None 比 0 更明确表「未抓到基线」）。

    物理意图：post_close T-1 兜底也取不到时，会把 None 透传给 breaker（不再 float() 强转
    触发 TypeError），breaker 必须 fail-closed 处理 None（与 0 同语义）。
    """
    monkeypatch.setattr("trading.compute.breaker._mode", lambda: "live")
    monkeypatch.setattr("trading.compute.breaker._alert_critical", lambda msg: None)
    with pytest.raises(_CriticalHalt):
        check_daily_loss_limit(None, 965_000, limit=-0.03)  # type: ignore[arg-type]


def test_live_baseline_negative_raises_halt(monkeypatch):
    """live + start_equity<0（脏数据/对账反算负值）→ raise _CriticalHalt（同 0/None fail-closed）。"""
    monkeypatch.setattr("trading.compute.breaker._mode", lambda: "live")
    monkeypatch.setattr("trading.compute.breaker._alert_critical", lambda msg: None)
    with pytest.raises(_CriticalHalt):
        check_daily_loss_limit(-100, 965_000, limit=-0.03)


# ============================================================================
# 模拟盘：基线缺失 → 返 True 停手 + CRITICAL 告警（DG-G3 裁决：不抛 halt 进程）
# ============================================================================

def test_dry_run_baseline_zero_stops_and_alerts(monkeypatch):
    """dry_run + start_equity=0 → 返 True（C-1 当日停手）+ 告警被调（不抛 halt）。

    物理意图（DG-G3 裁决「模拟盘=停手+CRITICAL 告警，不抛 halt 进程」）：
        dry_run 是影子观测/回放态，停手即「当日不再下注」（C-1 触发语义），但不应
        抛 _CriticalHalt 中断整个引擎进程（与 live 区分：live 才停调度）。
    """
    monkeypatch.setattr("trading.compute.breaker._mode", lambda: "dry_run")
    alert_calls: list[str] = []
    monkeypatch.setattr(
        "trading.compute.breaker._alert_critical", lambda msg: alert_calls.append(msg))
    # start_equity=0：返 True（C-1 熔断停手语义）
    assert check_daily_loss_limit(0, 965_000, limit=-0.03) is True
    assert len(alert_calls) == 1, "基线缺失应推一次 CRITICAL 告警"
    assert "基线缺失" in alert_calls[0] or "fail-closed" in alert_calls[0]


def test_dry_run_baseline_none_stops_and_alerts(monkeypatch):
    """dry_run + start_equity=None → 返 True（None 与 0 fail-closed 语义等价）。"""
    monkeypatch.setattr("trading.compute.breaker._mode", lambda: "dry_run")
    monkeypatch.setattr("trading.compute.breaker._alert_critical", lambda msg: None)
    assert check_daily_loss_limit(None, 965_000, limit=-0.03) is True  # type: ignore[arg-type]


# ============================================================================
# 行为等价红线：有基线路径判定逻辑零变更（spec G3「有基线路径行为不变」）
# ============================================================================

def test_valid_baseline_judgment_unchanged(monkeypatch):
    """有基线路径判定逻辑零变更（spec G3 行为等价红线）。

    覆盖既有 test_circuit_breaker.py 的核心契约：-3.5% 触发 / -2% 不触发 / 边界 -3.0% 触发。
    本测试守护 fail-closed 改动不污染正常判定路径。
    """
    # 即便 _mode=live，有基线时也不应 raise / 不应告警
    monkeypatch.setattr("trading.compute.breaker._mode", lambda: "live")
    alert_calls: list[str] = []
    monkeypatch.setattr(
        "trading.compute.breaker._alert_critical", lambda msg: alert_calls.append(msg))
    # -3.5% 触发
    assert check_daily_loss_limit(1_000_000, 965_000, limit=-0.03) is True
    # -2% 不触发
    assert check_daily_loss_limit(1_000_000, 980_000, limit=-0.03) is False
    # 边界 -3.0% 触发（<= 风控宁可多触发）
    assert check_daily_loss_limit(1_000_000, 970_000, limit=-0.03) is True
    # 有基线路径不触达告警
    assert alert_calls == []
