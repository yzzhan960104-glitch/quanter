# -*- coding: utf-8 -*-
"""盘后对账单测（Task 7）。

测试物理意图（Why 这么测）：
- run_reconcile 是「调度 + 偏差告警」的薄封装，对账纯逻辑在 reconcile() 里
  （已被 tests/trading/test_circuit_breaker.py 等覆盖）。本测试只验证：
  1) 无漂移（broker == local）→ rec.is_ok True → 不触发告警；
  2) 有漂移（only_local 或 drifted）→ rec.is_ok False → 触发告警路径。
- notifier 是网络副作用（真发钉钉），测试必须 mock 掉 fire_and_forget，
  断言它「被调用」而非「真发」。
- 用真实 reconcile() 构造 ReconciliationResult（不用 dict 凭空臆造），
  确保测试反映 ReconciliationResult 的真实结构（list[PositionDrift] + is_ok）。
"""
import asyncio

import pytest

from trading import reconcile_job
from trading.execution_gateway import reconcile


class FakeGW:
    """对账测试用的假网关：注入一个券商持仓字典，sync_positions 复用真实 reconcile。

    Why 复用真实 reconcile 而非凭空构造 dict：ReconciliationResult 字段是
    list[PositionDrift]，is_ok 由 reconcile 内部按 drifted/only_local/only_broker
    三类推导；测试若绕过 reconcile 手搓 dataclass，一旦字段语义改了测试不会失败，
    失去保护意义。直接调真实纯函数是最小代价的「结构真实」。
    """

    def __init__(self, broker_positions: dict[str, float]) -> None:
        self._broker = broker_positions

    async def sync_positions(self, local, tolerance=0.0):
        # 复刻 BaseExecutionGateway.sync_positions 模板方法的骨架：
        # 拉券商 → 调 reconcile 纯函数 → 返 ReconciliationResult。
        return reconcile(local, self._broker, tolerance)


def _run(coro):
    """同步跑 async，便于在 sync 测试函数里断言。"""
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


def test_reconcile_no_drift_does_not_alert(monkeypatch):
    """无漂移：broker == local → rec.is_ok True → 不触发 notifier。"""
    called: list[tuple] = []

    # mock 掉 fire_and_forget：断言「不被调用」；真发钉钉会污染测试环境。
    # _alert_drift 函数级 import core.notifier，patch 真实模块符号即可生效。
    monkeypatch.setattr("core.notifier.fire_and_forget", lambda coro: called.append(("fire", coro)))

    local = {"510300.SH": 100.0, "600000.SH": 200.0}
    gw = FakeGW(dict(local))  # broker 与 local 完全一致

    rec = _run(reconcile_job.run_reconcile(gw, dict(local), tolerance=0.0))

    assert rec.is_ok is True, "broker==local 必无漂移，is_ok 应为 True"
    assert called == [], "无漂移时绝不应触发告警"


def _close_coro_factory(callback):
    """构造 mock fire_and_forget：记录被调用 + 关闭未 await 的协程避免 RuntimeWarning。

    Why：fire_and_forget 生产语义是「后台调度不阻塞」，协程在 daemon 线程里 await。
    测试里我们不想真起线程，但直接丢弃协程会触发 RuntimeWarning（协程从未 await）；
    显式 close() 关闭协程是最干净的「占位」语义。
    """
    def fake(coro):
        try:
            callback()
        finally:
            # 协程未 await，显式 close 避免资源告警
            try:
                coro.close()
            except Exception:
                pass
    return fake


def test_reconcile_only_local_triggers_alert(monkeypatch):
    """有漂移·only_local 类：券商缺 000001.SZ → 本地乐观记账 → 触发告警。

    风险场景：本地以为成交了 100 股但券商无对应持仓，疑似订单未真正成交或
    丢单（网络超时后本地乐观记账），会让策略高估持仓、超额下单。
    """
    captured: list[str] = []
    monkeypatch.setattr(
        "core.notifier.fire_and_forget",
        _close_coro_factory(lambda: captured.append("called")),
    )

    local = {"510300.SH": 100.0, "000001.SZ": 100.0}
    # broker 缺 000001.SZ → 该标的归入 only_local
    broker = {"510300.SH": 100.0}
    gw = FakeGW(broker)

    rec = _run(reconcile_job.run_reconcile(gw, dict(local), tolerance=0.0))

    assert rec.is_ok is False, "only_local 非空时 is_ok 必为 False"
    assert len(rec.only_local) == 1
    assert rec.only_local[0].symbol == "000001.SZ"
    assert captured == ["called"], "有漂移必须触发 fire_and_forget 告警路径"


