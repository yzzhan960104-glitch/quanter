# -*- coding: utf-8 -*-
"""组件3 ProbabilisticBroker：QMT gw 行为层概率模拟（spec §7）。

物理意图：mock QMT 网关"行为"（成交/拒单/部分/延迟），价格全真（stk_mins via MinBarFeeder）。
固定 random.Random(seed) → 事件序列可重复；构造场景（熔断日/超期标的）显式指定。
gate（_gw_health_gate）放行：gw._connected=True + is_client_ready=True。
"""
from __future__ import annotations

import random
from contextlib import contextmanager
from datetime import date, time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

# 概率参数（spec §7）
P_FILLED = 0.70
P_PARTIAL = 0.15
P_REJECTED = 0.05
# TODO(spec §7 未实现，final review Important-2)：主推延迟（成交回报延后 1 时点注入）。
# spec §7 表把"主推延迟 10%"列为 4 种概率事件之一，模拟 QMT 成交回报 1-2s 延迟下 stoploss
# 状态机一致性。V4 只实现了 FILLED/PARTIAL_FILLED/REJECTED 三态，本常量声明了槽位但
# _sample_state 从未读取它（FILLED 路径吞掉 80% = P_FILLED + P_DELAYED），_delayed_fills
# 队列从未 append/pop。V7+ 若要补主推延迟注入：
#   ① _sample_state：r 落 [P_FILLED, P_FILLED+P_DELAYED) → 返 "DELAYED"，订单入 _delayed_fills；
#   ② 下一盘中时点 stoploss 进入时 flush _delayed_fills（_handle_order_update 延后注入成交回报）；
#   ③ 验 stoploss 在延迟窗口内不重复发单 / 不漏平仓（状态一致性）。
# 保留常量 + 队列声明而非删除：避免 spec §7 设计意图丢失，下个阅读者能直接接住 V7+ 实现。
P_DELAYED = 0.10  # 主推延迟（成交回报延后注入，spec §7 未实现，见 TODO）


