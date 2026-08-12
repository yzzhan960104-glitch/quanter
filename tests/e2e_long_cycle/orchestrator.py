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

I-1 fix（V7 review Important）：_build_ctx 的 monitor_ctx state 必须补全 decide_exit
    holding 期必读键全集（neckline/atr/holding_days/is_last/lot1_open/lot2_open/tp1/tp2），
    否则 decide_exit 读 state["neckline"] 立即 KeyError → stop_loss_monitor 行 1078-1086
    try-except 捕获走 D12 fallback（should_trigger_stop 单价比对），丧失 tp1 分级止盈 +
    trailing 演进 + cancel_on 主路径判定。state 构造对齐 engine._stoploss 真身
    （engine.py:2581-2593）+ decide_exit 契约（execution.py:142-153）；老 plan 缺字段
    （neckline/atr/tp1/tp2 任一 None）→ 只塞最小 state 走 fallback（保守，与真身同款防御）。

关键风险防御（CLAUDE.md 边界审查）：
    - 跨日 MinBarFeeder 上下文污染：set_context 每盘中时点重设（不跨日复用旧 ctx）；
      degraded 标记跨日累积（V3 设计如此，反映全周期行情源健康度）。
    - gw 双注入冲突：orchestrator 不直接传 gw 给 V2/stop_loss_monitor（传 None 让真身
      走 patched get_gateway），broker.attach 是唯一 gw 注入点。
    - monitor_ctx state 缺 decide_exit 必读键（I-1）：plan 缺 neckline/atr/tp1/tp2 →
      只塞最小 state（stop 观测用），decide_exit 主路径 KeyError 走 D12 fallback
      （should_trigger_stop + stop_prices），盘中不裸奔。plan 字段齐全 → decide_exit
      主路径真跑（tp1 分级止盈 + trailing 演进 + cancel_on 主路径判定）。
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
            result = asyncio.run(signal_scanner.run_eod_phase(t_date))

            # ── data_ready 注入（full_run 集成修复 · 根因 1）──
            # 物理意图：生产 pipeline_then_eod（trading/orchestrate/pipeline.py）在跑完
            # 采集校验后会 upsert_data_ready(today, daily, ok=1) 落库，供 T+1 pre_open gate
            # ③ 段（engine.pre_open → _pre_open_gate → state_store.get_data_ready）双检放行
            # （C-2 链路，参考 tests/trading/test_e2e_trading_flow.py::
            # test_e2e_pipeline_then_eod_to_pre_open_gate_all_green）。
            #
            # E2E orchestrator 的 pipeline_then_eod 只调了 _eod 落 plan，【没写 data_ready】
            # → T+1 pre_open gate ③ 读 get_data_ready(T+1, daily) 命中 None → 拦截早返：
            #     "pre_open gate 未通过：数据 daily 未就绪（未采集），跳过挂单"
            # → 全周期无单可挂 → fill 表空 → 表间一致性 drift（position>0 ∧ fill=0）。
            #
            # 修法：在 _eod 落 plan 后补写 data_ready(T+1, daily, ok=1)。注意 key 是 T+1
            # （pre_open 读 T+1 的 data_ready，与生产 pipeline_then_eod 在 T 日落 T+1 key
            # 的口径对齐——pre_open(date=T+1) 内部 get_data_ready(T+1, daily)）。
            # 软降级：upsert 失败不应中断回放（生产同源 try-except 不阻断）。
            from trading import state_store
            try:
                # 生产 pipeline_then_eod 在 T 日盘后落 data_ready(T)（pipeline.py:103 口径）；
                # pre_open(T+1) gate③ 查 expected_latest_trade_day(now)=T（engine.py:2110
                # b77b2df7 修复后）。故 E2E 注入 key = T 日（旧注释写 T+1 已误导修正）。
                state_store.upsert_data_ready(
                    t_date.isoformat(), "daily",
                    ok=True, melted=False,
                    latest_date=t_date.isoformat(),     # 采集到的最新交易日 = T 日
                    expected_date=t_date.isoformat(),   # 期望最新日 = T（gate③ 查询口径）
                    message="E2E orchestrator mock 采集完成（pipeline_then_eod 注入）",
                )
            except Exception:
                # data_ready 落库失败不阻断回放（生产 pipeline.py:103 同款软降级）；
                # 失败会在 pre_open gate ③ 段表现为"未采集"拦截，由测试断言暴露。
                pass
            # ── pipeline 台账 done 注入（W5 get_ready 单口 · 2026-08-05 e2e smoke 修复）──
            # 物理意图：生产 pipeline_then_eod（trading/orchestrate/pipeline.py:55）成功后
            # 会 job_ledger.finish_run("pipeline", today, "done")；W5 get_ready 要求
            # data_ready 全绿 AND job_ledger.pipeline done 双绿才放行 pre_open。E2E
            # orchestrator 之前只注入 data_ready 未写台账 → pre_open gate 拦截 → 全周期
            # 无单可挂 → fill 空（test_orchestrator_smoke 断言失败）。此处与生产同口径补写。
            from trading import job_ledger
            try:
                # begin_run→finish_run 成对（finish_run 是 UPDATE，无 running 行则 no-op；
                # 与生产 pipeline_then_eod 的 begin/finish 配对语义一致）。
                job_ledger.begin_run("pipeline", t_date.isoformat(),
                                     clock.now().isoformat())
                job_ledger.finish_run("pipeline", t_date.isoformat(), "done",
                                      "E2E orchestrator mock pipeline 完成")
            except Exception:
                # 台账写失败不阻断回放（与生产软降级同语义）；pre_open gate 会据此拦截并
                # 由测试断言暴露（fail-closed，不静默）。
                pass
            return result

        # ============================================================
        # ② pre_open：T+1 09:25 broker.attach + V2 挂 T+1 单
        # ============================================================
        if phase == "pre_open":
            from tests.e2e_long_cycle import signal_scanner
            # broker.attach patch engine.get_gateway/_submit/_cancel_all（单一 gw 注入点）。
            # V2 run_pre_open_phase 直调 engine.pre_open(today=T+1)，内部 get_gateway() 命中 patch。
            # gw=None 传参：V2 不消费 gw（直调模块级），让真身走 patched get_gateway（避免双 gw）。
            with broker.attach(t_plus_1, time(9, 25)) as gw:
                eng._gw = gw  # _order_direction 内存兜底（_handle_order_update 方向反查）
                result = asyncio.run(signal_scanner.run_pre_open_phase(t_plus_1, gw=None))
                # design §4.2：挂单后注入成交回报（BUY fill 落账 + _place_take_profit 挂 TP 限价单）
                asyncio.run(broker.inject_fills(eng))
            return result

        # ============================================================
        # ③ stoploss：T+1 盘中 MinBarFeeder 注入行情 + stop_loss_monitor 真身判定
        # ============================================================
        if phase == "stoploss":
            from trading.engine import stop_loss_monitor
            from trading import trading_plan

            # 读当前持仓：从 broker 内存读（simulate_fetch_positions，与 stop_loss_monitor 真身
            # gw._fetch_broker_positions 同源），而非 position_book。Why broker 而非 position_book：
            #   - 数据源一致性（M-3 fix · I-1 配套）：stop_loss_monitor 真身内部用
            #     gw._fetch_broker_positions() 做实际巡检标的，orchestrator 用同源数据判
            #     holding/pending 才不致错位（真身读 broker 有 sym 进 holding 循环，orchestrator
            #     读 position_book 空 → 跳过 → smoke 永不验 decide_exit 主路径）。
            #   - E2E force_state=FILLED 写 broker 内存不写 position_book DB（apply_fill 链路
            #     依赖 on_stock_trade 回调，smoke 不模拟），position_book 恒空 → 老 logic 永跳过。
            # broker.simulate_fetch_positions 返 {sym: {volume, avg_price, entry_date?}}，
            # 取 volume>0 作持仓（与真身 engine.py:1046 qty 取 volume 同款）。
            broker_positions = broker.simulate_fetch_positions(t_plus_1)
            positions = {s: pos.get("volume", 0) for s, pos in broker_positions.items()
                         if (pos.get("volume", 0) or 0) > 0}
            syms = list(positions.keys())
            if not syms:
                return {"checked": 0, "reason": "无持仓，跳过止损监控"}

            # MinBarFeeder 上下文设置：覆盖当前 syms + t_date + 时点（每盘中时点重设，
            # 防跨时点/跨日复用旧 ctx 污染行情切片）。patch_get_quotes 在 with 内生效。
            min_bar_feeder.set_context(syms, t_plus_1, now_time)

            # 从 T+1 plan 读 stop/tp/atr/cancel_on 构造 monitor_ctx（decide_exit 输入）+
            # pending_ctx（cancel_on 撤单阈值）。plan 含全量 orders（含未成交 pending）。
            plan = trading_plan.load_plan(t_plus_1_iso)
            # I-1 fix：monitor_ctx state 必须补全 decide_exit 必读键（neckline/atr/holding_days/
            # is_last/lot1_open/lot2_open/tp1/tp2），否则 decide_exit 读 state["neckline"] 立即
            # KeyError → stop_loss_monitor 行 1078-1086 try-except 捕获走 D12 fallback
            # （should_trigger_stop 单价比对），丧失 tp1 分级止盈 + trailing 演进 + cancel_on
            # 主路径判定能力。holding_days 从 broker 持仓的 entry_date 算（与 engine._stoploss
            # 真身 engine.py:2572-2575 同源，entry_date 缺 → 0 向后兼容）。
            entry_dates = {
                s: pos["entry_date"] for s, pos in broker_positions.items()
                if pos.get("entry_date")
            }
            monitor_ctx, pending_ctx = _build_ctx(plan, positions, entry_dates, t_plus_1_iso)

            # 双 context 叠加：行情 patch + gw patch（顺序无依赖，但 gw patch 内
            # stop_loss_monitor 会 await get_quotes，行情 patch 必须在内层先就位）。
            with min_bar_feeder.patch_get_quotes(), broker.attach(t_plus_1, now_time) as gw:
                eng._gw = gw  # _order_direction 内存兜底（_handle_order_update 方向反查）
                result = asyncio.run(stop_loss_monitor(
                    # stop_prices：D12 fallback 兜底（decide_exit 异常时退回 should_trigger_stop
                    # 用此比价）。从 monitor_ctx.state.stop 提取；None 安全（fallback 跳过该 sym）。
                    stop_prices={
                        s: (monitor_ctx.get(s, {}).get("state") or {}).get("stop")
                        for s in syms
                    },
                    gw=None,  # 让真身走 patched get_gateway（broker.attach 注入）
                    monitor_ctx=monitor_ctx,
                    pending_ctx=pending_ctx,
                    # M-1 fix：显式传 eng._ports——保 e2e blackout 节流语义完整（不传则
                    # ports=None 守卫跳过 blackout 分支，e2e 长周期回测的行情黑屏 30min
                    # 节流行为将不参与验证；eng._ports 由 TradingEngine 构造时装配默认
                    # QuoteBlackoutThrottle，与生产 _stoploss 路径同源）。
                    ports=eng._ports,
                ))
                # design §4.2：盘中扫描 TP 限价单（stk_mins 累积 high>=tp 真实价格触发）
                # + 注入 STOP/TP 成交回报（fill/position/order 状态推进）
                asyncio.run(broker.scan_resting_and_inject(eng, t_plus_1, now_time))
            return result

        # ============================================================
        # ④ post_close：T+1 15:30 broker.attach + V2 对账落表 + V6 snapshot
        # ============================================================
        if phase == "post_close":
            from tests.e2e_long_cycle import signal_scanner
            # broker.attach 注入 query_asset（熔断日构造 -4%）/ _fetch_broker_positions（超期标的）。
            # V2 run_post_close_phase 直调 engine.post_close(date=T+1, gw, local_positions)，
            # 内部对账 + trailing 演进 + max_holding 标记 + 清白名单 + 落 account_daily。
            with broker.attach(t_plus_1, time(15, 30)) as gw:
                eng._gw = gw  # _order_direction 内存兜底（_handle_order_update 方向反查）
                result = asyncio.run(signal_scanner.run_post_close_phase(t_plus_1, gw=None))
                # design §4.2：post_close 内超期平仓等卖单的成交回报落账
                asyncio.run(broker.inject_fills(eng))

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


