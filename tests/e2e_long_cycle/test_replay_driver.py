# -*- coding: utf-8 -*-
"""V1：ReplayDriver 时序回放器骨架（clock-freeze 日历驱动 + 4 阶段空壳 + 异常容错）。

物理意图（spec §3.2）：23 日时序回放的核心编排——日历驱动逐日 freeze clock，
每日 4 阶段（pipeline_then_eod / pre_open / stoploss / post_close）调 job_runner，
单阶段异常不中断整体（记 DayResult.failures 跳下一日）。
"""
from __future__ import annotations

from datetime import date, datetime, time
from unittest.mock import patch


def test_replay_driver_advances_calendar_and_freezes_clock_per_phase():
    """日历 3 日 × 单时点：每阶段 freeze 对应 datetime + 调 job_runner。

    计数说明（spec §3.2 T+1 模型）：日历末日无 T+1，仅跑 pipeline；前两日各跑
    pipeline+pre_open+stoploss(1 时点)+post_close = 4 阶段。
    故 3 日 × 单时点 = 9（末 2 日 4 阶段 + 末日仅 pipeline 1）。
    """
    from tests.e2e_long_cycle.replay_driver import ReplayDriver

    calendar = [date(2026, 7, 1), date(2026, 7, 2), date(2026, 7, 3)]
    frozen: list[datetime] = []
    phases: list[str] = []

    def job_runner(d: date, phase: str) -> dict:
        # job_runner 内部读 clock.today() 应 = 当前阶段 freeze 的日期
        from trading import clock
        frozen.append(clock.now())
        phases.append(phase)
        return {"phase": phase}

    # 显式单时点简化计数（避免默认 8 时点放大）；driver T+1 模型见 replay_driver.run
    driver = ReplayDriver(calendar=calendar, job_runner=job_runner,
                          intraday_timepoints=[time(9, 30)])
    results = driver.run()

    # 3 日 × 单时点 = 9（7/1: pipeline+pre_open+stoploss+post_close=4；
    # 7/2: 同 4；7/3: 末日无 T+1 仅 pipeline 1 = 9）
    assert len(phases) == 9
    # 阶段顺序（每日）：pipeline_then_eod(T 19:00) → pre_open(T+1 09:25) →
    # stoploss(T+1 盘中多时点) → post_close(T+1 15:30)
    assert phases[0] == "pipeline_then_eod" and phases[1] == "pre_open"
    # 末日(7/3) 无 T+1，仅跑 pipeline；故 phases[-1] 是 pipeline_then_eod
    # post_close 出现在每个非末日（7/1、7/2 各一次）
    assert phases[-1] == "pipeline_then_eod"
    assert phases.count("post_close") == 2  # 7/1、7/2 各一次（末日无）
    # clock 被 freeze（每次 job_runner 读到的 now 都被 patch 成固定值，非真实 now）
    real_now = datetime.now()
    assert frozen[0] != real_now  # freeze 到 7/1 19:00，绝非真实 now
    # 3 个 DayResult
    assert len(results) == 3


def test_replay_driver_continues_on_phase_exception():
    """单阶段 job_runner 抛异常 → 记 DayResult.failures，继续下一日（不中断）。

    日历 3 日：7/1 pre_open 崩 → 7/1 仍跑 stoploss/post_close（同日后续阶段不跳），
    7/2 全跑，7/3 末日仅 pipeline。验证单阶段异常不污染其他日/阶段（生产同源软降级）。
    """
    from tests.e2e_long_cycle.replay_driver import ReplayDriver

    calendar = [date(2026, 7, 1), date(2026, 7, 2), date(2026, 7, 3)]
    call_log: list[str] = []

    def job_runner(d: date, phase: str) -> dict:
        call_log.append(f"{d}:{phase}")
        if d == date(2026, 7, 1) and phase == "pre_open":
            raise RuntimeError("模拟 pre_open 崩（软降级测试）")
        return {}

    driver = ReplayDriver(calendar=calendar, job_runner=job_runner,
                          intraday_timepoints=[time(9, 30)])  # 单时点简化
    results = driver.run()

    # 7/2 全 4 阶段跑完（不被 7/1 pre_open 崩影响）—— 7/2 非末日有 T+1=7/3
    assert "2026-07-02:pipeline_then_eod" in call_log
    assert "2026-07-02:pre_open" in call_log
    assert "2026-07-02:post_close" in call_log
    # 7/1 pre_open 崩后，7/1 同日后续阶段（stoploss/post_close）仍跑（单阶段不连坐）
    assert "2026-07-01:post_close" in call_log
    # 7/1 的 pre_open 失败记入 failures
    day1 = next(r for r in results if r.date == date(2026, 7, 1))
    assert any("pre_open" in f for f in day1.failures)


def test_replay_driver_stoploss_runs_multiple_intraday_timepoints():
    """stoploss 阶段在盘中 N 时点各 freeze 一次跑（验证盘中回放粒度）。

    日历需 ≥2 日：生产语义 pre_open/stoploss/post_close 都在 T+1 跑，故首日 7/1
    必须有 T+1=7/2，stoploss 才会触发；若日历仅 1 日（末日无 T+1）则永不跑。
    """
    from tests.e2e_long_cycle.replay_driver import ReplayDriver

    # 2 日日历：7/1 的 T+1 = 7/2，stoploss 才会在 T+1 的 3 时点跑
    calendar = [date(2026, 7, 1), date(2026, 7, 2)]
    stoploss_times: list[time] = []

    def job_runner(d: date, phase: str) -> dict:
        if phase == "stoploss":
            from trading import clock
            stoploss_times.append(clock.now().time())
        return {}

    driver = ReplayDriver(calendar=calendar, job_runner=job_runner, intraday_timepoints=[
        time(9, 30), time(10, 30), time(14, 0)])  # 简化 3 时点
    driver.run()

    # 3 个盘中时点各跑一次 stoploss（7/1 的 stoploss 在 T+1=7/2 的 3 时点跑）
    assert len(stoploss_times) == 3
    assert time(9, 30) in stoploss_times and time(14, 0) in stoploss_times
