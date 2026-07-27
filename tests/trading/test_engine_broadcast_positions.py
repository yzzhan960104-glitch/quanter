# -*- coding: utf-8 -*-
"""_broadcast_positions_pnl 格式单测：每仓成本/现价/盈亏% + 账户浮亏率（Task12+）。

物理意图：钉钉持仓播报是研究员 19:00 看的盈亏全貌，格式拼错 / 字段名错 = 误导人审。
固化三条契约：
  ① 每仓字段齐全 → 「成本 现价 +N.N%（浮盈）」三件套；
  ② 汇总 → 累计盈亏 + 浮亏率 = Σ浮盈 / Σ(avg×qty)（B1 零新存储口径）；
  ③ 盲价仓位（avg/last/pct 缺失）→ 显 N/A，不计入浮亏率分母（不猜价审计红线）。

mock 范式：``engine.get_gateway``（模块级函数，engine.py:151 注释明示 monkeypatch 点）+
局部 import 的 ``get_positions`` / ``NotificationManager`` / ``fire_and_forget`` patch
真身模块路径（与 test_engine_order_update_handler 同款局部 import patch 口径）。

asyncio 约定：本仓 pytest-asyncio 为 strict 模式，用 ``asyncio.run(...)`` 同步包装
（与 Task 8/10/12 同口径，不引入 @pytest.mark.asyncio 风格分叉）。
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from trading.engine import TradingEngine


def _fake_position(sym: str, qty: float, avg: float, last: float) -> dict:
    """构造 get_positions 返回项（avg/last 给定 → pnl/pnl_pct 可算），对齐真身字段集。"""
    pnl = (last - avg) * qty
    pct = (last - avg) / avg * 100 if avg else None
    return {
        "symbol": sym, "qty": qty,
        "avg_price": avg, "last_price": last,
        "market_value": last * qty, "pnl": pnl, "pnl_pct": pct,
        "strategy": None, "entry_rationale": None,
    }


def _run_broadcast(positions: list, total_asset: float = 10000.0) -> str:
    """调 _broadcast_positions_pnl，截获 notify_risk_event 的 md 并返回。"""
    async def _go() -> str:
        eng = TradingEngine()
        gw = MagicMock()
        gw.query_asset = AsyncMock(return_value={"total_asset": total_asset})
        nm_inst = MagicMock()
        # notify_risk_event 被 fire_and_forget 包裹前先求值（同步调），用 MagicMock 记录 msg
        nm_inst.notify_risk_event = MagicMock(return_value=None)
        with patch("trading.engine.get_gateway", return_value=gw), \
             patch("presentation.server.services.trading_service.get_positions",
                   new=AsyncMock(return_value=positions)), \
             patch("infra.notifier.NotificationManager") as NM, \
             patch("infra.notifier.fire_and_forget"):
            NM.get_default.return_value = nm_inst
            await eng._broadcast_positions_pnl()
        return nm_inst.notify_risk_event.call_args.args[0]
    return asyncio.run(_go())


def test_broadcast_per_position_cost_last_pct():
    """每仓齐全 → 「成本 现价 +N.N%（浮盈）」三件套（盈/亏两种符号都覆盖）。"""
    positions = [
        _fake_position("300001.SZ", 100, 10.0, 11.0),   # +10%
        _fake_position("300002.SZ", 100, 11.0, 10.0),   # -9.09%
    ]
    md = _run_broadcast(positions)
    assert "300001.SZ" in md and "300002.SZ" in md
    assert "成本10.00" in md and "现价11.00" in md   # 成本/现价渲染（2 位小数）
    assert "+10.00%" in md                            # 盈亏% 正
    assert "-9.09%" in md                             # 盈亏% 负（浮亏场景）
    assert "浮盈+100" in md and "浮盈-100" in md       # 绝对浮盈（+/- 前缀）


def test_broadcast_account_pnl_rate():
    """汇总浮亏率 = Σ浮盈 / Σ(avg×qty)（B1 零新存储，相对持仓成本的投入产出比）。

    仓1 成本 10×100=1000 浮盈+100；仓2 成本 11×100=1100 浮盈-100
    → Σ浮盈=0, Σ成本=2100 → 浮亏率 0.00%。
    """
    positions = [
        _fake_position("300001.SZ", 100, 10.0, 11.0),
        _fake_position("300002.SZ", 100, 11.0, 10.0),
    ]
    md = _run_broadcast(positions)
    assert "累计盈亏" in md
    assert "0.00%" in md            # 浮亏率渲染
    summary = [ln for ln in md.splitlines() if "汇总" in ln][0]
    assert "已估值 2/2 仓" in summary   # 两仓均非盲价，计入已估值


def test_broadcast_blind_price_na():
    """盲价仓位（avg/last/pct 全 None）→ 显 N/A，不计入浮亏率分母（不猜价红线）。"""
    positions = [
        {"symbol": "300001.SZ", "qty": 100.0, "avg_price": None, "last_price": None,
         "market_value": None, "pnl": None, "pnl_pct": None,
         "strategy": None, "entry_rationale": None},
    ]
    md = _run_broadcast(positions)
    assert "浮盈N/A" in md
    # 全盲 → total_cost=0 → 汇总行不显浮亏率（rate_str=""），且已估值 0/1
    summary = [ln for ln in md.splitlines() if "汇总" in ln][0]
    assert "%" not in summary
    assert "已估值 0/1 仓" in summary
