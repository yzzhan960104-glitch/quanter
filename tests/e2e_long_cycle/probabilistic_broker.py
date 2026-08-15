# -*- coding: utf-8 -*-
"""组件3 ProbabilisticBroker：QMT gw 行为层概率模拟（spec §7 + design 2026-08-01 成交回报注入）。

物理意图：mock QMT 网关"行为"（成交/拒单/部分/延迟），价格全真（stk_mins via MinBarFeeder）。
固定 random.Random(seed) → 事件序列可重复；构造场景（熔断日/超期标的）显式指定。
gate（_gw_health_gate）放行：gw._connected=True + is_client_ready=True。

2026-08-01 增强（design §4.1 · 报表真实交易/持仓列表）：
- FILLED/PARTIAL_FILLED/REJECTED 先入 ``_pending_reports``，由 orchestrator 在 attach 上下文内
  经 ``engine._handle_order_update`` 真身落账（fill/position/trade_event），防空转；
- TP 限价单（SELL 且 price 命中 plan tp1/tp2 ±1e-6）挂 ``_resting`` 等价格，盘中 stk_mins
  累积 high >= tp 价才成交（真实价格驱动，非挂即成交）；
- 持仓镜像 ``_positions`` 真实增减（BUY 加 / SELL 减），SELL clamp 到 0 防负持仓/双卖。
"""
from __future__ import annotations

import random
from contextlib import contextmanager
from datetime import date, time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from trading import clock

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

# QMT order_type 契约（与 broker/qmt.py:724 + engine._order_direction 同源；CI 无 xtquant 硬编码）
_STOCK_BUY = 23
_STOCK_SELL = 24

# 注入深度上限：BUY fill → TP 挂单 → TP fill 链式排空，防环（design §7 风险缓解）
_MAX_INJECT_DEPTH = 50


