# -*- coding: utf-8 -*-
"""V7 全链路 job_runner：串 signal_scanner/MinBarFeeder/ProbabilisticBroker/discovery_stub
+ engine 真身（spec §5 每日时序编排）。

物理意图（spec §5）：ReplayDriver 每日每阶段 freeze clock 后调本 runner，runner 据阶段
dispatch 到对应真身 + 组件：
    - pipeline_then_eod（T 日 19:00）：V2 run_eod_phase —— 自扫创板科创 universe ×
      detect_signal 真跑 × eod_plan 真身落 T+1 plan（AUTO_CONFIRM_PLAN=true 自动确认）。
    - pre_open（T+1 09:25）：V4 broker.attach（patch get_gateway/_submit/_cancel_all）+
      V2 run_pre_open_phase —— 读 T+1 已确认 plan → 撤昨日遗留单 → 注入白名单 → 逐单挂单。
    - stoploss（T+1 盘中 N 时点）：V3 MinBarFeeder.set_context + patch_get_quotes 注入
      stk_mins 时点行情 + V4 broker.attach + engine.stop_loss_monitor 真身 —— decide_exit
      主路径判定止损/止盈/cancel_on（pending 期撤单）。
    - post_close（T+1 15:30）：V4 broker.attach + V2 run_post_close_phase —— 对账 +
      trailing 演进 + max_holding 标记 + 清动态白名单；V6 snapshot 采集当日每表落点。

接口对齐（V1-V6 实测签名 + engine 真身）：
    - V2 run_eod_phase(t_date) / run_pre_open_phase(t_plus_1, gw) / run_post_close_phase(
      t_plus_1, gw)：gw 参数实际不被消费（V2 直调模块级 engine.pre_open/post_close，
      内部走 get_gateway()），故 orchestrator 传 gw=None 让真身走 broker.attach patched
      的 get_gateway（单一 patch 口子，避免双 gw 注入冲突）。
    - V4 broker.attach(t_date, up_to)：patch engine.get_gateway/_submit/_cancel_all_open_orders
      + 注入 query_asset（熔断日构造）/ _fetch_broker_positions（超期标的构造）。生命周期
      = 单阶段 contextmanager，每阶段进入/退出。
    - V3 MinBarFeeder.set_context(syms, t_date, up_to) + patch_get_quotes()：set_context
      覆盖当前上下文（syms + t_date + up_to），patch_get_quotes patch trading.qmt_market_data.
      get_quotes 返 feed 结果（_stoploss 内 `quotes = await get_quotes(syms)` 命中）。
    - engine.stop_loss_monitor(stop_prices, *, gw, monitor_ctx, pending_ctx)：主路径走
      monitor_ctx（decide_exit 契约），stop_prices 仅 D12 fallback 兜底。
    - V6 snapshot_collector.snapshot(t_date)：读 state_store 6 表 + plan/review 当日落点。

关键风险防御（CLAUDE.md 边界审查）：
    - 跨日 MinBarFeeder 上下文污染：set_context 每盘中时点重设（不跨日复用旧 ctx）；
      degraded 标记跨日累积（V3 设计如此，反映全周期行情源健康度）。
    - gw 双注入冲突：orchestrator 不直接传 gw 给 V2/stop_loss_monitor（传 None 让真身
      走 patched get_gateway），broker.attach 是唯一 gw 注入点。
    - monitor_ctx 缺 stop：plan order 无 stop_price（异常）→ state.stop=None →
      stop_prices 该 sym=None → stop_loss_monitor 内部 decide_exit 用 state.stop，
      stop_prices 仅 fallback；None 安全（decide_exit 自身防 None）。
"""
from __future__ import annotations

import asyncio
from datetime import date, time
from typing import Any, Callable

from trading import clock
from trading.calendar import next_trading_day


