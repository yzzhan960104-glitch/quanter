# -*- coding: utf-8 -*-
"""熔断单测（Task 6）。

覆盖二期自动交易引擎 post_close（盘后）触发点的两个工具函数：
1. ``check_daily_loss_limit``：日亏上限判定（防穿仓）；
2. ``cancel_all_open_orders``：撤所有未终态单（补全 emergency_halt 漏洞）。

Why 类型契约要反映真实网关：
- 真实 ``QmtExecutionGateway`` 的 ``_orders`` 流水里
  ``rec["state"]`` 全部是 ``OrderState`` 枚举（见 ``qmt_gateway._map_qmt_status``
  与 ``cleanup_orders`` 的终态集）；本测试的 FakeGW 必须用枚举构造，否则
  会落入「字符串自洽的假世界」，无法暴露终态集误用字符串导致的误撤单 bug。
"""
import asyncio
import os

# Layer2 阶段6：circuit_breaker 垫片已删，两个被测函数的真身分别在：
# - check_daily_loss_limit → trading.compute.breaker（纯判定 functional core）
# - cancel_all_open_orders → trading.io.breaker（撤单副作用壳）
# 本测试用 cb 别名聚合两者，保持测试体内 cb.check_daily_loss_limit /
# cb.cancel_all_open_orders 调用零改动（语义仍是「熔断」工具函数对）。
from trading.compute.breaker import check_daily_loss_limit as _check_loss
# W6-A：io.breaker（cancel_all 撤单壳）随 QMT live 面删除——撤单属柜台副作用，
# 掘金腿的对应物是 pilot_body._cancel_and_sync（tests/emquant 侧守卫）。
from trading.types.order_state import OrderState  # Layer2 follow-up #4c：改指 types 真身


class _CBShim:
    """W6-A 后只剩 compute.breaker 真身（io 侧撤单壳已删，见文件头注）。"""
    check_daily_loss_limit = staticmethod(_check_loss)


cb = _CBShim


# ----------------------------------------------------------------- 日亏熔断


def test_daily_loss_limit_triggers():
    """日亏触及 -3% 触发熔断。"""
    # -3.5% 已穿透 -3% 上限 -> 触发
    assert cb.check_daily_loss_limit(1_000_000, 965_000, limit=-0.03) is True
    # -2% 未穿透 -3% 上限 -> 不触发
    assert cb.check_daily_loss_limit(1_000_000, 980_000, limit=-0.03) is False


def test_daily_loss_limit_boundary_equal():
    """恰好等于 limit（-3.0%）应触发：采用 <= 风控宁可多触发也不容忍边界裸奔。"""
    assert cb.check_daily_loss_limit(1_000_000, 970_000, limit=-0.03) is True


def test_daily_loss_limit_invalid_start_equity(monkeypatch):
    """基线缺失 fail-closed（DG-G3 · 2026-08-13）：start_equity<=0/None 不再 return False 放行。

    物理意图（DG-G3 裁决）：原 ``return False`` fail-open 让 account_daily 漏采时
    日内 -3% 熔断静默失效（实盘敞口失控红线）。改 fail-closed：
        - dry_run（默认）：返 True（C-1 当日停手）+ CRITICAL 告警；
        - live：raise _CriticalHalt（详见 test_breaker_fail_closed.py）。
    本测覆盖 dry_run 默认路径（live halt 路径在新测覆盖）。breaker 在 fail-closed
    分支内调 _alert_critical/_mode（monkeypatch 拦截避免真发钉钉 + 固定 dry_run）。
    """
    # dry_run 模式 + 拦截告警副用（避免真发钉钉 + 防 _mode 读 env 漂移）
    monkeypatch.setattr("trading.compute.breaker._mode", lambda: "dry_run")
    monkeypatch.setattr("trading.compute.breaker._alert_critical", lambda msg: None)
    # start_equity<=0 / None / 负值 均触发 fail-closed（返 True 停手）
    assert cb.check_daily_loss_limit(0, 0, limit=-0.03) is True
    assert cb.check_daily_loss_limit(-100, -200, limit=-0.03) is True
    assert cb.check_daily_loss_limit(None, 965_000, limit=-0.03) is True  # type: ignore[arg-type]


def test_daily_loss_limit_env_default(monkeypatch):
    """limit 缺省读 env ``CIRCUIT_DAILY_LOSS_LIMIT``，默认 -0.03。"""
    monkeypatch.setenv("CIRCUIT_DAILY_LOSS_LIMIT", "-0.05")
    # 默认值 -5%：亏 4% 不触发
    assert cb.check_daily_loss_limit(1_000_000, 960_000) is False
    # 默认值 -5%：亏 6% 触发
    assert cb.check_daily_loss_limit(1_000_000, 940_000) is True


def test_daily_loss_limit_env_unset_default(monkeypatch):
    """env 未设置时回退 -0.03 默认值。"""
    monkeypatch.delenv("CIRCUIT_DAILY_LOSS_LIMIT", raising=False)
    # -3% 边界应触发
    assert cb.check_daily_loss_limit(1_000_000, 970_000) is True


# --------------------------------------------------------------- 撤未终态单