class ProbabilisticBroker:
    """QMT gw 行为模拟器：概率成交 + 构造熔断/超期。

    Args:
        seed: 随机种子（可重复）。
        min_bar_feeder: MinBarFeeder（成交价取 stk_mins 时点价）。
        circuit_breaker_days: 熔断日集合（query_asset 返 start×0.96）。
        start_equity: 熔断基线。
        expired_symbols: 超期标的 {sym: {entry_date, holding_days_ref}}（_fetch_broker_positions 注入）。
        force_state: 测试用强制状态（绕过概率）。
    """

    def __init__(self, seed: int, min_bar_feeder: Any,
                 circuit_breaker_days: set[date] | None = None,
                 start_equity: float = 1_000_000.0,
                 expired_symbols: dict | None = None,
                 force_state: str | None = None) -> None:
        self._rng = random.Random(seed)
        self._feeder = min_bar_feeder
        self._cb_days = circuit_breaker_days or set()
        self._start_equity = start_equity
        self._expired = expired_symbols or {}
        self._force_state = force_state
        # TODO(spec §7 未实现，final review Important-2，与 P_DELAYED 配套）：成交回报延迟队列，
        # 模拟 QMT 主推延后 1 时点注入。当前从未 append/pop（_sample_state FILLED 路径直接同步返），
        # 保留槽位等 V7+ 补延迟注入逻辑（详见 P_DELAYED TODO）。删除会导致 spec §7「主推延迟」
        # 设计意图完全丢失，故保守保留 + 显式标注。
        self._delayed_fills: list[dict] = []
        self._positions: dict[str, dict] = {}  # 内存持仓（模拟 gw._fetch_broker_positions）

    def price_for(self, sym: str, t_date: date, up_to: time) -> float:
        """取 stk_mins 时点价（成交价/止损价真实）。"""
        q = self._feeder.feed([sym], t_date, up_to)
        return q.get(sym, {}).get("last_price", 10.0)

    def simulate_submit(self, order: dict, t_date: date, up_to: time) -> dict:
        """概率分发：FILLED/PARTIAL_FILLED/REJECTED + 延迟。返 _submit 等价 result dict。"""
        state = self._force_state or self._sample_state()
        price = self.price_for(order["symbol"], t_date, up_to)
        oid = f"{t_date.isoformat()}_{order['symbol']}_{self._rng.randint(0, 99999)}"
        if state == "FILLED":
            traded = order["qty"]
            self._positions[order["symbol"]] = {"volume": traded, "avg_price": price}
            return {"order_id": oid, "state": "FILLED", "price": price, "traded_volume": traded}
        if state == "PARTIAL_FILLED":
            traded = max(100, int(order["qty"] * self._rng.uniform(0.3, 0.7)) // 100 * 100)
            self._positions[order["symbol"]] = {"volume": traded, "avg_price": price}
            return {"order_id": oid, "state": "PARTIAL_FILLED", "price": price, "traded_volume": traded}
        return {"order_id": oid, "state": "REJECTED", "message": "涨停价拒单（模拟）"}

    def _sample_state(self) -> str:
        r = self._rng.random()
        if r < P_REJECTED:
            return "REJECTED"
        if r < P_REJECTED + P_PARTIAL:
            return "PARTIAL_FILLED"
        return "FILLED"  # 含延迟（延迟在 _handle_order_update 注入时体现）

    def simulate_query_asset(self, t_date: date) -> dict:
        """构造熔断日：熔断日返 start×0.96（-4% < -3% 阈值）；正常日返 start×(1+小波动)。"""
        if t_date in self._cb_days:
            return {"total_asset": self._start_equity * 0.96,
                    "cash": self._start_equity * 0.5, "market_value": self._start_equity * 0.46}
        # 正常日小幅波动（+0.5%~-1%）
        drift = self._rng.uniform(-0.01, 0.005)
        return {"total_asset": self._start_equity * (1 + drift),
                "cash": self._start_equity * 0.5, "market_value": self._start_equity * 0.5}

    def simulate_fetch_positions(self, t_date: date) -> dict[str, dict]:
        """返当前内存持仓（含构造的超期标的 entry_date）。"""
        # 注入构造超期标的（_scan_expired_positions 读 entry_date 算 holding_days）
        out = dict(self._positions)
        for sym, meta in self._expired.items():
            if t_date >= meta.get("holding_days_ref", t_date):
                out.setdefault(sym, {"volume": 100, "avg_price": 10.0,
                                     "entry_date": meta["entry_date"]})
        return out

    @contextmanager
    def attach(self, t_date: date, up_to: time):
        """patch engine gateway 链路：get_gateway 返 mock gw + _submit/_cancel_all 概率 + 持仓/资产注入。

        生命周期：ReplayDriver 每阶段（pre_open/stoploss/post_close）进入此 context。
        """
        gw = MagicMock()
        gw._connected = True
        gw.is_client_ready = lambda *a, **kw: True  # gate 放行
        gw.is_locked = False
        gw._lock_down = False
        gw._orders = {}
        gw.query_asset = AsyncMock(return_value=self.simulate_query_asset(t_date))
        gw._fetch_broker_positions = AsyncMock(return_value=self.simulate_fetch_positions(t_date))
        gw.query_orders = AsyncMock(return_value=[])
        gw.cancel_order = AsyncMock(return_value=None)
        gw._confirm_cancelled = AsyncMock(return_value=True)
        # post_close 真身调 reconcile_job.run_reconcile → await gw.sync_positions(...)
        # （BaseExecutionGateway 模板方法）。补 AsyncMock 返零漂 ReconciliationResult，
        # 否则 post_close 对账崩（V7 串联时暴露的 V4 缺口）。
        # 物理含义：概率 broker 内存持仓即"真相"，对账视为零漂（drift 由概率模型自洽）。
        # 全链路真漂移场景由 ReportBuilder._check_consistency 从 fill/position 表实算暴露。
        from trading.reconcile_job import ReconciliationResult
        gw.sync_positions = AsyncMock(
            return_value=ReconciliationResult(
                matched=[], drifted=[], only_local=[], only_broker=[],
                max_abs_drift=0.0, is_ok=True))

        async def _submit_mock(order, *, confirm=True):
            return self.simulate_submit(
                {"symbol": order.symbol, "qty": order.qty, "side": order.side, "price": order.price},
                t_date, up_to)

        with patch("trading.engine.get_gateway", lambda: gw), \
             patch("trading.engine._submit", _submit_mock), \
             patch("trading.engine._cancel_all_open_orders",
                   AsyncMock(return_value={"cancelled": 0, "unconfirmed": 0})):
            yield gw