def build_job_runner(
    min_bar_feeder: Any,
    broker: Any,
    dingtalk_log: Any,
    snapshot_collector: Any,
    eng: Any,
) -> Callable[[date, str], dict]:
    """构造 ReplayDriver 的 job_runner（闭包持有 V1-V6 组件 + engine 实例）。

    Args:
        min_bar_feeder: V3 MinBarFeeder（stoploss 阶段注入 stk_mins 行情）。
        broker: V4 ProbabilisticBroker（pre_open/stoploss/post_close 阶段 patch gw 链路）。
        dingtalk_log: V5 DingTalkLog（已 collect() context，本 runner 不再进 context，
            仅透传引用供 ReportBuilder §4；真推/记录由 V5 collect context 全程接管）。
        snapshot_collector: V6 TableSnapshotCollector（post_close 后 snapshot 当日落点）。
        eng: TradingEngine 实例（discovery_stub.attach 注册 cron 用 eng.sched）。

    Returns:
        job_runner(t_date, phase) -> dict：ReplayDriver 每日每阶段回调。
    """

    def job_runner(t_date: date, phase: str) -> dict:
        # T+1 业务日（pre_open/stoploss/post_close 用）；next_trading_day 返 ISO 字符串。
        t_plus_1_iso = next_trading_day(t_date.isoformat())
        t_plus_1 = date.fromisoformat(t_plus_1_iso)
        # clock 已被 ReplayDriver freeze 到阶段时点（C-6 单一口子），取 .time() 作行情切片上界。
        now_time = clock.now().time()

        # ============================================================
        # ① pipeline_then_eod：T 日盘后扫信号落 T+1 plan（V2 真身）
        # ============================================================
        if phase == "pipeline_then_eod":
            from tests.e2e_long_cycle import signal_scanner
            # V2 run_eod_phase：自扫 universe × detect_signal 真跑 × eod_plan 落盘。
            # 不需 broker/feeder（无挂单/行情），直接 await（async fn → asyncio.run 同步化）。
            return asyncio.run(signal_scanner.run_eod_phase(t_date))

        # ============================================================
        # ② pre_open：T+1 09:25 broker.attach + V2 挂 T+1 单
        # ============================================================
        if phase == "pre_open":
            from tests.e2e_long_cycle import signal_scanner
            # broker.attach patch engine.get_gateway/_submit/_cancel_all（单一 gw 注入点）。
            # V2 run_pre_open_phase 直调 engine.pre_open(today=T+1)，内部 get_gateway() 命中 patch。
            # gw=None 传参：V2 不消费 gw（直调模块级），让真身走 patched get_gateway（避免双 gw）。
            with broker.attach(t_plus_1, time(9, 25)):
                return asyncio.run(signal_scanner.run_pre_open_phase(t_plus_1, gw=None))

        # ============================================================
        # ③ stoploss：T+1 盘中 MinBarFeeder 注入行情 + stop_loss_monitor 真身判定
        # ============================================================
        if phase == "stoploss":
            from trading import position_book
            from trading.engine import stop_loss_monitor
            from trading import trading_plan

            # 读当前持仓（broker 模拟的 gw._fetch_broker_positions 已写回内存 +
            # post_close 对账落 position_book；stoploss 阶段读 position_book 真身）。
            # get_local_positions() -> {sym: qty}，无持仓则跳过（盘中无敞口无需巡检）。
            positions = position_book.get_local_positions()
            syms = list(positions.keys())
            if not syms:
                return {"checked": 0, "reason": "无持仓，跳过止损监控"}

            # MinBarFeeder 上下文设置：覆盖当前 syms + t_date + 时点（每盘中时点重设，
            # 防跨时点/跨日复用旧 ctx 污染行情切片）。patch_get_quotes 在 with 内生效。
            min_bar_feeder.set_context(syms, t_plus_1, now_time)

            # 从 T+1 plan 读 stop/tp/atr/cancel_on 构造 monitor_ctx（decide_exit 输入）+
            # pending_ctx（cancel_on 撤单阈值）。plan 含全量 orders（含未成交 pending）。
            plan = trading_plan.load_plan(t_plus_1_iso)
            monitor_ctx, pending_ctx = _build_ctx(plan, positions)

            # 双 context 叠加：行情 patch + gw patch（顺序无依赖，但 gw patch 内
            # stop_loss_monitor 会 await get_quotes，行情 patch 必须在内层先就位）。
            with min_bar_feeder.patch_get_quotes(), broker.attach(t_plus_1, now_time):
                return asyncio.run(stop_loss_monitor(
                    # stop_prices：D12 fallback 兜底（decide_exit 异常时退回 should_trigger_stop
                    # 用此比价）。从 monitor_ctx.state.stop 提取；None 安全（fallback 跳过该 sym）。
                    stop_prices={
                        s: (monitor_ctx.get(s, {}).get("state") or {}).get("stop")
                        for s in syms
                    },
                    gw=None,  # 让真身走 patched get_gateway（broker.attach 注入）
                    monitor_ctx=monitor_ctx,
                    pending_ctx=pending_ctx,
                ))

        # ============================================================
        # ④ post_close：T+1 15:30 broker.attach + V2 对账落表 + V6 snapshot
        # ============================================================
        if phase == "post_close":
            from tests.e2e_long_cycle import signal_scanner
            # broker.attach 注入 query_asset（熔断日构造 -4%）/ _fetch_broker_positions（超期标的）。
            # V2 run_post_close_phase 直调 engine.post_close(date=T+1, gw, local_positions)，
            # 内部对账 + trailing 演进 + max_holding 标记 + 清白名单 + 落 account_daily。
            with broker.attach(t_plus_1, time(15, 30)):
                result = asyncio.run(signal_scanner.run_post_close_phase(t_plus_1, gw=None))

            # V6 snapshot 当日每表落点（post_close 已落表，此刻读真相）。
            # 容错：snapshot 内部 sqlite3 直查，表缺失/DB 未 init 返 0 不抛（首日健壮）。
            try:
                snapshot_collector.snapshot(t_plus_1)
            except Exception:
                # snapshot 失败不应中断回放（生产同源软降级）；ReportBuilder §3 会暴露缺失。
                pass
            return result

        # 未识别阶段（防御性）：返 skipped 不抛（ReplayDriver 异常会记 failures）。
        return {"phase": phase, "skipped": True}

    return job_runner


