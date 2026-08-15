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

CR-3 追加（tech-debt-full-wave · 2026-08-15）：「盘中组合级 -3% 熔断评估点前移」的
节流/计数运行态同属此类可变状态（5min 评估节流 + 连续评估失败计数），按同一红线
同范式收敛为 ``PortfolioBreakerThrottle`` dataclass，经 ``EnginePorts.breaker_throttle``
注入 stop_loss_monitor（依赖方向与 blackout 完全同构）。

设计契约（行为等价 · 与原 engine 模块级实现逐字对齐）：
    - ``should_alert(now)`` ⟺ ``now - last_ts >= interval``（原 ``_now_mono - _last_ts >= _INTERVAL``）
      首次 ``last_ts=0.0`` → ``now >= interval`` 即 True（与原模块级初值 0.0 + 首轮触发等价）。
    - ``mark(now)`` ⟺ ``last_ts = now``（原 ``_last_ts = _now_mono``）。
    - 默认 ``interval=1800.0``（= ``30 * 60``）对齐原 ``_QUOTE_BLACKOUT_ALERT_INTERVAL_S``。
    - 默认 ``last_ts=0.0`` 对齐原模块级初值。
    - 线程安全（``threading.Lock``，与 ``trading/critical.py`` 同范式）：``fire_if_due``
      在单一 Lock 内 check+mark 原子返回——杜绝并发双发告警（M4 红线）。``should_alert`` /
      ``mark`` 是无跨步原子保证的分解方法（仅供单线程查询/兼容），生产路径勿组合调用
      （check-then-act 非原子，IntervalTrigger catchup 重叠 / daemon 并发下可双发）。

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

    线程安全（``threading.Lock``，与 ``trading/critical.py`` ``_halt`` 顺序契约互斥同范式）：
        ``fire_if_due`` 是**生产主路径**——在单一 Lock 内 check+mark 原子返回，并发下至多
        一条线程拿到 True（杜绝 IntervalTrigger catchup 重叠 / fire_and_forget daemon 线程
        并发双发）。``should_alert`` / ``mark`` 是**无跨步原子保证**的分解方法，仅供单线程
        查询/兼容旧调用方语义——生产路径勿组合调用（check-then-act 非原子，并发可双发）。

    Why ``Lock``（非 ``RLock``）：
        ``fire_if_due`` 单方法单次获取锁，无需可重入；``should_alert`` / ``mark`` 各自单次
        加锁亦无重入需求。``Lock`` 语义更窄、意图更明（对齐 brief 原意「threading.Lock，
        与 critical.py 同范式」），杜绝「外部 ``with _lock`` 组合 should_alert+mark」这类
        本不该存在的反模式调用（生产已统一走 ``fire_if_due`` 原子路径）。

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
    # __post_init__ 在 dataclass 字段赋值后建立 Lock（field 不能直接持有 Lock ——
    # threading.Lock 不可 pickle / 不可默认工厂共享，必须在实例化时新建）。
    # ---------------------------------------------------------------------------
    def __post_init__(self) -> None:
        # 互斥锁：``fire_if_due`` 在单一 Lock 内 check+mark 原子返回，杜绝并发双发。
        # 与 trading/critical.py _halt 顺序契约互斥同范式（critical 是 halt 顺序互斥，
        # 本类是节流状态机互斥——两者都是「daemon 线程下防 race」的同一套手段）。
        # dataclass 非 frozen，可直接赋值（M-7：简化 object.__setattr__ 冗余写法）。
        self._lock = threading.Lock()

    def should_alert(self, now: float) -> bool:
        """判定本轮是否应推告警（``now - last_ts >= interval`` 即可告警）。

        ⚠️ 单步查询，无跨步原子保证：调用方组合「should_alert → mark」是非原子的
        check-then-act——并发下两线程可同时看到 True 都去 mark → 双发告警。生产路径
        请用 ``fire_if_due``（单一 Lock 内 check+mark 原子返回）。本方法仅供单线程
        查询/兼容旧调用方语义。

        Args:
            now: 当前单调时钟时间戳（``time.monotonic()``，调用方传入）。

        Returns:
            True=应告警（已过节流窗口或首次调用）；False=节流窗口内（不重复推）。

        等价原 engine 模块级：
            ``_now_mono - _last_quote_blackout_alert_ts >= _QUOTE_BLACKOUT_ALERT_INTERVAL_S``
        """
        with self._lock:
            return (now - self.last_ts) >= self.interval

    def mark(self, now: float) -> None:
        """记录本轮告警时间戳（``last_ts = now``），作为下一轮节流判定基准。

        ⚠️ 单步写入，无跨步原子保证：必须与 ``should_alert`` 原子组合才安全，裸组合
        见 ``should_alert`` 警告。生产路径用 ``fire_if_due``。本方法仅供单线程/兼容。

        必须在 ``should_alert(now)=True`` 后调用以更新基准；忘调 → 节流失效（每轮都触发）。

        Args:
            now: 当前单调时钟时间戳（应与配套 ``should_alert`` 同一 ``now``，保证一致基准）。

        等价原 engine 模块级：
            ``_last_quote_blackout_alert_ts = _now_mono``
        """
        with self._lock:
            self.last_ts = now

    def fire_if_due(self, now: float) -> bool:
        """原子节流判定 + 时间戳更新（**生产主路径**，stop_loss_monitor 唯一调用入口）。

        在单一 ``with self._lock:`` 内完成 check（``now - last_ts >= interval``）+ mark
        （``last_ts = now``）——check-then-act 跨步被收进互斥区，并发下至多一条线程返回
        True（其余线程看到已更新的 last_ts → 返回 False），杜绝 IntervalTrigger catchup
        重叠 / fire_and_forget daemon 线程并发下的告警双发（M4 红线）。

        单线程下与 ``should_alert(now)=True; mark(now)`` 两步语义逐字等价（行为等价红线）；
        并发下更安全（原子）。故生产路径统一走本方法，``should_alert`` / ``mark`` 不再
        组合调用。

        Args:
            now: 当前单调时钟时间戳（``time.monotonic()``，调用方传入）。

        Returns:
            True=应告警（已过窗口，本调用已原子更新 last_ts）；False=节流窗口内（不动 last_ts）。
        """
        with self._lock:
            if (now - self.last_ts) >= self.interval:
                self.last_ts = now
                return True
            return False