class ProbabilisticBroker:
    """QMT gw 行为模拟器：概率成交 + 构造熔断/超期 + 成交回报注入。

    Args:
        seed: 随机种子（可重复）。
        min_bar_feeder: MinBarFeeder（成交价取 stk_mins 时点价）。
        circuit_breaker_days: 熔断日集合（query_asset 返 start×0.96）。
        start_equity: 熔断基线。
        expired_symbols: 超期标的 {sym: {entry_date, holding_days_ref}}（_fetch_broker_positions 注入）。
        force_state: 测试用强制状态（绕过概率，仅 BUY 生效）。
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
        # 保留槽位等 V7+ 补延迟注入逻辑（详见 P_DELAYED TODO）。
        self._delayed_fills: list[dict] = []
        # 持仓镜像（design §4.1）：BUY 加 / SELL 减，clamp 0；stop_loss_monitor 经
        # gw._fetch_broker_positions 读它，与 DB position（apply_fill_to_position）同源演进。
        self._positions: dict[str, dict] = {}
        # 待注入成交回报（inject_fills 排空）：{oid, symbol, side, qty, price, state, t_date, traded_time}
        self._pending_reports: list[dict] = []
        # TP 限价挂单 {oid: {symbol, qty, price, t_date, traded_time}}（盘中 high>=tp 才成交）
        self._resting: dict[str, dict] = {}
        self._last_gw = None  # attach 注入的 mock gw（simulate_submit 注册方向锚点用）

    # ============================= 行情 =============================
    def price_for(self, sym: str, t_date: date, up_to: time) -> float:
        """取 stk_mins 时点价（成交价/止损价真实）。"""
        q = self._feeder.feed([sym], t_date, up_to)
        return q.get(sym, {}).get("last_price", 10.0)

    # ==================== 持仓镜像 + 回报队列 ====================
    def _apply_mirror(self, symbol: str, qty: float, price: float) -> None:
        """镜像持仓增减：BUY 加（加权 avg）/ SELL 减（clamp 0，归零删行）。"""
        qty = float(qty)
        if qty > 0:
            old = float(self._positions.get(symbol, {}).get("volume", 0.0))
            old_avg = self._positions.get(symbol, {}).get("avg_price")
            new_qty = old + qty
            new_avg = (old * old_avg + qty * price) / new_qty if old > 0 and old_avg else price
            self._positions[symbol] = {"volume": new_qty, "avg_price": new_avg}
        else:
            cur = float(self._positions.get(symbol, {}).get("volume", 0.0))
            new = max(0.0, cur + qty)  # clamp 防负（真实柜台不会卖超，模拟同口径）
            if new <= 0:
                self._positions.pop(symbol, None)
            else:
                self._positions[symbol]["volume"] = new

    def _queue_report(self, oid: str, symbol: str, side: str, qty: float, price: float,
                      state: str, t_date: date, traded_time: str,
                      purpose: str | None = None) -> None:
        """排队一笔成交回报（FILLED/PARTIAL_FILLED/REJECTED），供 inject_fills 注入。

        purpose 仅 TP1/TP2 成交时携带（resting 触发已知目的）；市价卖单为 None。
        """
        self._pending_reports.append({
            "oid": oid, "symbol": symbol, "side": side, "qty": float(qty),
            "price": float(price), "state": state, "t_date": t_date,
            "traded_time": traded_time, "purpose": purpose,
        })

    def _tp_purpose(self, symbol: str, price: float, t_date: date) -> str | None:
        """SELL 是否 TP 限价单：price 命中 plan 该标的 tp1/take_profit（±1e-6）。

        Why 从 plan 判而非看价格高低：STOP/超期市价卖也带价（跌停/现价），只有命中计划
        止盈价才能确定是"限价挂单等成交"（spec §7：high >= tp 才触发）。
        返 purpose（TP1/TP2）供 resting 与 _backfill_sell_order_state 精确回填。
        """
        from trading import trading_plan
        plan = trading_plan.load_plan(t_date.isoformat())
        for o in (plan or {}).get("orders", []):
            if (o.get("order") or {}).get("symbol") != symbol:
                continue
            if o.get("tp1") is not None and abs(float(price) - float(o["tp1"])) < 1e-6:
                return "TP1"
            if o.get("take_profit") is not None and abs(float(price) - float(o["take_profit"])) < 1e-6:
                return "TP2"
        return None

    @staticmethod
    def _db_state(state: str) -> str:
        """回报状态 → order 表 state 列（_order_state_to_db 契约：PARTIAL_FILLED → PARTIAL）。"""
        return "PARTIAL" if state == "PARTIAL_FILLED" else state

    def _backfill_sell_order_state(self, rep: dict, oid: str) -> None:
        """TP/STOP/EXPIRED 行 broker_oid 回填（E2E 不模拟生产 async_response 链路）。

        生产 _record_tp/add_order_qty/_record_stop/超期平仓的内部 order_id 确定性生成
        ``{date}_{symbol}_{purpose}_1``。⚠️ 必须按【实际成交目的】回填，不能盲扫全部
        目的行——否则 STOP 成交会把同 symbol 未触发的 TP2 行误标 FILLED（smoke 实测）。
        - TP1/TP2：resting 触发时目的已知（rep["purpose"]）；
        - STOP/EXPIRED_CLOSE（市价卖）：两者至多一行存在，且价格应与成交价一致
          （STOP 行由 _record_stop 落盘价=成交价；EXPIRED 行落跌停/现价），用价格匹配防误标。
        """
        from trading import state_store
        purposes = ([rep["purpose"]] if rep.get("purpose") in ("TP1", "TP2")
                    else ["STOP", "EXPIRED_CLOSE"])
        for purpose in purposes:
            internal_oid = f"{rep['t_date']}_{rep['symbol']}_{purpose}_1"
            try:
                with state_store._connect(state_store._DEFAULT_DB) as con:
                    row = con.execute(
                        'SELECT order_id, price FROM "order" WHERE order_id=?',
                        (internal_oid,)).fetchone()
                    if row is None:
                        continue  # 该目的行不存在（单腿 plan 无 TP1 / 未落 STOP 行）
                    if purpose in ("STOP", "EXPIRED_CLOSE") and row["price"] is not None                             and abs(float(row["price"]) - float(rep["price"])) > 1e-6:
                        continue  # 价格不匹配 → 非本笔成交的目的行，跳过
                state_store.update_order_state(
                    internal_oid, self._db_state(rep["state"]), broker_oid=oid,
                    filled_qty=rep["qty"], filled_price=rep["price"])
            except Exception:
                pass  # 行不存在/未落库 → 跳过（软降级，不阻断注入）

    # ========================= 成交回报注入 =========================
    async def inject_fills(self, eng) -> None:
        """排空待注入回报：kind=order 推进状态 + kind=trade 生产落账（fill/position/事件）。

        物理意图（design §4.1）：真实 QMT 的成交回报链（柜台状态推送 + 成交推送）在 E2E
        里由本方法模拟——先发 kind=order（_advance_order_state_from_status 推进 order.state），
        再发 kind=trade（_handle_order_update 真身写 insert_fill → apply_fill_to_position →
        trade_event FILLED；BUY 还会经 _place_take_profit 挂 TP 限价单 → 新回报入队循环排空）。

        Args:
            eng: TradingEngine 实例（_handle_order_update 真身；调用方须已设 eng._gw = mock gw）。
        """
        depth = 0
        while self._pending_reports and depth < _MAX_INJECT_DEPTH:
            rep = self._pending_reports.pop(0)
            oid = rep["oid"]
            if rep["state"] == "REJECTED":
                # 拒单无成交：只推进 order 状态（不落 fill/position）
                await eng._handle_order_update(
                    {"kind": "order", "order_id": oid, "state": "REJECTED", "traded_volume": 0})
                continue
            # ① 柜台委托状态推送（累计成交口径 → order.state/filled_*）
            await eng._handle_order_update({
                "kind": "order", "order_id": oid, "state": rep["state"],
                "traded_volume": rep["qty"], "traded_price": rep["price"]})
            # ② 成交回报 → 生产账本（fill/position/trade_event + BUY 挂 TP）
            await eng._handle_order_update({
                "kind": "trade", "order_id": oid, "stock_code": rep["symbol"],
                "traded_volume": rep["qty"], "traded_price": rep["price"],
                "traded_time": rep["traded_time"]})
            # ③ TP/STOP 行 broker_oid 回填（见 _backfill_sell_order_state）
            if rep["side"] == "SELL":
                self._backfill_sell_order_state(rep, oid)
            depth += 1

    async def scan_resting_and_inject(self, eng, t_date: date, up_to: time) -> None:
        """盘中扫描 TP 限价单：stk_mins 累积 high >= tp 价 → FILLED（真实价格驱动）。

        物理意图（spec §7）：TP1/TP2 是预挂限价卖单，成交与否由真实分钟行情决定——
        当日截至当前时点的高价触及限价即成交（非概率）。只在盘中时点调用
        （9:30-15:00），pre_open/post_close 不扫（盘前无 bar、盘后不再成交）。

        Args:
            eng: TradingEngine 实例（透传 inject_fills）。
            t_date: 交易日（T+1）。
            up_to: 当前盘中时点（ReplayDriver freeze 的 clock.now().time()）。
        """
        if not self._resting:
            return
        syms = sorted({r["symbol"] for r in self._resting.values()})
        quotes = self._feeder.feed(syms, t_date, up_to)
        for oid in list(self._resting):
            r = self._resting[oid]
            high = (quotes.get(r["symbol"]) or {}).get("high")
            if high is None or float(high) < float(r["price"]):
                continue  # 价格未到，继续挂单
            # 成交 qty clamp 到当前镜像持仓（TP1 已成交后 TP2 不会超卖）
            qty = min(float(r["qty"]),
                      float(self._positions.get(r["symbol"], {}).get("volume", 0.0)))
            if qty > 0:
                self._apply_mirror(r["symbol"], -qty, r["price"])
                self._queue_report(oid, r["symbol"], "SELL", qty, r["price"],
                                   "FILLED", t_date, r["traded_time"],
                                   purpose=r.get("purpose"))
            del self._resting[oid]
        if self._pending_reports:
            await self.inject_fills(eng)

    # ========================= 模拟接口 =========================
    def simulate_submit(self, order: dict, t_date: date, up_to: time) -> dict:
        """概率分发：FILLED/PARTIAL_FILLED/REJECTED + TP 限价挂单 + 市价卖单。

        物理意图（design §4.1）：
        - BUY（OPEN）：70/15/5 概率（force_state 可覆盖）→ 排队回报 + 镜像加仓；
        - SELL 命中 plan tp1/tp2：TP 限价单 → SUBMITTED 挂 _resting（不立即成交）；
        - SELL 其他（STOP/超期平仓）：市价卖 → FILLED + 镜像减仓（clamp 防负）。
        """
        symbol = order["symbol"]
        qty = float(order["qty"])
        side = str(order.get("side") or "buy").upper()
        price = float(order.get("price") or 0.0) or self.price_for(symbol, t_date, up_to)
        oid = f"{t_date.isoformat()}_{symbol}_{self._rng.randint(0, 99999)}"
        traded_time = clock.now().isoformat()
        # 方向反查锚点：engine._order_direction 先查 DB side，miss 时回退 gw._orders
        # （order_type 23=BUY / 24=SELL，与 broker/qmt.py:724 同源）
        if self._last_gw is not None:
            self._last_gw._orders[oid] = {"order_type": _STOCK_BUY if side == "BUY" else _STOCK_SELL}

        tp_purpose = self._tp_purpose(symbol, price, t_date) if side == "SELL" else None
        if tp_purpose is not None:
            # TP 限价单：挂单等价格（spec §7：stk_mins 当日 high ≥ tp 价即触发）
            self._resting[oid] = {"symbol": symbol, "qty": qty, "price": price,
                                  "t_date": t_date, "traded_time": traded_time,
                                  "purpose": tp_purpose}
            return {"order_id": oid, "state": "SUBMITTED", "price": price}

        if side == "SELL":
            # STOP/超期市价卖：立即成交；clamp 到镜像持仓（防负/双卖）
            qty = min(qty, float(self._positions.get(symbol, {}).get("volume", 0.0)))
            if qty <= 0:
                return {"order_id": oid, "state": "REJECTED",
                        "message": "无持仓可卖（镜像 clamp）"}
            self._apply_mirror(symbol, -qty, price)
            self._queue_report(oid, symbol, "SELL", qty, price, "FILLED", t_date, traded_time)
            return {"order_id": oid, "state": "FILLED", "price": price, "traded_volume": qty}

        # BUY：概率（force_state 覆盖；_sample_state 含延迟槽位见 P_DELAYED TODO）
        state = self._force_state or self._sample_state()
        if state == "REJECTED":
            self._queue_report(oid, symbol, "BUY", 0, price, "REJECTED", t_date, traded_time)
            return {"order_id": oid, "state": "REJECTED", "message": "涨停价拒单（模拟）"}
        traded = qty if state == "FILLED" else \
            max(100, int(qty * self._rng.uniform(0.3, 0.7)) // 100 * 100)
        self._apply_mirror(symbol, traded, price)
        self._queue_report(oid, symbol, "BUY", traded, price, state, t_date, traded_time)
        return {"order_id": oid, "state": state, "price": price, "traded_volume": traded}

    def _sample_state(self) -> str:
        r = self._rng.random()
        if r < P_REJECTED:
            return "REJECTED"
        if r < P_REJECTED + P_PARTIAL:
            return "PARTIAL_FILLED"
        return "FILLED"  # 含延迟（延迟在 _handle_order_update 注入时体现）

    def simulate_query_asset(self, t_date: date, up_to: time | None = None) -> dict:
        """构造熔断日：熔断日【盘后】返 start×0.96（-4% < -3% 阈值）。

        物理语义（full_run 集成修复 · 根因 3）：
            熔断判定的数学契约是 ``check_daily_loss_limit(start_equity, curr_equity)``，
            其中 ``start_equity`` 是 pre_open（09:25）抓的「未受当日交易影响」基线，
            ``curr_equity`` 是 post_close（15:30）拉的「盘后已受当日亏损」总资产。
            回撤 = (curr - start) / start，熔断日 curr 应比 start 低 ≥3%。

        Args:
            t_date: 交易日期。
            up_to: 时点（pre_open 09:25 / post_close 15:30）；None 时按盘后语义。
        """
        from datetime import time as _time
        # 盘前时点（pre_open 09:25 抓基线）：返未受当日交易影响的基线值。
        if up_to is not None and up_to < _time(9, 30):
            return {"total_asset": self._start_equity,
                    "cash": self._start_equity * 0.5, "market_value": self._start_equity * 0.5}
        # 盘后/盘中时点：熔断日返 -4%，正常日小波动。
        if t_date in self._cb_days:
            return {"total_asset": self._start_equity * 0.96,
                    "cash": self._start_equity * 0.5, "market_value": self._start_equity * 0.46}
        drift = self._rng.uniform(-0.01, 0.005)
        return {"total_asset": self._start_equity * (1 + drift),
                "cash": self._start_equity * 0.5, "market_value": self._start_equity * 0.5}

    def simulate_fetch_positions(self, t_date: date) -> dict[str, dict]:
        """返当前镜像持仓（含构造超期标的 entry_date），并同步 expired 入镜像。

        物理意图：stop_loss_monitor 真身经 gw._fetch_broker_positions 巡检持仓；
        超期构造标的（300099.SZ）须进镜像，才能被 monitor/超期平仓 SELL clamp 后真实减仓。
        """
        # 构造超期标的入镜像（SELL 平仓 clamp 需要；同步后返回副本）
        for sym, meta in self._expired.items():
            if t_date >= meta.get("holding_days_ref", t_date):
                pos = self._positions.setdefault(
                    sym, {"volume": 100, "avg_price": 10.0, "entry_date": meta["entry_date"]})
                pos.setdefault("entry_date", meta["entry_date"])
        # ⚠️ 必须返副本且【不重绑 self._positions】：stop_loss_monitor 迭代本返回值期间，
        # simulate_submit/_apply_mirror 会原地改 self._positions；重绑会让 monitor 迭代的
        # dict 与镜像同一对象 → "dictionary changed size during iteration"（smoke 实测）。
        return dict(self._positions)

    @contextmanager
    def attach(self, t_date: date, up_to: time):
        """patch engine gateway 链路：get_gateway 返 mock gw + _submit/_cancel_all 概率 + 持仓/资产注入。

        生命周期：ReplayDriver 每阶段（pre_open/stoploss/post_close）进入此 context。
        E2E 用 orchestrator 在 context 内设 eng._gw = gw（_order_direction 内存兜底）。
        """
        gw = MagicMock()
        gw._connected = True
        gw.is_client_ready = lambda *a, **kw: True  # gate 放行
        gw.is_locked = False
        gw._lock_down = False
        gw._orders = {}
        self._last_gw = gw  # simulate_submit 方向锚点注册（_order_direction 内存兜底）
        # query_asset 注入：透传 up_to 时点给 simulate_query_asset（full_run 集成修复 · 根因 3）。
        _cb_up_to = up_to
        gw.query_asset = AsyncMock(
            side_effect=lambda: self.simulate_query_asset(t_date, _cb_up_to))
        gw._fetch_broker_positions = AsyncMock(return_value=self.simulate_fetch_positions(t_date))
        gw.query_orders = AsyncMock(return_value=[])
        gw.cancel_order = AsyncMock(return_value=None)
        gw._confirm_cancelled = AsyncMock(return_value=True)
        # post_close 真身调 reconcile_job.run_reconcile → await gw.sync_positions(...)
        # （BaseExecutionGateway 模板方法）。补 AsyncMock 返零漂 ReconciliationResult。
        from trading.reconcile_job import ReconciliationResult
        gw.sync_positions = AsyncMock(
            return_value=ReconciliationResult(
                matched=[], drifted=[], only_local=[], only_broker=[],
                max_abs_drift=0.0, is_ok=True))

        async def _submit_mock(order, *, confirm=True):
            side = str(order.side).upper()
            result = self.simulate_submit(
                {"symbol": order.symbol, "qty": order.qty, "side": side,
                 "price": getattr(order, "price", None)},
                t_date, up_to)
            return result

        # W1-A/T2-Task20 fix（同 aeb02036 fix(test_e2e_trading_flow) 根因）：
        # phases 顶部 ``from trading.gateway_service/io.breaker import`` 在 phases 模块
        # 命名空间绑定【本地引用】——patch ``trading.engine.X``（re-export 别名）不影响
        # phases 的 from…import 本地绑定，调用方 ``gw = get_gateway()`` 仍读原函数返 None
        # → e2e orchestrator 三阶段（pre_open/stoploss/post_close）"网关未装配"早返 skip。
        # broker.attach 是 e2e 单一 gw 注入点（orchestrator.py L157/209/240 三处 with 包裹），
        # 必须把 4 个 phases 调用方模块（pre_open/stop_loss/post_close/exit）的本地引用同步 patch。
        # engine.X 保 patch：_health_guard（engine.py 实例方法）读 engine 模块全局 ``get_gateway``
        # （engine.py:367 自有定义），与 phases 本地引用两口子分离（Task 7 fix 同款双口子）。
        # _cancel_all_open_orders 仅 pre_open/post_close 读（stop_loss/exit 不 import）。
        with patch("trading.engine.get_gateway", lambda: gw), \
             patch("trading.engine._submit", _submit_mock), \
             patch("trading.phases.pre_open.get_gateway", lambda: gw), \
             patch("trading.phases.pre_open._submit", _submit_mock), \
             patch("trading.phases.pre_open._cancel_all_open_orders",
                   AsyncMock(return_value={"cancelled": 0, "unconfirmed": 0})), \
             patch("trading.phases.stop_loss.get_gateway", lambda: gw), \
             patch("trading.phases.stop_loss._submit", _submit_mock), \
             patch("trading.phases.post_close.get_gateway", lambda: gw), \
             patch("trading.phases.post_close._cancel_all_open_orders",
                   AsyncMock(return_value={"cancelled": 0, "unconfirmed": 0})), \
             patch("trading.phases.exit._submit", _submit_mock):
            yield gw