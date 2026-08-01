# -*- coding: utf-8 -*-
"""V2：真实信号扫描接入（detect_signal 真跑扫创板科创 ~500 × 真实 7 月日线落 T+1 plan）。

物理意图（spec §5 目标 2）：直调 engine.eod_plan 真身扫创板科创 ~500 标的 × data_lake 真实
7 月日线，产真实颈线法信号 → 落 T+1 plan。n_orders=0 也正常（当日无信号，编排层不断言信号数）。
本测试聚焦「真实扫描 → eod_plan 真身落盘 → plan 结构正确」这条链路，不 mock 信号。
"""
from __future__ import annotations

import asyncio
from datetime import date

import pytest

from trading import engine, trading_plan


def test_run_eod_phase_lands_t_plus_1_plan(isolated_state, monkeypatch):
    """run_eod_phase(T 日) → eod_plan 落 T+1 plan（confirmed=AUTO_CONFIRM_PLAN=true）。

    物理意图（spec §5）：路径 B 自扫创板科创 universe（discovery.snapshot.load_universe 冻结
    口径）× detect_signal 在真实 7 月日线无前视真跑 → engine.eod_plan 真身落 T+1 plan。
    断言三层：
        ① plan 落盘（load_plan(T+1) 非 None）；
        ② confirmed=True（AUTO_CONFIRM_PLAN 模拟人审闸已过）；
        ③ orders 嵌套结构正确（每单含 order/stop_price/take_profit 三段，与 Task8 契约一致）。
    n_orders 可能 0（7/1 当日无颈线突破信号），不断言信号数（编排层关注落盘结构而非信号量）。
    """
    # patch 钉钉推送（V5 改真推）：保留 save_plan 真实落盘，不触达 dws。
    # 用模块属性 patch（与 test_e2e_trading_flow.py:isolated 同范式，命中 engine.eod_plan 内调用点）。
    monkeypatch.setattr(trading_plan, "push_plan_to_dingtalk", lambda d, o, **kw: True)
    # AUTO_TRADE_MODE=dry_run：eod_plan 内 _mode() 读到，影子模式（不触真单）。
    monkeypatch.setenv("AUTO_TRADE_MODE", "dry_run")
    # AUTO_CONFIRM_PLAN=true：自动确认（人审闸模拟），eod_plan 落盘即 confirm。
    monkeypatch.setenv("AUTO_CONFIRM_PLAN", "true")

    t_date = date(2026, 7, 1)  # T 日（识别日，df_upto 截断于此）
    result = asyncio.run(_run_eod(t_date))

    # ① T+1 plan 落盘：result["date"] = next_trading_day(T) = T+1（eod_plan 真身返回值）
    t_plus_1 = result["date"]
    plan = trading_plan.load_plan(t_plus_1)
    assert plan is not None, f"T+1={t_plus_1} plan 应落盘（eod_plan 真身）"

    # ② confirmed=True（AUTO_CONFIRM_PLAN=true 模拟人审闸已过）
    assert plan["confirmed"] is True

    # ③ orders 嵌套结构正确（Task8 契约：order + stop_price + take_profit 三段）
    # n_orders=0 不进循环（当日无信号也 PASS，编排层不断言信号数）。
    for o in plan["orders"]:
        assert "order" in o, "每单必含 order 段（symbol/qty/side/price）"
        assert "stop_price" in o, "每单必含 stop_price（止损价）"
        assert "take_profit" in o, "每单必含 take_profit（止盈价）"
        assert "symbol" in o["order"], "order 段必含 symbol"


async def _run_eod(t_date: date) -> dict:
    """异步 wrapper：从 tests.e2e_long_cycle.signal_scanner 导入并跑 run_eod_phase。

    Why 延迟导入：signal_scanner 顶部 import 触发 discovery.snapshot 读 454MB parquet，
    仅在真跑 eod_phase 时才加载——避免 test 模块导入期即触发重 IO（collect-only 场景如
    pytest --co 会卡）。且 isolated_state fixture 必须先生效（patch DB/env）再跑扫描。
    """
    from tests.e2e_long_cycle import signal_scanner
    return await signal_scanner.run_eod_phase(t_date)