def test_reconcile_drifted_qty_triggers_alert(monkeypatch):
    """有漂移·drifted 类：数量不等 → 敞口失真 → 触发告警。

    风险场景：drifted 是最危险的漂移——本地记 100 股、券商只记 90，可能
    部分成交未回写或回调丢消息，直接导致敞口失真。brief 原版 _has_drift 只
    看 only_*会漏掉 drifted，这里专门钉死该回归。
    """
    captured: list[str] = []
    monkeypatch.setattr(
        "core.notifier.fire_and_forget",
        _close_coro_factory(lambda: captured.append("called")),
    )

    local = {"600000.SH": 100.0}
    broker = {"600000.SH": 90.0}  # 数量差 10 股，零容差下归 drifted
    gw = FakeGW(broker)

    rec = _run(reconcile_job.run_reconcile(gw, dict(local), tolerance=0.0))

    assert rec.is_ok is False, "drifted 非空时 is_ok 必为 False"
    assert len(rec.drifted) == 1
    assert rec.drifted[0].symbol == "600000.SH"
    assert captured == ["called"], "drifted 类漂移必须触发告警（不能被 only_* 判定漏掉）"


def test_reconcile_alert_message_is_human_readable(monkeypatch):
    """告警消息必须把 PositionDrift 展开成可读中文，不能 str(list) 原样输出。

    Why：PositionDrift 是 frozen dataclass，str() 会得到
    'PositionDrift(symbol=..., local_qty=...)' 这种英文字段名堆砌，
    手机端钉钉推送给研究员根本看不懂——必须中文标明 symbol/本地/券商/偏差。

    How：把 notify_risk_event 改成同步函数，在**调用时**（非 await）就把 msg
    记下来——fire_and_forget mock 直接 close 协程即可（协程体不会执行），
    我们断言「msg 被构造且内容可读」，不需要 await 走完通道发送。
    """
    captured_msgs: list[str] = []

    class FakeMgr:
        # 同步：调用即记录 msg，返回一个空 awaitable 占位
        def notify_risk_event(self, msg, level="INFO"):
            captured_msgs.append(msg)

            class _NoopAwaitable:
                def __await__(self_):
                    return iter([])  # yield nothing → 直接 StopIteration

            return _NoopAwaitable()

    monkeypatch.setattr(
        "core.notifier.NotificationManager",
        type("NM", (), {"get_default": staticmethod(lambda: FakeMgr())}),
    )
    # fire_and_forget 占位：notify_risk_event 是同步函数，已记录 msg；
    # 这里它收到的不是 coroutine，直接吞掉即可。
    monkeypatch.setattr("core.notifier.fire_and_forget", lambda awaitable: None)

    local = {"510300.SH": 100.0, "000001.SZ": 100.0}
    broker = {"510300.SH": 100.0}  # 缺 000001.SZ → only_local
    gw = FakeGW(broker)

    _run(reconcile_job.run_reconcile(gw, dict(local), tolerance=0.0))

    assert len(captured_msgs) == 1, "应恰好推送一条告警"
    msg = captured_msgs[0]
    # 可读性断言：含中文键 + symbol + 数量，而非裸 dataclass repr
    assert "000001.SZ" in msg
    assert "本地" in msg or "券商" in msg, "告警须中文标明本地/券商，便于研究员识别"
    assert "PositionDrift(" not in msg, "禁止直接 str(list[PositionDrift])"


def test_reconcile_notifier_exception_does_not_block(monkeypatch):
    """notifier 失败不能阻塞对账主流程：仍需返回 ReconciliationResult。

    Why：对账是盘后风控关键产物，告警通道挂了（如钉钉限流/网络抖动）不应
    导致整个对账 job 抛异常——否则上层 scheduler 会因告警侧故障而丢对账结果，
    风险敞口彻底失明。与 qmt_gateway._on_disconnect_fatal 同模式（fire_and_forget
    try-except 吞异常）。
    """
    def exploding_fire_and_forget(coro):
        # 关闭协程再抛，避免 RuntimeWarning
        try:
            coro.close()
        except Exception:
            pass
        raise RuntimeError("钉钉通道爆炸")

    monkeypatch.setattr("core.notifier.fire_and_forget", exploding_fire_and_forget)

    local = {"000001.SZ": 100.0}
    broker: dict[str, float] = {}  # only_local
    gw = FakeGW(broker)

    # 不能抛——fire_and_forget 异常应被内部 try-except 吞掉
    rec = _run(reconcile_job.run_reconcile(gw, dict(local), tolerance=0.0))

    assert rec.is_ok is False
    assert len(rec.only_local) == 1
