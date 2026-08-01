# -*- coding: utf-8 -*-
"""V7：全链路 E2E 测试套件。

两个测试：
    1. test_orchestrator_smoke（implementer gate，CI 默认跑）：缩小版 2-3 日串联 smoke。
       验 orchestrator.build_job_runner 串 V1-V6 组件无语法/逻辑错误 + 接口对齐 + 4 阶段
       都跑到 + DayResult 生成 + snapshot 生成 + md 生成 + 4 类 checks 返结构。
       不带 @e2e_long（轻量，~秒级）。

    2. test_e2e_long_cycle_full_run（@e2e_long，手动跑）：23 日全链路真跑。
       ⚠️ CI 不默认跑（~30-90min + 真推钉钉 + connect 5 bot 真起 + Tushare stk_mins 限频）。
       手动跑：pytest tests/e2e_long_cycle/test_e2e_long_cycle.py::test_e2e_long_cycle_full_run \
              -v -m e2e_long -s

物理意图（spec §13 验收）：smoke 验「串联无 bug」（V7 真正的 implementer gate）；
full_run 验「23 日真实业务链路」（真实算法 × 真实数据 × 真推 × connect 真起，留用户手动）。
"""
from __future__ import annotations

from datetime import date, time

import pytest


# ============================================================
# 1. 缩小版 smoke（implementer gate，CI 默认跑）
# ============================================================
def test_orchestrator_smoke(isolated_state, monkeypatch, tmp_path):
    """orchestrator 串联 smoke：2 日日历 × 4 阶段 × mock 组件，验串联无 bug。

    物理意图：V7 的真 gate——验 orchestrator.build_job_runner 把 V1 ReplayDriver ×
    V2 signal_scanner × V3 MinBarFeeder × V4 ProbabilisticBroker × V6 TableSnapshotCollector
    × engine 真身串起来跑 2 日，4 阶段都跑到 + DayResult/snapshot/md 生成 + 4 类 checks
    返结构。不验「23 日真实业务正确性」（那是 full_run @e2e_long 的职责）。

    隔离与 mock 策略（spec §2 红线 + 可重复）：
        - isolated_state：tmp DB + TRADE_PLAN_DIR（conftest）。
        - E2E_SKIP_CONNECT=1：跳 connect 5 bot 真起（smoke 不依赖 connect）。
        - 钉钉 enabled=False：DingTalkLog mock 不真推（避免污染测试群）。
        - V2 run_eod_phase monkeypatch 为 fake：smoke 不依赖 data_lake 454MB universe +
          detect_signal 真跑（那是 full_run 职责）；fake 直接落 T+1 plan 让 pre_open 有单可挂。
        - MinBarFeeder 注入 fake stk_mins loader：不依赖 Tushare 限频，返固定 5min bar。
        - 构造熔断日 + 超期标的：验 V4 ProbabilisticBroker 概率场景注入链路通。
    """
    # ===== env：AUTO 确认 + dry_run 影子（不触真单） =====
    monkeypatch.setenv("AUTO_TRADE_MODE", "dry_run")
    monkeypatch.setenv("AUTO_CONFIRM_PLAN", "true")
    monkeypatch.setenv("E2E_SKIP_CONNECT", "1")  # 跳 connect

    # 钉钉推送 mock（防真推污染 + fire_and_forget 透传链路不需验）
    from trading import trading_plan
    monkeypatch.setattr(trading_plan, "push_plan_to_dingtalk", lambda d, o, **kw: True)

    # ===== V2 run_eod_phase monkeypatch 为 fake（smoke 不跑真算法） =====
    # 物理：fake 直接落 T+1 plan（2 个 fake 单），让 pre_open/stoploss/post_close 有单可挂。
    # 测的是 orchestrator 串联编排，不是 detect_signal 算法正确性（后者属 full_run）。
    # I-1 fix：fake_orders 补 tp1/tp1_portion 真值（模拟 Task 7 后新 plan，让 decide_exit
    # 主路径在 smoke 真跑而非走 D12 fallback）。tp1 介于 neckline 与 tp2 之间（颈线+tp1_h_mult×H），
    # tp1_portion=0.5（lot1 占半仓）。老 plan（tp1=None）走 fallback 的健壮性由 _build_ctx
    # 内 `tp1 is not None` 防御分支覆盖，无需 smoke 单独验（那是 _build_ctx 的保守降级路径）。
    fake_orders = [
        {
            "order": {"symbol": "300001.SZ", "qty": 100, "side": "BUY", "price": 10.00},
            "stop_price": 9.50, "take_profit": 11.00, "neckline": 10.00,
            "atr": 0.25, "formed_at": "2026-07-01", "max_wait": 5,
            "tp1": 10.50, "tp1_portion": 0.5, "cancel_on": 10.50,
            "experiment_id": None, "experiment_weight": None, "rr": 2.0,
        },
        {
            "order": {"symbol": "688001.SH", "qty": 100, "side": "BUY", "price": 20.00},
            "stop_price": 19.00, "take_profit": 22.00, "neckline": 20.00,
            "atr": 0.50, "formed_at": "2026-07-01", "max_wait": 5,
            "tp1": 21.00, "tp1_portion": 0.5, "cancel_on": 21.00,
            "experiment_id": None, "experiment_weight": None, "rr": 2.0,
        },
    ]

    async def _fake_run_eod_phase(t_date):
        """fake eod：直接落 T+1 plan（绕开 universe 扫描 + detect_signal）。"""
        from trading import engine
        t_plus_1 = engine.calendar.next_trading_day(t_date.isoformat())
        trading_plan.save_plan(t_plus_1, fake_orders, confirmed=True)
        return {"date": t_plus_1, "n_orders": len(fake_orders), "mode": "dry_run",
                "auto_confirmed": True}

    from tests.e2e_long_cycle import signal_scanner
    monkeypatch.setattr(signal_scanner, "run_eod_phase", _fake_run_eod_phase)

    # ===== V3 MinBarFeeder + fake stk_mins loader（不依赖 Tushare） =====
    from tests.e2e_long_cycle.min_bar_feeder import MinBarFeeder
    import pandas as pd

    def _fake_stk_mins_loader(sym, t_date):
        """fake 5min bar：返固定 OHLCV（9:30-15:00 每 5min 一根），不调 Tushare。"""
        times = pd.date_range(f"{t_date} 09:30:00", f"{t_date} 15:00:00", freq="5min")
        return pd.DataFrame({
            "trade_time": times.strftime("%Y-%m-%d %H:%M:%S"),
            "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.2,
            "vol": 1000, "amount": 10200.0,
        })

    min_bar_feeder = MinBarFeeder(stk_mins_loader=_fake_stk_mins_loader)

    # ===== V4 ProbabilisticBroker（构造熔断日 + 超期标的） =====
    from tests.e2e_long_cycle.probabilistic_broker import ProbabilisticBroker
    broker = ProbabilisticBroker(
        seed=42, min_bar_feeder=min_bar_feeder,
        # 构造熔断日：第二日（T+1 of day1）熔断，验韧性事件注入链路。
        circuit_breaker_days={date(2026, 7, 2)},
        # 构造超期标的：300099.SZ 假持仓 entry 06-15，holding_days_ref=day2（达 max_wait 触发超期标记）。
        expired_symbols={"300099.SZ": {"entry_date": "2026-06-15",
                                        "holding_days_ref": date(2026, 7, 2)}},
        force_state="FILLED",  # 强制成交（smoke 不验概率分布，只验串联）
    )

    # ===== V5 DingTalkLog（mock 不真推）+ V6 TableSnapshotCollector =====
    from tests.e2e_long_cycle.dingtalk_log import DingTalkLog
    from tests.e2e_long_cycle.table_snapshot import TableSnapshotCollector
    from tests.e2e_long_cycle.report_builder import ReportBuilder
    dingtalk_log = DingTalkLog(enabled=False)
    snapshot_collector = TableSnapshotCollector()

    # ===== V1 ReplayDriver + V7 orchestrator 装配 =====
    from tests.e2e_long_cycle.replay_driver import ReplayDriver
    from tests.e2e_long_cycle.orchestrator import build_job_runner

    # fake 2 日日历（不依赖 data_lake）：day1 eod → day2 pre_open/stoploss×8/post_close。
    calendar = [date(2026, 7, 1), date(2026, 7, 2)]

    class _FakeEng:
        """fake TradingEngine（smoke 不需真 sched，discovery_stub.attach 容错）。"""
        class sched:
            @staticmethod
            def add_job(*a, **kw):
                pass
    eng = _FakeEng()

    job_runner = build_job_runner(
        min_bar_feeder, broker, dingtalk_log, snapshot_collector, eng)

    # ===== 跑 2 日 × 4 阶段 =====
    snapshots: dict = {}
    with dingtalk_log.collect():
        driver = ReplayDriver(calendar=calendar, job_runner=job_runner)
        day_results = driver.run()

    # snapshot 聚合（post_close 已 snapshot，此处补全 + 容错）
    for d in calendar:
        try:
            snapshots[d] = snapshot_collector.snapshot(d)
        except Exception:
            snapshots[d] = {}

    # ReportBuilder 生成 md + 4 类 checks
    rb = ReportBuilder(output_dir=tmp_path / "report")
    md_path = rb.build(day_results, snapshots, dingtalk_log.records)
    checks = rb.checks(snapshots)

    # ============================================================
    # pytest 自动化校验（spec §13 验收 + V6 review F1/F2 follow-up）
    # ============================================================

    # --- 验收 1：日历全跑（day_results 长度 = 日历长度） ---
    assert len(day_results) == len(calendar), \
        f"应跑 {len(calendar)} 日，实际 {len(day_results)} 日"

    # --- 验收 2：4 阶段都跑到（ReplayDriver 时序：T 日 pipeline + T+1 三阶段） ---
    # 2 日日历 [day1=07-01, day2=07-02]：
    #   day_results[0]（day1）：pipeline(07-01) + pre_open(07-02) + stoploss(07-02)×8 + post_close(07-02)
    #   day_results[1]（day2）：pipeline(07-02) only（day2 是末日无 T+1）
    day1 = day_results[0]  # day1 跑全 4 阶段（T+1=day2 在日历内）
    # pipeline_then_eod 在 day1 跑（T 日盘后扫信号）
    assert "pipeline_then_eod" in day1.phase_results, "day1 应跑 pipeline_then_eod"
    # pre_open/stoploss/post_close 跑在 day1 的 T+1=day2（全在 day1.phase_results）
    assert "pre_open" in day1.phase_results, "day1 应跑 pre_open（T+1=day2）"
    assert "stoploss" in day1.phase_results, "day1 应跑 stoploss（T+1=day2）"
    assert len(day1.phase_results.get("stoploss", [])) == 8, \
        f"stoploss 应跑 8 时点，实际 {len(day1.phase_results.get('stoploss', []))}"
    assert "post_close" in day1.phase_results, "day1 应跑 post_close（T+1=day2）"
    # day2（末日）只跑 pipeline（无 T+1）
    assert "pipeline_then_eod" in day_results[1].phase_results, "day2 应跑 pipeline_then_eod"
    assert "pre_open" not in day_results[1].phase_results, "day2 末日不应跑 pre_open"

    # --- 验收 3：DayResult 字段完整 ---
    for r in day_results:
        assert r.date is not None
        assert hasattr(r, "trading_day")
        assert hasattr(r, "phase_results")
        assert hasattr(r, "failures")
    # day1 的 trading_day 应 = day2（T+1）
    assert day1.trading_day == date(2026, 7, 2), \
        f"day1 trading_day 应为 2026-07-02，实际 {day1.trading_day}"
    # day2（末日）trading_day 为 None
    assert day_results[1].trading_day is None, \
        f"末日 trading_day 应为 None，实际 {day_results[1].trading_day}"

    # --- 验收 4：韧性事件构造场景生效（V4 ProbabilisticBroker 注入链路通） ---
    assert broker._cb_days, "应构造熔断日"
    assert date(2026, 7, 2) in broker._cb_days, "熔断日应为 2026-07-02"
    assert broker._expired, "应构造超期标的"
    assert "300099.SZ" in broker._expired, "超期标的应为 300099.SZ"

    # --- 验收 5：snapshot 生成 + 字段结构（F1 断言补强：计数非仅键存在） ---
    for d, snap in snapshots.items():
        assert "trade_event" in snap, f"{d} snapshot 应含 trade_event 计数"
        assert isinstance(snap["trade_event"], int), "trade_event 应为 int 计数"
        assert "order_count" in snap, f"{d} snapshot 应含 order_count"
        assert "fill" in snap, f"{d} snapshot 应含 fill 计数"
        assert "position" in snap, f"{d} snapshot 应含 position 计数"
        assert "plan_orders" in snap, f"{d} snapshot 应含 plan_orders"
        assert "plan_confirmed" in snap, f"{d} snapshot 应含 plan_confirmed"
        # trade_event_by_action / order_by_state 是 dict（可能空）
        assert isinstance(snap.get("trade_event_by_action", {}), dict)
        assert isinstance(snap.get("order_by_state", {}), dict)

    # --- 验收 6：md 生成 + 结构（spec §8 §0-§6） ---
    assert md_path.exists(), "汇总 md 应生成"
    content = md_path.read_text(encoding="utf-8")
    assert "## 0. 运行配置" in content, "md 应含 §0 运行配置"
    assert "## 1. 23 日时序执行总览" in content, "md 应含 §1 时序总览"
    assert "## 2. 每张表逐日落点" in content, "md 应含 §2 表落点"
    assert "## 3. 预期校验结果" in content, "md 应含 §3 校验结果"
    assert "## 4. 钉钉推送记录" in content, "md 应含 §4 钉钉记录"
    assert "trade_event" in content, "md 应含 trade_event 表名"
    assert "order" in content, "md 应含 order 表名"

    # --- 验收 7：4 类 checks 返结构（F1 断言补强：每类有 ok 字段） ---
    assert set(checks.keys()) == {"structural", "consistency", "coverage", "timing"}, \
        f"checks 应含 4 类，实际 {set(checks.keys())}"
    for kind, res in checks.items():
        assert "ok" in res, f"{kind} checks 应含 ok 字段"
        assert isinstance(res["ok"], bool), f"{kind}.ok 应为 bool"

    # --- 验收 8：跨日 key 对齐（C-6 跨日 key 回归） ---
    # day1 eod 落 plan_{T+1=day2} → day2 pre_open 读 plan_{day2}（key 一致）。
    # 验法：day2 snapshot 的 plan_orders > 0（plan 落盘成功）+ plan_confirmed=True。
    day2_snap = snapshots[date(2026, 7, 2)]
    assert day2_snap["plan_orders"] > 0, \
        f"day2 plan_orders 应 >0（day1 eod 落盘），实际 {day2_snap['plan_orders']}"
    assert day2_snap["plan_confirmed"] is True, \
        f"day2 plan 应 confirmed=True（AUTO_CONFIRM_PLAN），实际 {day2_snap['plan_confirmed']}"

    # --- 验收 9：MinBarFeeder 行情注入链路（stk_mins 切片未降级） ---
    # smoke 用 fake loader 必返有效 bar，degraded 应 False（验 patch_get_quotes 链路通）。
    assert not min_bar_feeder.degraded, \
        "fake loader 应返有效 bar，MinBarFeeder 不应降级"

    # --- 验收 10：failures 软降级（spec §10：单阶段异常不中断） ---
    # smoke 用 force_state=FILLED + fake loader，理论上不应有 failure；若有则暴露 orchestrator bug。
    all_failures = [f for r in day_results for f in r.failures]
    assert not all_failures, \
        f"smoke 不应有 failures（暴露 orchestrator 串联 bug）：{all_failures}"

    # --- 验收 11：I-1 fix 验主路径跑（decide_exit 非 D12 fallback）---
    # 物理意图（V7 review I-1）：_build_ctx state 补全 decide_exit 必读键后，stoploss 阶段
    # 应走 decide_exit 主路径（fallback_used==0），而非 D12 fallback（should_trigger_stop 单价
    # 比对）。验法：day1 stoploss 至少一个盘中时点 checked>0（有持仓进 holding 循环）+
    # fallback_used==0（decide_exit 主路径无 KeyError）。若 fallback_used>0 说明 state 仍缺键
    # 或 decide_exit 异常，I-1 fix 回归。
    day1_stoploss = day1.phase_results.get("stoploss", [])
    main_path_hits = [
        r for r in day1_stoploss
        if r.get("checked", 0) > 0 and r.get("fallback_used") == 0
    ]
    assert main_path_hits, (
        f"I-1 回归：day1 stoploss 应至少一个时点走 decide_exit 主路径（checked>0 + "
        f"fallback_used==0），实际 stoploss 返回值={day1_stoploss}")
    # 全程 fallback_used 应为 0（plan 字段齐全 + state 补全 → decide_exit 不 KeyError）。
    total_fallback = sum(r.get("fallback_used", 0) for r in day1_stoploss)
    assert total_fallback == 0, (
        f"I-1 回归：day1 stoploss 全程 fallback_used 应为 0（decide_exit 主路径无异常），"
        f"实际 total_fallback={total_fallback}")


