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
from trading.io.breaker import cancel_all_open_orders as _cancel_all
from trading.order_state import OrderState


class _CBShim:
    """聚合 compute.breaker + io.breaker 两真身的熔断工具对（兼容旧 cb 别名）。"""
    check_daily_loss_limit = staticmethod(_check_loss)
    cancel_all_open_orders = staticmethod(_cancel_all)


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


def test_daily_loss_limit_invalid_start_equity():
    """start_equity <= 0 返回 False（防除零；初始权益为 0 视作「无持仓基线」不熔断）。"""
    assert cb.check_daily_loss_limit(0, 0, limit=-0.03) is False
    assert cb.check_daily_loss_limit(-100, -200, limit=-0.03) is False


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


class _FakeGW:
    """最小网关桩：仅暴露 ``_orders`` 与 async ``cancel_order``。

    state 必须用 ``OrderState`` 枚举构造，以对齐真实网关 _orders 的类型契约。
    """

    def __init__(self, orders):
        self._orders = orders
        self.cancelled = []

    async def cancel_order(self, oid):
        self.cancelled.append(oid)
        return None


def test_cancel_all_open_orders_skips_terminal_states():
    """只撤未终态单，FILLED/CANCELLED/REJECTED/FAILED/PARTIAL_CANCELLED 一律不撤。"""
    gw = _FakeGW({
        "1": {"state": OrderState.SUBMITTED},          # 未终态 -> 撤
        "2": {"state": OrderState.FILLED},             # 终态 -> 不撤
        "3": {"state": OrderState.CANCELLED},          # 终态 -> 不撤
        "4": {"state": OrderState.REJECTED},           # 终态 -> 不撤
        "5": {"state": OrderState.FAILED},             # 终态 -> 不撤
        "6": {"state": OrderState.PARTIAL_CANCELLED},  # 终态 -> 不撤
        "7": {"state": OrderState.PARTIAL_FILLED},     # 未终态 -> 撤
        "8": {"state": OrderState.PENDING},            # 未终态 -> 撤
    })
    n = asyncio.run(cb.cancel_all_open_orders(gw))
    # 仅 1/7/8 撤单，顺序按 _orders 迭代序（Python 3.7+ dict 保序）
    assert n == 3
    assert gw.cancelled == ["1", "7", "8"]


def test_cancel_all_open_orders_resilient_to_single_failure():
    """单笔撤单抛异常不中断后续，logger.exception 记录后继续。

    Why：熔断路径必须尽最大努力撤完所有未终态单，一笔柜台异常不能让其余
    敞口单继续暴露——熔断的物理意图就是「宁可错杀也要把所有口子堵上」。
    """
    class FailGW(_FakeGW):
        async def cancel_order(self, oid):
            if oid == "1":
                raise RuntimeError("模拟柜台超时")
            self.cancelled.append(oid)

    gw = FailGW({
        "1": {"state": OrderState.SUBMITTED},
        "2": {"state": OrderState.SUBMITTED},
    })
    n = asyncio.run(cb.cancel_all_open_orders(gw))
    # 第一笔失败被吞，第二笔仍被撤
    assert n == 1
    assert gw.cancelled == ["2"]


def test_cancel_all_open_orders_missing_orders_attr():
    """gw 无 _orders 属性时返 0，不抛 AttributeError（防御性：熔断路径不允许未捕获异常）。"""
    class BareGW:
        async def cancel_order(self, oid):
            raise AssertionError("不应被调用")

    n = asyncio.run(cb.cancel_all_open_orders(BareGW()))
    assert n == 0


def test_cancel_all_open_orders_empty():
    """_orders 为空字典时返 0。"""
    gw = _FakeGW({})
    assert asyncio.run(cb.cancel_all_open_orders(gw)) == 0
    assert gw.cancelled == []