@dataclass
class PortfolioBreakerThrottle:
    """盘中组合级熔断 5min 评估节流 + 连续评估失败计数状态机（CR-3 · 2026-08-15）。

    物理意图（tech-debt CR-3「评估点前移」· 三分支设计的公共基础设施）：
        「日内 -3% 组合熔断」原唯一判定点在 15:30 post_close（盘后闸）——盘中穿线后
        敞口要裸奔至收盘才停手。CR-3 把同一判定（同 ``get_start_equity`` 基线读口 +
        同 ``check_daily_loss_limit`` 纯判定）前移进 stop_loss_monitor 的 30s 巡检。
        但 ``query_asset`` 是柜台同步 C++ 查询（投线程池 + 超时兜底），每 30s 评估
        一轮既打柜台又会在触发/失明场景刷屏。本状态机承载两职：
        - ``should_check``：5min 评估节流（同窗内至多评估一次，单一 Lock 内
          check+mark 原子——与 ``QuoteBlackoutThrottle.fire_if_due`` 同范式）；
        - ``miss_streak``：连续评估失败（query_asset 断线返 {}/异常）计数，≥3 才推
          CRITICAL——单次抖动不叫醒人工，持续失明（3×5min=15min）才升级观测。

    Why 经 EnginePorts 注入而非模块级 global（W1-A「模块级可变状态收口」红线）：
        节流/计数是跨轮巡检的可变运行态，模块级 global 会被 engine/测试/多实例互相
        污染；dataclass 实例 + ``ports.breaker_throttle`` 显式透传让状态生命周期绑定
        engine 实例（每 TradingEngine 一份，30s 巡检共享同一份 → 节流窗口与失败
        计数才跨轮连续）。与 ``QuoteBlackoutThrottle``（ports.blackout）完全同构。

    线程安全（``threading.Lock``，与本模块 QuoteBlackoutThrottle / trading.critical
    同范式）：``should_check`` 在单一 Lock 内 check+mark 原子返回——IntervalTrigger
    catchup 重叠 / fire_and_forget daemon 并发下至多一条线程拿到 True；record_miss /
    record_success / reset 亦各自单次加锁，无重入需求故用 ``Lock`` 非 ``RLock``。

    Attributes:
        last_check_ts: 上次评估时间戳（``time.monotonic()`` 域，单调时钟避开系统时钟
            漂移）。默认 ``0.0`` = 从未评估，首轮 ``now - 0 >= interval`` 必触发。
        interval: 评估节流窗口（秒），默认 ``300.0``（= 5min，CR-3 设计锁定值：
            30s 巡检 × 10 轮评一次——柜台查询频率与告警频次的平衡点）。
        miss_streak: 连续评估失败计数（成功评估且未触发即清零；≥3 推 CRITICAL）。
    """

    last_check_ts: float = 0.0
    interval: float = 300.0   # 5min（CR-3 设计锁定）
    miss_streak: int = 0

    # ---------------------------------------------------------------------------
    # __post_init__ 在 dataclass 字段赋值后建立 Lock（threading.Lock 不可 pickle /
    # 不可默认工厂共享，必须实例化时新建——与 QuoteBlackoutThrottle 同款）。
    # ---------------------------------------------------------------------------
    def __post_init__(self) -> None:
        # 互斥锁：节流判定 + 失败计数全部单次加锁收口，杜绝并发双评估/双计数。
        self._lock = threading.Lock()

    def should_check(self, now: float) -> bool:
        """原子「评估窗口判定 + 占坑」（check+mark 单一 Lock 内，**生产主路径**）。

        ``now - last_check_ts >= interval`` 即到窗：本调用原子更新 last_check_ts 并返
        True（占住本轮评估权），窗内返 False（不动 last_check_ts）。check-then-act
        跨步收进互斥区，并发下至多一条线程拿到 True——与 QuoteBlackoutThrottle.
        fire_if_due 同范式（I-1 fix 的原子化理由同源）。

        Args:
            now: 当前单调时钟时间戳（``time.monotonic()``，调用方传入）。

        Returns:
            True=到窗应评估（已原子占坑）；False=5min 窗内（本轮跳过）。
        """
        with self._lock:
            if (now - self.last_check_ts) >= self.interval:
                self.last_check_ts = now
                return True
            return False

    def record_miss(self) -> int:
        """记一次评估失败（curr 取不到），原子自增并返新计数。

        Why 返回新计数（而非裸 void）：调用方判 ``>= 3`` 需读自增后的值，「自增+读」
        收进同一互斥区才是真原子（分开则并发下两轮 miss 可同读 2 → 双双不告警漏升线）。

        Returns:
            自增后的 miss_streak（调用方据此判 ≥3 推 CRITICAL）。
        """
        with self._lock:
            self.miss_streak += 1
            return self.miss_streak

    def record_success(self) -> None:
        """评估成功且未触发 → miss_streak 清零。

        物理意图：断线计数不跨恢复期累积——网关自愈后首轮成功评估即归零，后续再断线
        从 0 重新数（≥3 升线语义恒为「连续失明 15min」而非「当日累计失明」）。
        """
        with self._lock:
            self.miss_streak = 0

    def reset(self) -> None:
        """全量归零（last_check_ts + miss_streak）——复位口（测试隔离/运维重置用）。

        与 QuoteBlackoutThrottle 的差异：本类无 mark/should_alert 分解方法（消费方
        只走 should_check 原子路径，无旧调用方兼容包袱）；reset 供测试显式归零复用
        同一实例的场景（M4 式 conftest autouse 的复位语义同源）。
        """
        with self._lock:
            self.last_check_ts = 0.0
            self.miss_streak = 0