def _build_ctx(plan: dict | None, positions: dict,
               entry_dates: dict | None = None, today_iso: str | None = None
               ) -> tuple[dict, dict]:
    """从 plan orders 构造 monitor_ctx + pending_ctx（decide_exit / pending 撤单输入）。

    物理意图（spec §5 stoploss 阶段 + D11/D12 · I-1 fix）：
        - monitor_ctx：{sym: {"state": {...}, "cfg": {...}}}，对齐 decide_exit 契约
          （execution.py:131-201）+ simulate_exit cfg（backtest.py:177-183）+ 实盘 _stoploss
          真身范式（engine.py:2581-2593）。state 必读键全集：
            - phase："holding"（持仓巡检）/ "pending"（挂单未成交期，decide_exit pending 分支）。
            - entry：成交价（holding 期 decide_exit 实际不读 entry，pnl 在调用方算；这里透传
              plan.order.price 作语义锚，与真身 entry=None 等价安全，decide_exit 不 KeyError）。
            - stop：当日固定止损价（plan.stop_price，D12 fallback 观测用；decide_exit holding
              分支不读 state.stop，自己调 compute_stop_price 重算 trailing stop）。
            - tp1 / tp2：分级止盈价（plan.tp1 / plan.take_profit；decide_exit priority 2/3 直读
              state["tp2"]/state["tp1"]，必读键，缺即 KeyError 走 fallback）。
            - neckline / atr：trailing 基准 + 波动（plan.neckline / plan.atr；compute_stop_price
              必读，缺即 KeyError 走 fallback）。
            - holding_days：持有交易日数（从 position_book.entry_date + trading_days_between 算，
              与 engine._stoploss 真身 engine.py:2572-2575 同源；entry_date 缺失 → 0 向后兼容）。
            - is_last：是否持有期最后一根（holding_days >= max_holding，resolution 6 超期即末根）。
            - lot1_open / lot2_open：分级止盈腿状态（默认 True；实盘 monitor 不维护 lot 翻转，
              _place_take_profit 限价单成交翻 lot，对齐 simulate_exit 单根无状态语义）。
        - pending_ctx：{sym: cancel_on}，D11 pending 期撤单阈值（plan.cancel_on）。
          仅 plan 显式提供 cancel_on 的标的入 pending_ctx（None/缺失不入，放飞不撤）。

    Args:
        plan: trading_plan.load_plan(T+1) 返回值；None（无 plan）/{"orders":[...]}。
        positions: position_book.get_local_positions() -> {sym: qty}（当前持仓，判 holding/pending）。
        entry_dates: position_book.get_entry_dates() -> {sym: entry_date_iso}（算 holding_days 用；
            None/缺省 → holding_days 全 0，decide_exit 主路径仍正常跑）。
        today_iso: holding_days 的 end 日期（ISO 字符串）；None/缺省 → 用 clock.today()。

    Returns:
        (monitor_ctx, pending_ctx) 二元组。
    """
    from trading.compute.stop import trading_days_between
    from trading import clock as _clock
    # holding_days 的 end 日期：显式传入优先（job_runner 已 freeze 到 t_plus_1），否则 clock.today。
    _today = today_iso if today_iso else _clock.today()
    # max_holding 从 _trade_cfg() 取（与实盘 _stoploss engine.py:2543 同源，env 可调）。
    from trading.engine import _trade_cfg
    max_holding = _trade_cfg().get("max_holding", 15)

    monitor_ctx: dict[str, dict] = {}
    pending_ctx: dict[str, float] = {}
    for o in (plan or {}).get("orders", []):
        sym = (o.get("order") or {}).get("symbol")
        if not sym:
            continue  # 异常 order（无 symbol）跳过，不污染 ctx

        # 持仓判定：sym 在 positions（qty>0）= holding；否则 pending（挂单未成交）。
        phase = "holding" if sym in positions else "pending"

        # ── I-1 fix：补全 decide_exit holding 期必读键（对齐 engine.py:2563-2591 真身）──
        # 缺 neckline/atr/tp1/tp2 的 order（老 plan 无字段）→ 无法构造 decide_exit state
        # （compute_stop_price 要 neckline+atr，priority 2/3 要 tp1/tp2）→ 只塞 stop_prices 走
        # fallback（保守，不拿脏 state 喂 decide_exit），与真身 engine.py:2570-2571 同款防御。
        neckline = o.get("neckline")
        atr = o.get("atr")
        tp1 = o.get("tp1")
        tp2 = o.get("take_profit")
        stop_price = o.get("stop_price")

        if phase == "holding" and (neckline is not None and atr is not None
                                   and tp1 is not None and tp2 is not None):
            # holding 期 + plan 字段齐全 → 构造 decide_exit 主路径 state（不 KeyError）。
            # holding_days 从 position_book.entry_date 算（与真身 engine.py:2572-2575 同源）；
            # entry_date 缺失（E2E force_state=FILLED 不写真 DB / 老数据）→ 0（base_stop，零回归）。
            entry_date = (entry_dates or {}).get(sym)
            holding_days = trading_days_between(entry_date, _today) if entry_date else 0
            # is_last 用 >=（第 max_holding 日即当末根 → decide_exit TIMEOUT 市价优先强平），
            # 对齐回测 simulate_exit is_last 语义 + engine.py:2580 真身（resolution 6）。
            is_last = holding_days >= max_holding
            state = {
                "phase": "holding",
                # entry：decide_exit holding 分支不读（pnl 在调用方算）；透传 plan 挂单价作语义锚。
                "entry": (o.get("order") or {}).get("price"),
                "stop": float(stop_price) if stop_price is not None else None,  # D12 fallback 观测
                "tp1": float(tp1), "tp2": float(tp2),
                "neckline": float(neckline), "atr": float(atr),
                "holding_days": holding_days, "is_last": is_last,
                # lot 默认 True：实盘 monitor 不翻 lot（_place_take_profit 限价单成交翻），
                # 对齐 simulate_exit 单根无状态语义（engine.py:2590-2591 真身同款）。
                "lot1_open": True, "lot2_open": True,
            }
        elif phase == "pending":
            # pending 期（挂单未成交）：decide_exit pending 分支只读 state.cancel_on + bar.high，
            # 不读 neckline/atr/tp1/tp2（pending 不判 trailing/止盈）。state 简构 + cancel_on 入 state。
            state = {
                "phase": "pending",
                "cancel_on": o.get("cancel_on"),   # None=不撤单放飞（decide_exit pending 分支 .get 安全）
            }
        else:
            # holding 期但 plan 缺 neckline/atr/tp1/tp2（老 plan / 异常）→ 只塞最小 state，
            # decide_exit 会因 compute_stop_price 缺键 KeyError → 走 D12 fallback（stop_prices 兜底）。
            # 与真身 engine.py:2570-2571 同款保守防御：不拿脏 state 喂 decide_exit 主路径。
            state = {
                "phase": "holding",
                "stop": float(stop_price) if stop_price is not None else None,
            }

        monitor_ctx[sym] = {
            "state": state,
            "cfg": _cfg_from_plan(o),
        }
        # pending_ctx：plan 显式提供 cancel_on 即入（无论 holding/pending，真身 engine.py:2599-2601
        # 同款——cancel_on 落盘就塞 pending_ctx，pending 期撤单巡检独立于 decide_exit 路径）。
        if o.get("cancel_on") is not None:
            pending_ctx[sym] = float(o["cancel_on"])
    return monitor_ctx, pending_ctx


