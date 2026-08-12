# -*- coding: utf-8 -*-
"""QuoteBlackoutThrottle 行情黑屏节流语义测试（W1-A/T2 · 模块级可变状态收口）。

物理定位（spec §0.2 硬约束 5 · 行为等价红线）：
    stop_loss_monitor 在 live 模式下若所有相关标的均无有效 last_price（行情源整体失效 /
    xtdata 黑屏）→ 止损链路裸奔，必须 _alert_critical 推钉钉 CRITICAL 唤醒人工。原节流
    状态为 engine 模块级 ``_last_quote_blackout_alert_ts``（global 可变）+ 常量
    ``_QUOTE_BLACKOUT_ALERT_INTERVAL_S = 30 * 60``——W1-A「模块级可变状态收口」红线要求
    收敛为显式 dataclass + 经 EnginePorts 注入。本测试验证 QuoteBlackoutThrottle 节流语义
    与原模块级实现逐字等价：
        - ``should_alert(now)``：``now - last_ts >= interval`` 即可告警（首次 last_ts=0.0
          时 ``now >= interval`` 必返 True）。**单步查询，无跨步原子保证**（仅供单线程/兼容）。
        - ``mark(now)``：置 ``last_ts = now``（记录本次告警时间戳，下一轮判定基准）。单步写入。
        - ``fire_if_due(now)``：**生产主路径**——单一 Lock 内 check+mark 原子返回 True/False，
          杜绝并发双发（stop_loss_monitor 唯一调用入口）。单线程下与 should_alert+mark 两步
          逐字等价，并发下更安全。
        - 默认 ``interval=1800.0``（30min × 60s）对齐原 ``_QUOTE_BLACKOUT_ALERT_INTERVAL_S``。
        - 默认 ``last_ts=0.0`` 对齐原模块级初值（首次调用必触发告警）。
        - 线程安全（``threading.Lock``，与 ``trading/critical.py`` 同范式）：``fire_if_due``
          单一 Lock 内 check+mark 原子——N 线程裸调并发，至多一条拿 True（杜绝双发）。

测试边界：
    纯单元测试，不起 APScheduler / 不触达 engine / 不依赖行情源；仅验节流状态机语义。
"""
from __future__ import annotations

import threading

from trading.alerting import QuoteBlackoutThrottle


# ============================================================================
# 1. should_alert / mark 基础语义
# ============================================================================
def test_defaults_match_legacy_module_state():
    """默认 last_ts=0.0 / interval=1800.0 对齐原 engine 模块级初值与 30min 常量。"""
    t = QuoteBlackoutThrottle()
    assert t.last_ts == 0.0
    assert t.interval == 1800.0  # 30 * 60，原 _QUOTE_BLACKOUT_ALERT_INTERVAL_S


def test_should_alert_true_when_interval_elapsed():
    """last_ts=0.0 + now ≥ interval → True（首次调用必告警，对齐原模块级初值语义）。"""
    t = QuoteBlackoutThrottle(last_ts=0.0, interval=1800.0)
    # now=2000.0 ≥ 1800.0 → True
    assert t.should_alert(2000.0) is True


def test_should_alert_true_at_exact_boundary():
    """now - last_ts == interval 边界等价触发（``>=`` 语义，对齐原 ``_now_mono - last >= INTERVAL``）。"""
    t = QuoteBlackoutThrottle(last_ts=100.0, interval=1800.0)
    # 100 + 1800 = 1900 → 边界等价触发
    assert t.should_alert(1900.0) is True


def test_should_alert_false_within_interval():
    """now - last_ts < interval → False（节流窗口内不重复告警）。"""
    t = QuoteBlackoutThrottle(last_ts=100.0, interval=1800.0)
    # 1000 - 100 = 900 < 1800 → False
    assert t.should_alert(1000.0) is False


def test_mark_updates_last_ts():
    """mark(now) 置 last_ts = now（记录告警时间戳为下一轮判定基准）。"""
    t = QuoteBlackoutThrottle(last_ts=0.0, interval=1800.0)
    t.mark(500.0)
    assert t.last_ts == 500.0


# ============================================================================
# 2. 节流语义——should_alert + mark 组合（防告警风暴）
# ============================================================================
def test_throttle_semantics_first_alert_then_silent_within_interval():
    """两轮调用：第一轮 mark，第二轮 interval 内 should_alert=False（30min 节流核心断言）。

    对齐 test_quote_blackout_throttled_30min 语义：连续两轮黑屏只推一条 CRITICAL。
    """
    t = QuoteBlackoutThrottle(last_ts=0.0, interval=1800.0)
    # 第一轮：now=2000，可告警 → mark
    assert t.should_alert(2000.0) is True
    t.mark(2000.0)
    # 第二轮：now=3000，3000-2000=1000 < 1800 → 不告警
    assert t.should_alert(3000.0) is False


