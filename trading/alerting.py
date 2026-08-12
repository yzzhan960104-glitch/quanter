# -*- coding: utf-8 -*-
"""行情黑屏节流告警状态机（W1-A/T2 · 模块级可变状态收口 · 集群 A 基础设施）。

物理定位（spec §0.2 硬约束 5 · 行为等价红线 + W1-A「模块级可变状态收口」红线）：
    stop_loss_monitor 在 live 模式下若盘中所有相关标的均无有效 last_price（行情源整体失效
    / xtdata 黑屏）→ 止损链路裸奔，必须 _alert_critical 推钉钉 CRITICAL 唤醒人工介入。
    每 30s（IntervalTrigger）推一条 = 告警风暴（运营群麻木）；原节流由 engine 模块级
    可变状态实现：
        - ``_last_quote_blackout_alert_ts: float = 0.0``（上次告警时间戳，``time.monotonic`` 域）
        - ``_QUOTE_BLACKOUT_ALERT_INTERVAL_S = 30 * 60``（节流窗口 = 30min）

    W1-A「模块级可变状态收口」红线要求把上述 global 可变状态收敛为显式 dataclass 实例
    + 经 EnginePorts 注入（依赖方向：engine 持实例 → ports.blackout → stop_loss_monitor）。
    本模块定义该 dataclass：``QuoteBlackoutThrottle``。

设计契约（行为等价 · 与原 engine 模块级实现逐字对齐）：
    - ``should_alert(now)`` ⟺ ``now - last_ts >= interval``（原 ``_now_mono - _last_ts >= _INTERVAL``）
      首次 ``last_ts=0.0`` → ``now >= interval`` 即 True（与原模块级初值 0.0 + 首轮触发等价）。
    - ``mark(now)`` ⟺ ``last_ts = now``（原 ``_last_ts = _now_mono``）。
    - 默认 ``interval=1800.0``（= ``30 * 60``）对齐原 ``_QUOTE_BLACKOUT_ALERT_INTERVAL_S``。
    - 默认 ``last_ts=0.0`` 对齐原模块级初值。
    - 线程安全（``threading.Lock``，与 ``trading/critical.py`` 同范式）：保护
      should_alert + mark 的 read-modify-write 原子性。生产路径 IntervalTrigger(30s) 单驱动，
      但 catchup 补跑 / 手工触发 / fire_and_forget daemon 线程理论可并发；Lock 把节流判定
      收为互斥区，杜绝告警风暴（M4 红线）。

T1 拆分红线（缝合点设计）：
    本模块属「集群 A · 最独立基础设施」（零下游交易耦合）：不依赖 engine / phases / 网关 /
    行情源 / DB，仅持节流状态机纯逻辑。engine 侧构造 ``QuoteBlackoutThrottle()`` 装入
    ``EnginePorts.blackout``，stop_loss_monitor 经 ``ports.blackout`` 读写——依赖方向由
    隐式模块级 global 反查变为显式参数透传（与 pre_open 经 ports.gate 同口径）。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass


@dataclass
class QuoteBlackoutThrottle:
    """行情黑屏 30min 节流告警状态机（原 engine 模块级 ``_last_quote_blackout_alert_ts`` 收口）。

    物理意图（spec §4.6 R2 降级告警 + M4 告警风暴红线）：
        xtdata 行情源整体失效（所有相关标的无有效 last_price）→ 止损链路裸奔 → 必须
        _alert_critical 推钉钉 CRITICAL 唤醒人工。每 30s 一轮巡检若无节流 → 一次黑屏
        事件推 60+ 条告警/小时（运营群麻木，违背 M4「告警风暴」红线）。本类把告警频次
        降至「至多每 30min 一条」，平衡「尽快知会」与「不刷屏」。

    线程安全（``threading.RLock``，与 ``trading/critical.py`` ``_halt`` 顺序契约互斥同范式）：
        ``should_alert`` + ``mark`` 各自加锁（方法级互斥）；调用方需「should_alert → mark」
        原子串行时显式 ``with throttle._lock:`` 包裹两步——``RLock`` 可重入，允许外部
        持锁后调用本类方法（``Lock`` 不可重入会自死锁）。
        生产路径 IntervalTrigger(30s) 单驱动下无须显式组合，但 catchup 补跑 / 手工触发
        / fire_and_forget daemon 线程理论可重叠；显式 ``with _lock`` 留给需要原子串行的
        调用方（stop_loss_monitor 当前用 should_alert 判 + mark 写两步，未来可收敛为单
        ``fire_if_due`` 方法 follow-up）。

    Why ``RLock`` 而非 ``Lock``：
        ``should_alert`` / ``mark`` 单独公开，调用方组合「check-then-act」时需要外层
        持锁——非重入 ``Lock`` 会在方法二次获取时死锁。``RLock`` 同线程可重入，方法级
        加锁与外层组合锁并存无冲突（与 GIL 下 Python 互斥语义最小代价对齐）。

    Attributes:
        last_ts: 上次告警时间戳（``time.monotonic()`` 域，单调时钟避开系统时钟漂移）。
            默认 ``0.0`` 对齐原 engine 模块级 ``_last_quote_blackout_alert_ts`` 初值
            （首次调用 ``now >= interval`` 必触发）。
        interval: 节流窗口（秒）。默认 ``1800.0``（= 30 × 60），对齐原
            ``_QUOTE_BLACKOUT_ALERT_INTERVAL_S``。窗口内重复 should_alert 返 False。
    """

    last_ts: float = 0.0
    interval: float = 1800.0  # 30 * 60（原 _QUOTE_BLACKOUT_ALERT_INTERVAL_S）

    # ---------------------------------------------------------------------------
    # __post_init__ 在 dataclass 字段赋值后建立 RLock（field 不能直接持有 RLock ——
    # threading.RLock 不可 pickle / 不可默认工厂共享，必须在实例化时新建）。
    # ---------------------------------------------------------------------------
    def __post_init__(self) -> None:
        # 可重入互斥锁：保护 should_alert / mark 的 read-modify-write 语义，
        # 允许调用方在 ``with throttle._lock:`` 下原子串行调 should_alert + mark。
        # 与 trading/critical.py _halt 顺序契约互斥同范式（critical 是 halt 顺序互斥，
        # 本类是节流状态机互斥——两者都是「daemon 线程下防 race」的同一套手段）。
        object.__setattr__(self, "_lock", threading.RLock())

    def should_alert(self, now: float) -> bool:
        """判定本轮是否应推告警（``now - last_ts >= interval`` 即可告警）。

        Args:
            now: 当前单调时钟时间戳（``time.monotonic()``，调用方传入）。

        Returns:
            True=应告警（已过节流窗口或首次调用）；False=节流窗口内（不重复推）。

        等价原 engine 模块级：
            ``_now_mono - _eng_mod._last_quote_blackout_alert_ts >= _eng_mod._QUOTE_BLACKOUT_ALERT_INTERVAL_S``
        """
        with self._lock:
            return (now - self.last_ts) >= self.interval

    def mark(self, now: float) -> None:
        """记录本轮告警时间戳（``last_ts = now``），作为下一轮节流判定基准。

        必须在 ``should_alert(now)=True`` 后调用以更新基准；忘调 → 节流失效（每轮都触发）。

        Args:
            now: 当前单调时钟时间戳（应与配套 ``should_alert`` 同一 ``now``，保证一致基准）。

        等价原 engine 模块级：
            ``_eng_mod._last_quote_blackout_alert_ts = _now_mono``
        """
        with self._lock:
            self.last_ts = now