def _build_ctx(plan: dict | None, positions: dict) -> tuple[dict, dict]:
    """从 plan orders 构造 monitor_ctx + pending_ctx（decide_exit / pending 撤单输入）。

    物理意图（spec §5 stoploss 阶段 + D11/D12）：
        - monitor_ctx：{sym: {"state": {...}, "cfg": {...}}}，对齐 decide_exit 契约
          （execution.py:131-201）+ simulate_exit cfg（backtest.py:177-183）。
            - state.stop / state.tp：止损/止盈价（plan.stop_price / plan.take_profit）。
            - state.entry：颈线（形态基准 c*，plan.neckline）。
            - state.phase："holding"（已成交持仓）/ "pending"（挂单未成交）。
        - pending_ctx：{sym: cancel_on}，D11 pending 期撤单阈值（plan.cancel_on）。
          仅 pending 标的（sym 不在 positions）入 pending_ctx。

    Args:
        plan: trading_plan.load_plan(T+1) 返回值；None（无 plan）/{"orders":[...]}。
        positions: position_book.get_local_positions() -> {sym: qty}（当前持仓）。

    Returns:
        (monitor_ctx, pending_ctx) 二元组。
    """
    monitor_ctx: dict[str, dict] = {}
    pending_ctx: dict[str, float] = {}
    for o in (plan or {}).get("orders", []):
        sym = (o.get("order") or {}).get("symbol")
        if not sym:
            continue  # 异常 order（无 symbol）跳过，不污染 ctx
        monitor_ctx[sym] = {
            "state": {
                "stop": o.get("stop_price"),
                "tp": o.get("take_profit"),
                "entry": o.get("neckline"),
                # 持仓判定：sym 在 positions（qty>0）= holding；否则 pending（挂单未成交）。
                "phase": "holding" if sym in positions else "pending",
            },
            "cfg": _cfg_from_plan(o),
        }
        # pending_ctx：仅 pending 标的 + plan 显式提供 cancel_on（None/缺失不入，放飞不撤）。
        if o.get("cancel_on") is not None and sym not in positions:
            pending_ctx[sym] = o["cancel_on"]
    return monitor_ctx, pending_ctx


def _cfg_from_plan(o: dict) -> dict:
    """从 plan order 提 decide_exit cfg（对齐 simulate_exit cfg 键 + engine._trade_cfg 默认）。

    物理意图：decide_exit 是 Task 4 执行单源纯函数（回测 simulate_exit 等价已证），
    cfg 键必须覆盖 decide_exit 全部读取位（tp_h_mult/stop_atr_mult/max_wait/tp1_*）。
    缺键会致 decide_exit KeyError 退 fallback（D12 should_trigger_stop），丧失 tp1 分级
    止盈 + cancel_on 撤单能力——故 cfg 必须完整。

    Why 从 _trade_cfg() 取默认而非硬编码 2.0：与实盘 _trade_cfg() 单源对齐（env
    TRADE_TP_H_MULT 等可调），避免 E2E 与实盘 cfg 漂移致"测了 A 实盘跑 B"。
    plan.max_wait 透传（每标的窗口可能不同，由 signal formed_at 决定）。
    """
    from trading.engine import _trade_cfg
    cfg = _trade_cfg()  # 真身默认（env 可调，与实盘同源）
    return {
        "tp_h_mult": cfg["tp_h_mult"],
        "stop_atr_mult": cfg["stop_atr_mult"],
        "max_wait": o.get("max_wait", cfg["max_wait"]),  # plan 优先，缺省回退 cfg
        "tp1_h_mult": cfg.get("tp1_h_mult"),
        "tp1_portion": cfg.get("tp1_portion"),
    }
