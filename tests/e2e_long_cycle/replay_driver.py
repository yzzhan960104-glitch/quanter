# -*- coding: utf-8 -*-
"""组件1 ReplayDriver：23 日时序回放编排器（spec §3.2）。

物理意图：单进程 clock-freeze 逐日推进——遍历交易日历，每日 4 阶段（pipeline_then_eod/
pre_open/stoploss/post_close）freeze trading.clock.now 到对应 datetime 后调 job_runner。
盘中 stoploss 在 N 个时点各 freeze 一次（验证盘中即时触发）。单阶段异常记 DayResult.failures
不中断整体（生产同源软降级，spec §10）。

Why patch trading.clock.now 单一口子（C-6）：clock 无状态，patch 一处即冻结全包
（today/trading_day 一致派生），替代 patch 各模块 datetime。
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Callable, Iterable

# 每日 4 阶段的 freeze 时点（spec §3.2 + §5）。
# pipeline_then_eod 用 T 日 19:00；其余用 T+1 日（trading_day(T)）。
_PIPELINE_T = time(19, 0)        # T 日盘后采集+扫信号
_PRE_OPEN = time(9, 25)          # T+1 开盘前挂单
_STOPLOSS = time(10, 30)         # T+1 盘中（多时点由 intraday_timepoints 覆盖）
_POST_CLOSE = time(15, 30)       # T+1 盘后对账

# 默认盘中 8 时点（spec §3.2）。
DEFAULT_INTRADAY_TIMEPOINTS = [
    time(9, 30), time(10, 0), time(10, 30), time(11, 0),
    time(11, 30), time(13, 30), time(14, 30), time(15, 0),
]

# 单日 4 阶段名（job_runner 的 phase 参数取值）。
PHASE_PIPELINE = "pipeline_then_eod"
PHASE_PRE_OPEN = "pre_open"
PHASE_STOPLOSS = "stoploss"
PHASE_POST_CLOSE = "post_close"


@dataclass
class DayResult:
    """单日回放结果（供 ReportBuilder 汇总）。"""
    date: date
    # T+1 日（pre_open/post_close 的业务日期）；首日可能无 T+1（日历末日）。
    trading_day: date | None
    phase_results: dict[str, list[dict]] = field(default_factory=dict)  # phase -> [每时点 result]
    failures: list[str] = field(default_factory=list)  # "phase: 异常摘要"


JobRunner = Callable[[date, str], dict]
ClockFreezer = Callable[[datetime], "object"]  # 返 contextmanager


@contextmanager
def _freeze_clock(fixed: datetime):
    """patch trading.clock.now 到 fixed（C-6 单一口子冻结全包）。"""
    from unittest.mock import patch
    with patch("trading.clock.now", lambda: fixed):
        yield


class ReplayDriver:
    """时序回放编排器：日历驱动 + clock freeze + 每日 4 阶段 + 异常容错。

    Args:
        calendar: 交易日列表（如 7 月 23 日，从 data_lake 取）。
        job_runner: 每日每阶段回调 `(T_date, phase) -> result_dict`。V2-V5 在此注入真身。
        intraday_timepoints: stoploss 阶段的盘中 freeze 时点（默认 8 时点）。
        clock_freezer: 自定义 freeze（默认 patch trading.clock.now；测试可注入 mock）。
    """

    def __init__(
        self,
        calendar: list[date],
        job_runner: JobRunner,
        intraday_timepoints: list[time] | None = None,
        clock_freezer: ClockFreezer | None = None,
    ) -> None:
        self.calendar = calendar
        self.job_runner = job_runner
        self.intraday_timepoints = intraday_timepoints or DEFAULT_INTRADAY_TIMEPOINTS
        self._freeze = clock_freezer or _freeze_clock

    def _next_day(self, d: date) -> date | None:
        """日历的下一日（T+1）；末日返 None。"""
        try:
            idx = self.calendar.index(d)
        except ValueError:
            return None
        return self.calendar[idx + 1] if idx + 1 < len(self.calendar) else None

    def _run_phase(self, d: date, phase: str, freeze_dt: datetime, day: DayResult) -> None:
        """freeze clock 到 freeze_dt 后调 job_runner；异常记 day.failures 不抛。"""
        try:
            with self._freeze(freeze_dt):
                result = self.job_runner(d, phase)
            day.phase_results.setdefault(phase, []).append(result or {})
        except Exception as exc:
            # 生产同源软降级（spec §10）：单阶段异常不中断整体回放。
            day.failures.append(f"{phase}: {type(exc).__name__}: {exc}")

    def run(self) -> list[DayResult]:
        """遍历日历，每日跑 4 阶段（stoploss 多时点），返 DayResult 列表。"""
        results: list[DayResult] = []
        for d in self.calendar:
            t_plus_1 = self._next_day(d)
            day = DayResult(date=d, trading_day=t_plus_1)

            # ① pipeline_then_eod：T 日 19:00（采集 + 扫信号落 T+1 plan）
            self._run_phase(d, PHASE_PIPELINE,
                            datetime.combine(d, _PIPELINE_T), day)

            # T+1 不存在（日历末日）→ 仅跑 pipeline，跳过 T+1 三阶段
            if t_plus_1 is None:
                results.append(day)
                continue

            # ② pre_open：T+1 09:25（挂 T+1 单）
            self._run_phase(d, PHASE_PRE_OPEN,
                            datetime.combine(t_plus_1, _PRE_OPEN), day)

            # ③ stoploss：T+1 盘中 N 时点各 freeze 一次（盘中即时触发验证）
            for tp in self.intraday_timepoints:
                self._run_phase(d, PHASE_STOPLOSS,
                                datetime.combine(t_plus_1, tp), day)

            # ④ post_close：T+1 15:30（对账 + 熔断 + trailing + 超期 + 落表）
            self._run_phase(d, PHASE_POST_CLOSE,
                            datetime.combine(t_plus_1, _POST_CLOSE), day)

            results.append(day)
        return results


def load_july_calendar(lake_path: str = "data_lake/a_shares_daily.parquet") -> list[date]:
    """从 data_lake 取 7 月交易日列表（spec §3.2，与生产 trading.calendar 同源）。

    实测 7 月 23 交易日全覆盖（每日标的数 5218-5528，无缺采日）。
    """
    import pandas as pd
    df = pd.read_parquet(lake_path)
    dates = df.index.get_level_values("date")
    july = sorted(set(dates[(dates >= "2026-07-01") & (dates <= "2026-07-31")]))
    return [pd.Timestamp(d).date() for d in july]
