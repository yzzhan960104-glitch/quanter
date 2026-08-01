# -*- coding: utf-8 -*-
"""V2 真实信号扫描：自扫创板科创 universe × 真实 7 月日线 → eod_plan 真身落 T+1 plan。

物理意图（spec §5 目标 2「真实信号扫描」）：
    E2E 调生产真身 ``engine.eod_plan`` 落盘，但【信号不 mock】——信号由颈线法
    ``detect_signal`` 在真实 data_lake 7 月日线上实跑产生（无前视纯函数，df_upto 截断于
    T 日），让 e2e 验证的不仅是「落盘/挂单/对账」编排层，而是「真实算法在真实数据上
    产真实信号」这条完整业务链路。

路径选型（controller 决策：路径 B）：
    路径 A（直调 engine._eod）的关键障碍——_eod 第一步 resolve_active() 读生产
    experiment/experiments.db（非 tmp 隔离），不预置 ACTIVE 实验则 fail-fast 不落 plan；
    且 _eod 耦合 broadcast/完整性 gate 周边，破坏 E2E「可重复（不依赖 experiment DB 状态）」。
    路径 B 绕开 experiment 耦合：用 ``discovery.snapshot.load_universe``（snapshot 冻结口径，
    与 _eod 内部 _load_universe 创板科创前缀过滤对齐）自扫 universe + 逐标的跑 detect_signal
    + 调 eod_plan 真身落盘。既守「真实信号 + eod_plan 真身」红线，又满足可重复性。

隔离与影子（spec §2 红线）：
    - AUTO_TRADE_MODE=dry_run：eod_plan/pre_open/post_close 内 _mode() 读到，影子模式不触真单。
    - AUTO_CONFIRM_PLAN=true：模拟人审闸已过（eod_plan 落盘即 confirm），让 pre_open 直挂。
    - 钉钉推送由调用方（conftest/job）patch push_plan_to_dingtalk（V5 改真推）。
"""
from __future__ import annotations

from datetime import date

import pandas as pd

from trading import engine


async def run_eod_phase(t_date: date) -> dict:
    """T 日盘后：自扫创板科创 universe × detect_signal 真跑 → eod_plan 落 T+1 plan。

    物理时序（spec §5）：
        ① load_universe(start="2025-01-01")：创板科创 ~500 标的（snapshot 冻结口径，含 2025+
           2026 OHLCV），与 _eod 内部 _load_universe 创板科创前缀过滤对齐。
        ② 逐标的无前视截断 df_upto = sym_df[sym_df.index <= T 日]：window 不够（<DEFAULTS[
           window]）跳过（颈线 60 日窗口预热，新上市标的天然漏）。
        ③ detect_signal(sym, df_upto, DEFAULTS, EXEC_DEFAULTS, T 日)：颈线法识别纯函数真跑，
           产 Signal 或 None（无命中/cancel_on 守卫触发/非当日突破均返 None）。
        ④ eod_plan(T+1, signals, atr_map, capital) 真身落盘：落 T+1 plan + trade_event
           (SIGNAL/CONFIRMED)（AUTO_CONFIRM_PLAN=true 自动确认）。

    Args:
        t_date: T 日（识别日，df_upto 截断于此，严格因果无前视）。

    Returns:
        eod_plan 真身返回值 {"date": T+1, "n_orders": N, "mode":..., "auto_confirmed":...}。
        n_orders=0 正常（当日无信号），编排层不据此判失败。
    """
    # 延迟导入：discovery.snapshot 读 454MB parquet 较重，仅 eod_phase 用到时才加载。
    # EXEC_DEFAULTS 在 strategies.neckline.backtest 定义（method_v0 不导出），走包级 re-export。
    from discovery.snapshot import load_universe
    from strategies.neckline import EXEC_DEFAULTS
    from strategies.neckline.method_v0 import DEFAULTS, detect_signal

    # ① 加载创板科创 universe（snapshot 冻结口径：创板科创 + 近30日均额≥1e5千元）
    universe = load_universe(start="2025-01-01")

    # T+1 计划生效日（与 _eod 同口径：next_trading_day(today)），eod_plan 落盘 key = T+1
    t_plus_1 = engine.calendar.next_trading_day(t_date.isoformat())

    signals: list = []
    atr_map: dict = {}
    t_ts = pd.Timestamp(t_date)  # df_upto 截断锚点（pd.Timestamp 比较稳于 str）

    # ②③ 逐标的无前视扫描：df_upto 截断于 T 日，detect_signal 严格因果
    for sym, sym_df in universe.items():
        # 无前视截断：仅保留 T 日及之前 K 线（不含 T 日之后，防 look-ahead bias）
        df_upto = sym_df[sym_df.index <= t_ts]
        # 窗口预热：颈线需 window(默认60) 日形成，不足则跳过（不拿残缺窗口硬算伪信号）
        if len(df_upto) < DEFAULTS["window"]:
            continue
        # 颈线法识别纯函数真跑（无 mock）：返 Signal 或 None
        sig = detect_signal(sym, df_upto, DEFAULTS, EXEC_DEFAULTS, t_ts)
        if sig is not None:
            signals.append(sig)
            # atr_map 供 eod_plan → build_orders_from_signals 算 stop/tp（缺 ATR 跳过挂单）
            atr_map[sym] = sig.atr

    # ④ eod_plan 真身落盘：落 T+1 plan + trade_event(SIGNAL/CONFIRMED)。
    # AUTO_CONFIRM_PLAN=true（由调用方 env 注入）→ 落盘即 confirm，pre_open 次日直挂。
    return await engine.eod_plan(t_plus_1, signals, atr_map, capital=1_000_000.0)


async def run_pre_open_phase(t_plus_1: date, gw) -> dict:
    """T+1 开盘前：直调 engine.pre_open(today=T+1)（与 test_e2e_trading_flow.test_step3 同范式）。

    物理意图（spec §5）：读 T 日落的已确认计划 → 撤昨日遗留单 → 注入白名单 → 逐单挂单。
    gw 由 V4 ProbabilisticBroker 提供（patch engine.get_gateway）；AUTO_TRADE_MODE=dry_run
    下 _submit 返 DRY_RUN 不触真单。模块级直调拿业务返回 dict（{'submitted': N, ...}），
    不依赖 TradingEngine._pre_open 的 cron 包装层（其返 None）。
    """
    return await engine.pre_open(t_plus_1.isoformat())


async def run_post_close_phase(t_plus_1: date, gw) -> dict:
    """T+1 盘后：直调 engine.post_close(date=T+1, gw, local_positions)（与 test_step4 同范式）。

    物理意图（spec §5）：对账（broker 持仓 vs 本地账本）+ trailing 演进 + max_holding 标记
    + 清动态白名单。local_positions 从 position_book 真身读（eod→pre_open→成交回报已写入）。
    """
    from trading import position_book
    return await engine.post_close(
        t_plus_1.isoformat(),
        gw=gw,
        local_positions=position_book.get_local_positions(),
    )