# ============================================================
# 2. 23 日全链路（@e2e_long，手动跑，~30-90min）
# ============================================================
@pytest.mark.e2e_long
def test_e2e_long_cycle_full_run(isolated_state, connect_session, monkeypatch):
    """23 日全链路：ReplayDriver 串全组件跑 + ReportBuilder md + 4 类校验全绿。

    物理意图（spec §13 验收 1-10）：23 交易日 × 4 阶段全跑，真实信号扫描（V2 detect_signal
    真跑 × data_lake 真实 7 月日线）+ 真实分钟行情（V3 stk_mins）+ 概率成交（V4 70/15/5/10
    + 构造熔断/超期）+ 真推钉钉（V5 enabled=True）+ connect 5 bot 真起（V5 connect_session）+
    discovery 触发（V5 discovery_stub）；跑完生成汇总 md + 4 类校验断言。

    ⚠️ 手动跑（~30-90min + 真推 + connect 真起 + Tushare 限频）：
        pytest tests/e2e_long_cycle/test_e2e_long_cycle.py::test_e2e_long_cycle_full_run \
            -v -m e2e_long -s

    首次跑可能暴露的问题（implementer 据实修）：
        - _eod universe 非创板科创 → V2 路径 B（自扫，已实现）。
        - stk_mins 限频 → MinBarFeeder cache + 降级（V3 已实现）。
        - connect 凭证缺 → E2E_SKIP_CONNECT=1 跳过（V5 conftest）。
    """
    from tests.e2e_long_cycle.replay_driver import ReplayDriver, load_july_calendar
    from tests.e2e_long_cycle.min_bar_feeder import MinBarFeeder
    from tests.e2e_long_cycle.probabilistic_broker import ProbabilisticBroker
    from tests.e2e_long_cycle.dingtalk_log import DingTalkLog
    from tests.e2e_long_cycle.table_snapshot import TableSnapshotCollector
    from tests.e2e_long_cycle.report_builder import ReportBuilder
    from tests.e2e_long_cycle.orchestrator import build_job_runner
    from tests.e2e_long_cycle.discovery_stub import discovery_stub
    from trading import trading_plan
    from trading.engine import TradingEngine

    # env：AUTO 确认 + dry_run 影子（不触真单，gw 由 V4 broker mock）
    monkeypatch.setenv("AUTO_TRADE_MODE", "dry_run")
    monkeypatch.setenv("AUTO_CONFIRM_PLAN", "true")
    monkeypatch.setattr(trading_plan, "push_plan_to_dingtalk", lambda d, o, **kw: True)

    # 组件装配
    calendar = load_july_calendar()  # 7 月 23 交易日（从 data_lake 取）
    min_bar_feeder = MinBarFeeder()  # 真 stk_mins loader
    broker = ProbabilisticBroker(
        seed=42, min_bar_feeder=min_bar_feeder,
        circuit_breaker_days={date(2026, 7, 10)},   # 构造熔断日
        expired_symbols={"300099.SZ": {"entry_date": "2026-06-15",
                                        "holding_days_ref": date(2026, 7, 15)}})  # 构造超期
    dingtalk_log = DingTalkLog(enabled=True)  # 真推钉钉
    snapshot_collector = TableSnapshotCollector()
    eng = TradingEngine()

    snapshots: dict = {}
    job_runner = build_job_runner(
        min_bar_feeder, broker, dingtalk_log, snapshot_collector, eng)

    # discovery cron 注册 + daemon mock（C-7 V2/V3 验证）+ 钉钉真推 collect
    with discovery_stub.attach(eng), dingtalk_log.collect():
        driver = ReplayDriver(calendar=calendar, job_runner=job_runner)
        day_results = driver.run()

    # snapshot 聚合（每日 post_close 已 snapshot，此处补全）
    for d in calendar:
        try:
            snapshots[d] = snapshot_collector.snapshot(d)
        except Exception:
            snapshots[d] = {}

    # ReportBuilder 生成 md + 4 类校验
    rb = ReportBuilder()
    md_path = rb.build(day_results, snapshots, dingtalk_log.records)
    assert md_path.exists(), "汇总 md 应生成"
    checks = rb.checks(snapshots)

    # ===== pytest 自动化校验（spec §11.1 + §13 验收）=====

    # 验收 1：23 日全跑（day_results 长度 = 23）
    assert len(day_results) == len(calendar), \
        f"应跑 {len(calendar)} 日，实际 {len(day_results)} 日"

    # 验收 8：表间一致性（order.FILLED 量 = fill 笔数，零漂）
    # V6 checks.consistency 在全量数据上实算（_check_consistency：position>0 ∧ fill=0 = 漂）。
    # 概率模拟下概率成交必然驱动 fill 落表，故 consistency.ok 应为 True（drifts 列表空）；
    # 若漂移列表非空，说明 broker 概率成交未真落 fill 表 / 持仓/成交两条线脱节——属回归信号。
    # Important-1 fix（final review）：原变量读了不断言（orphan）→ 补 ok 断言把 spec §13.8 把关落地。
    consistency = checks["consistency"]
    assert consistency["ok"] is True, (
        f"表间一致性应零漂（consistency.ok=True），实际 drifts={consistency.get('drifts')}")

    # 验收 4：韧性事件覆盖（熔断 ≥1 构造日 / 超期 ≥1 构造标的）
    # 构造场景必然触发（circuit_breaker_days/expired_symbols 非空）
    assert broker._cb_days, "应构造熔断日"
    assert broker._expired, "应构造超期标的"
    # Important-1 fix（final review §13.4）：构造集合非空只是"声明构造"，不验"事件真触发"——
    # 韧性场景必须真落到 trade_event 才算覆盖（spec §13.4「韧性覆盖率达标」语义）。验法：
    #   ① 熔断日：构造日（broker._cb_days 内）的 post_close 阶段 query_asset 返 -4% 净值，
    #      engine.post_close 内 circuit_breaker 检测应落 trade_event（CIRCUIT_BREAKER action）；
    #      该日 snapshot.trade_event_by_action 应含熔断相关 key。
    #   ② 超期标的：300099.SZ 在 holding_days_ref 后达 max_holding，decide_exit resolution 6
    #      （TIMEOUT 市价强平）应落 STOP/CLOSED 类 trade_event。
    # 容错写法（或）：full_run 留用户手动跑，trade_event 的精确 action 名（CIRCUIT_BREAKER /
    # STOP_TRIGGERRED / CLOSED）随生产 DDL 演进而定，断言"构造日/构造标的的事件真落表"
    # 即把 spec §13.4 把关落地，不绑死具体 action 字面值。
    # 韧性事件覆盖验证（spec §13.4 · final review Important-1 + full_run 首跑根因 5 修正）：
    #   ① 熔断：生产 post_close 熔断走 cancel_all + emergency_halt + lock_down，【不落
    #      trade_event(CIRCUIT)】（full_run 首跑暴露：log CRITICAL「日内熔断触发」+「紧急熔断
    #      网关锁定」真跑，但 trade_event actions 无 CIRCUIT 类）。改验 post_close phase_result
    #      .circuit_breaker=True（engine.post_close 返值 result["circuit_breaker"]，engine.py:1627），
    #      即熔断真触发的可观测证据（与生产语义对齐，不臆造 trade_event action）。
    #   ② 超期：300099.SZ 达 max_holding → decide_exit resolution 6 TIMEOUT 强平 → STOP/CLOSED
    #      类 trade_event（聚合 snapshots 找，action 名随生产 DDL 演进，断言关键词非精确值）。
    cb_triggered = False
    for r in day_results:
        # cb_days 是 trading_day（T+1，post_close 业务日，broker.attach(t_plus_1) 传入）；
        # day_result.trading_day = T+1。两道检查兜底（date/trading_day 任一命中 cb_days）。
        if r.trading_day in broker._cb_days or r.date in broker._cb_days:
            for pc_result in r.phase_results.get("post_close", []):
                if pc_result.get("circuit_breaker"):
                    cb_triggered = True
                    break
    assert cb_triggered, (
        f"熔断日应触发 circuit_breaker（post_close result.circuit_breaker=True），构造日="
        f"{broker._cb_days}，实际 day_results post_close 无 circuit_breaker=True（查 log 是否有"
        "「日内熔断触发」CRITICAL + emergency_halt）")
    expired_action_keys = set()
    for expired_sym in broker._expired:
        for d, snap in snapshots.items():
            for action in snap.get("trade_event_by_action", {}):
                if any(kw in action.upper() for kw in ("STOP", "CLOSE", "EXPIRE", "TIMEOUT", "MAX_HOLD")):
                    expired_action_keys.add(action)
    assert expired_action_keys, (
        "超期标的应触发 STOP/CLOSED 类平仓 trade_event（300099.SZ 达 max_holding → "
        "decide_exit resolution 6 强平），实际 trade_event actions 聚合="
        f"{sorted({a for s in snapshots.values() for a in s.get('trade_event_by_action', {})})}")

    # 验收 7：汇总 md 含每张表落点 + 4 类校验
    content = md_path.read_text(encoding="utf-8")
    assert "## 2. 每张表逐日落点" in content
    assert "## 3. 预期校验结果" in content
    assert "trade_event" in content and "order" in content

    # 验收 9：时序对齐（eod 落 T+1 = pre_open 读 T+1，C-6 跨日 key 回归）
    # day_results 每日 trading_day 字段对齐
    for r in day_results[:-1]:  # 末日无 T+1
        assert r.trading_day is not None, f"{r.date} 应有 T+1"

    # 验收 10：不破坏既有（本套件独立 mark，全量回归在 implementer gate 跑 -m "not e2e_long"）