def _cfg_from_plan(o: dict) -> dict:
    """从 plan order 提 decide_exit cfg（对齐 simulate_exit cfg 键 + engine._stoploss 真身）。

    物理意图：decide_exit 是 Task 4 执行单源纯函数（回测 simulate_exit 等价已证），
    cfg 键必须覆盖 decide_exit 全部读取位（stop_atr_mult/trailing_*/tp1_portion/max_holding）。
    缺键会致 decide_exit KeyError 退 fallback（D12 should_trigger_stop），丧失 tp1 分级
    止盈 + trailing 演进能力——故 cfg 必须完整。

    Why 从 _trade_cfg() 取默认而非硬编码：与实盘 _trade_cfg() 单源对齐（env
    TRADE_TP_H_MULT 等可调），避免 E2E 与实盘 cfg 漂移致"测了 A 实盘跑 B"。
    plan.max_wait 透传（每标的窗口可能不同，由 signal formed_at 决定）。

    I-1 fix（顺带 M-1）：补全 trailing 三件套（grace/step/floor）+ max_holding，对齐
    engine._stoploss 真身 engine.py:2537-2543 decide_cfg 全集。decide_exit compute_stop_price
    用 cfg.get("trailing_*") 有默认值（不 KeyError），但补全让 E2E 与实盘 cfg 完全同源
    （trailing 真演进而非退化为固定止损），验主路径才有意义。
    """
    from trading.engine import _trade_cfg
    cfg = _trade_cfg()  # 真身默认（env 可调，与实盘同源）
    return {
        "stop_atr_mult": cfg["stop_atr_mult"],
        # trailing 三件套（M-1 fix）：compute_stop_price 用 cfg.get 默认 0/0.0/None，补全让
        # trailing 真演进（holding_days>grace 后每日收紧 step×ATR）。与真身 engine.py:2539-2541 同源。
        "trailing_grace": cfg.get("trailing_grace", 0) or 0,
        "trailing_step": cfg.get("trailing_step", 0.0) or 0.0,
        "trailing_floor": cfg.get("trailing_floor"),
        # tp1_portion：decide_exit priority 3 直读 cfg["tp1_portion"]（必读键，缺即 KeyError）。
        "tp1_portion": cfg.get("tp1_portion", 0.5),
        # max_holding：decide_exit 不直接读（is_last 由调用方算入 state），但 cfg 冗余备用
        # （对齐真身 engine.py:2543 + simulate_exit cfg），便于 _build_ctx 算 is_last 单源。
        "max_holding": cfg.get("max_holding", 15),
        # max_wait：plan 优先（每标的 formed_at 不同窗口可能不同），缺省回退 cfg；pending 期
        # cancel_on 巡检不读 max_wait（独立路径），此处仅语义透传。
        "max_wait": o.get("max_wait", cfg["max_wait"]),
    }
