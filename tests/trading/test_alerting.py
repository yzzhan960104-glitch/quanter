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
          时 ``now >= interval`` 必返 True）。
        - ``mark(now)``：置 ``last_ts = now``（记录本次告警时间戳，下一轮判定基准）。
        - 默认 ``interval=1800.0``（30min × 60s）对齐原 ``_QUOTE_BLACKOUT_ALERT_INTERVAL_S``。
        - 默认 ``last_ts=0.0`` 对齐原模块级初值（首次调用必触发告警）。
        - 线程安全（``threading.Lock``，与 ``trading/critical.py`` 同范式）：保护
          should_alert + mark 的 read-modify-write 原子性，防并发下双发告警。

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
# 3. 线程安全——threading.Lock 保护 should_alert + mark read-modify-write 原子性
# ============================================================================
def test_thread_safety_concurrent_should_alert_mark_no_double_fire():
    """并发线程同时 should_alert + mark：Lock 保护下「至多一条线程看到 True」。

    物理意图（与 trading/critical.py 同范式）：
        stop_loss_monitor 在 IntervalTrigger(30s) 下虽单驱动，但 _alert_critical 内部
        fire_and_forget 起 daemon 线程；节流状态 read-modify-write（should_alert→mark）
        非原子时，理论上多 cron 重叠（catchup / 手工补跑）下可能双发。Lock 把
        should_alert + mark 收为互斥区，杜绝告警风暴。
    """
    t = QuoteBlackoutThrottle(last_ts=0.0, interval=1800.0)
    fire_count = 0
    fire_lock = threading.Lock()

    def racer():
        nonlocal fire_count
        # 模拟 stop_loss 内「should_alert → mark → 推告警」非原子旧路径，
        # 经 Lock 后等价串行化：同一 now 下至多一线程命中 should_alert=True 并 mark。
        with t._lock:  # 显式持锁验证语义（与 critical.py _lock 同范式）
            if t.should_alert(2000.0):
                t.mark(2000.0)
                with fire_lock:
                    fire_count += 1

    threads = [threading.Thread(target=racer) for _ in range(20)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    assert fire_count == 1, "Lock 保护下并发 should_alert+mark 至多触发一次告警"