def test_throttle_re_alerts_after_interval_re_elapses():
    """interval 再次过后可再次告警（节流不是单次，是滚动窗口）。"""
    t = QuoteBlackoutThrottle(last_ts=0.0, interval=1800.0)
    t.mark(2000.0)
    # now=4000，4000-2000=2000 ≥ 1800 → 可再次告警
    assert t.should_alert(4000.0) is True


# ============================================================================
# 3. fire_if_due 原子主路径（生产 stop_loss_monitor 唯一调用入口）
# ============================================================================
def test_fire_if_due_returns_true_and_marks_when_interval_elapsed():
    """fire_if_due 首次调用返 True 并原子更新 last_ts（check+mark 单一 Lock 内完成）。"""
    t = QuoteBlackoutThrottle(last_ts=0.0, interval=1800.0)
    # now=2000 ≥ 1800 → True，且 last_ts 原子更新为 2000
    assert t.fire_if_due(2000.0) is True
    assert t.last_ts == 2000.0


def test_fire_if_due_returns_false_and_keeps_last_ts_within_interval():
    """节流窗口内 fire_if_due 返 False 且不动 last_ts（不误更新下一轮判定基准）。"""
    t = QuoteBlackoutThrottle(last_ts=2000.0, interval=1800.0)
    # 3000 - 2000 = 1000 < 1800 → False，last_ts 不动
    assert t.fire_if_due(3000.0) is False
    assert t.last_ts == 2000.0


def test_fire_if_due_two_rounds_first_alert_then_silent_within_interval():
    """两轮 fire_if_due：首轮 True 并 mark，次轮窗口内 False（30min 节流核心断言）。

    对齐 test_quote_blackout_throttled_30min 语义：连续两轮黑屏只推一条 CRITICAL。
    与 test_throttle_semantics_first_alert_then_silent_within_interval（should_alert+mark
    两步组合）对照——单线程下两者逐字等价（行为等价红线）。
    """
    t = QuoteBlackoutThrottle(last_ts=0.0, interval=1800.0)
    # 第一轮：now=2000，可告警 → True + mark
    assert t.fire_if_due(2000.0) is True
    # 第二轮：now=3000，3000-2000=1000 < 1800 → False（不告警，不动 last_ts）
    assert t.fire_if_due(3000.0) is False
    assert t.last_ts == 2000.0


# ============================================================================
# 4. 线程安全——fire_if_due 裸调并发（验类内 Lock 原子性，非外部锁串行化）
# ============================================================================
def test_thread_safety_concurrent_fire_if_due_no_double_fire():
    """并发线程**裸调** ``fire_if_due``：单一 Lock 内 check+mark 原子，至多一条线程拿 True。

    物理意图（验类内锁原子性，非外部锁串行化）：
        stop_loss_monitor 在 IntervalTrigger(30s) 下虽单驱动，但 catchup 补跑 / 手工触发
        / _alert_critical 内部 fire_and_forget daemon 线程理论可重叠。``fire_if_due`` 把
        check（should_alert）+ act（mark）收进单一 ``with self._lock:`` 互斥区——N 线程
        同时裸调，至多一条线程看到 True 并完成 mark，其余线程看到已更新的 last_ts 返回
        False。

    反模式对照（I-1 fix）：
        本测试**裸调** fire_if_due（不持外部锁），真正验证类内 Lock 原子性——若类内锁
        失效（删 ``self._lock`` 或 fire_if_due 拆成非原子 should_alert+mark 两步暴露给
        调用方），20 线程并发下多线程可同时看到 last_ts=0.0 满足 ``now - last_ts >= interval``
        → 都返回 True → fire_count > 1（测试红）。原实现用外部 ``with t._lock:`` 包
        should_alert+mark 的测试是 tautology（外部锁串行化后类内 RLock 零覆盖）。
    """
    t = QuoteBlackoutThrottle(last_ts=0.0, interval=1800.0)
    fire_count = 0
    fire_lock = threading.Lock()

    def racer():
        nonlocal fire_count
        # 裸调 fire_if_due——不持外部锁，验类内 Lock 把 check+mark 收为互斥区。
        # 若类内锁失效，20 线程并发下多线程可同时看到 last_ts=0.0 满足条件 → fire_count > 1。
        if t.fire_if_due(2000.0):
            with fire_lock:
                fire_count += 1

    threads = [threading.Thread(target=racer) for _ in range(20)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    # 契约：至多一条线程拿 True（fire_if_due 原子 check+mark）。实际因 all 同 now=2000.0
    # 首个获锁线程 mark 后其余线程见 last_ts=2000.0 → ``2000-2000=0 < 1800`` 返 False，
    # 故 fire_count 恰为 1；用 ``<= 1`` 表契约边界（防告警风暴红线）。
    assert fire_count <= 1, "fire_if_due 类内 Lock 原子：并发至多触发一次告警"
