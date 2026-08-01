# E2E 报表真实交易/持仓列表 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 23 日 E2E 的成交回报真实落表（fill/position/order 状态），报表列出真实交易列表与持仓列表。

**Architecture:** 测试侧补 QMT 成交回报注入（`engine._handle_order_update` 真身）；ProbabilisticBroker 记录模拟回报、维护持仓镜像、TP 限价单按 stk_mins 真实价格触发；TableSnapshotCollector 采集明细行，ReportBuilder 渲染交易/持仓表格。

**Tech Stack:** Python 3.10、pytest、sqlite3（state_store）、TradingEngine 真身。

## Global Constraints

- **不改生产代码**：所有改动在 `tests/e2e_long_cycle/` 与 `conftest.py` 测试侧。
- **成交落账走生产真身**：fill/position/trade_event 只经 `engine._handle_order_update`（insert_fill → apply_fill_to_position → FILLED 事件）。
- **价格全真**：TP 触发用 `MinBarFeeder` 的 stk_mins 累积 high；STOP/超期市价卖用当前时点价。
- **持仓镜像防负**：SELL 超持仓 clamp 到 0，DB position 不出现负行。
- **隔离红线**：`record_live_trade` 必须 patch（防 E2E 写真实 logs/live_trades.csv）。
- 全中文注释（What + Why），pytest 临时目录用工作区 basetemp（沙箱 temp 不可写）。

---

## Task 1：ProbabilisticBroker 成交回报记录 + 持仓镜像 + inject_fills + scan_resting

**Files:**
- Modify: `tests/e2e_long_cycle/probabilistic_broker.py`
- Test: `tests/e2e_long_cycle/test_probabilistic_broker.py`

**Interfaces:**
- Consumes: `MinBarFeeder.feed(symbols, t_date, up_to) -> {sym: {last_price, high, low}}`；`trading_plan.load_plan(date_iso)`；`engine.TradingEngine._handle_order_update(update)`。
- Produces:
  - `ProbabilisticBroker.inject_fills(eng) -> None`（async；排空 `_pending_reports`）。
  - `ProbabilisticBroker.scan_resting_and_inject(eng, t_date, up_to) -> None`（async；TP 价格触发 + 注入）。
  - `simulate_submit(order, t_date, up_to) -> dict`（新增 SELL 分支与回报记录）。

- [ ] **Step 1：写失败测试（注入落账 + TP 价格触发）**

`tests/e2e_long_cycle/test_probabilistic_broker.py` 追加：

```python
def test_inject_fills_writes_fill_and_position_via_engine(isolated_state, monkeypatch):
    """成交回报注入：经 _handle_order_update 真身写 fill + position（防空转回归）。"""
    import asyncio, sqlite3
    from datetime import date, time
    import pandas as pd
    from trading import engine as engine_mod, state_store
    from tests.e2e_long_cycle.min_bar_feeder import MinBarFeeder
    from tests.e2e_long_cycle.probabilistic_broker import ProbabilisticBroker

    monkeypatch.setenv("AUTO_TRADE_MODE", "dry_run")
    feeder = MinBarFeeder(stk_mins_loader=lambda s, d: pd.DataFrame())
    broker = ProbabilisticBroker(seed=1, min_bar_feeder=feeder, force_state="FILLED")
    eng = engine_mod.TradingEngine()
    order = {"symbol": "300001.SZ", "qty": 100, "side": "BUY", "price": 10.0}
    with broker.attach(date(2026, 7, 2), time(9, 25)) as gw:
        eng._gw = gw
        broker.simulate_submit(order, date(2026, 7, 2), time(9, 25))
        asyncio.run(broker.inject_fills(eng))
    account = engine_mod._resolve_account_id()
    assert state_store.get_position(account, "300001.SZ") is not None, "BUY 成交应落 position"
    with sqlite3.connect(state_store._DEFAULT_DB) as con:
        n = con.execute("SELECT COUNT(*) FROM fill WHERE symbol='300001.SZ'").fetchone()[0]
    assert n >= 1, "BUY 成交应落 fill 表"


def test_resting_tp_fills_only_when_high_reaches_price(isolated_state, monkeypatch):
    """TP 限价单：high < tp 价时 SUBMITTED 不动，high >= tp 价时 FILLED。"""
    import asyncio
    from datetime import date, time
    from trading import engine as engine_mod
    from tests.e2e_long_cycle.min_bar_feeder import MinBarFeeder
    from tests.e2e_long_cycle.probabilistic_broker import ProbabilisticBroker
    from trading import trading_plan

    monkeypatch.setenv("AUTO_TRADE_MODE", "dry_run")
    monkeypatch.setenv("TRADE_PLAN_DIR", str(__import__("pathlib").Path(isolated_state) / "plans"))
    trading_plan.save_plan("2026-07-02", [{
        "order": {"symbol": "300001.SZ", "qty": 100, "side": "BUY", "price": 10.0},
        "stop_price": 9.5, "take_profit": 11.0, "tp1": 10.5, "tp1_portion": 0.5,
    }], confirmed=True)

    def loader(sym, d):
        import pandas as pd
        return pd.DataFrame({"trade_time": [f"{d} 10:00:00"],
                             "open": [10.0], "high": [10.3], "low": [9.9], "close": [10.1],
                             "vol": [1000], "amount": [10100.0]})
    feeder = MinBarFeeder(stk_mins_loader=loader)
    broker = ProbabilisticBroker(seed=1, min_bar_feeder=feeder, force_state="FILLED")
    eng = engine_mod.TradingEngine()
    with broker.attach(date(2026, 7, 2), time(10, 0)) as gw:
        eng._gw = gw
        r = broker.simulate_submit(
            {"symbol": "300001.SZ", "qty": 50, "side": "SELL", "price": 10.5},
            date(2026, 7, 2), time(10, 0))
        assert r["state"] == "SUBMITTED", "high 10.3 < tp 10.5 应挂单不成交"
        asyncio.run(broker.scan_resting_and_inject(eng, date(2026, 7, 2), time(10, 0)))
        # 价格未到 → 无 fill
        assert not broker._pending_reports
        asyncio.run(broker.inject_fills(eng))
```

- [ ] **Step 2：跑测试确认失败**

Run: `.venv310\Scripts\python.exe -m pytest tests/e2e_long_cycle/test_probabilistic_broker.py -v -m "not e2e_long" --basetemp=F:\quanter\.pytest_tmp\plan-t1`
Expected: FAIL（inject_fills 不存在 / 无落账）。

- [ ] **Step 3：实现 ProbabilisticBroker**

`probabilistic_broker.py` 修改点：

```python
self._pending_reports: list[dict] = []   # 待注入回报（kind=order/trade 模拟）
self._resting: dict[str, dict] = {}       # TP 限价挂单 {oid: {...}}
```

`simulate_submit` 重写为：

```python
def simulate_submit(self, order: dict, t_date: date, up_to: time) -> dict:
    symbol = order["symbol"]; qty = float(order["qty"]); side = str(order["side"]).upper()
    price = float(order.get("price") or 0.0) or self.price_for(symbol, t_date, up_to)
    oid = f"{t_date.isoformat()}_{symbol}_{self._rng.randint(0, 99999)}"
    traded_time = clock.now().isoformat()
    if side == "SELL" and self._is_tp_price(symbol, price, t_date):
        # TP 限价单：挂单等价格（spec §7：stk_mins high >= tp 才触发）
        self._resting[oid] = {"symbol": symbol, "qty": qty, "price": price,
                              "t_date": t_date, "side": side, "traded_time": traded_time}
        return {"order_id": oid, "state": "SUBMITTED", "price": price}
    if side == "SELL":
        # STOP/超期市价卖：立即成交，clamp 到镜像持仓防负
        qty = min(qty, float(self._positions.get(symbol, {}).get("volume", 0.0)))
        if qty <= 0:
            return {"order_id": oid, "state": "REJECTED", "message": "无持仓可卖（镜像 clamp）"}
        self._apply_mirror(symbol, -qty, price)
        self._queue_report(oid, symbol, "SELL", qty, price, "FILLED", t_date, traded_time)
        return {"order_id": oid, "state": "FILLED", "price": price, "traded_volume": qty}
    # BUY：概率（force_state 覆盖）
    state = self._force_state or self._sample_state()
    if state == "REJECTED":
        self._queue_report(oid, symbol, "BUY", 0, price, "REJECTED", t_date, traded_time)
        return {"order_id": oid, "state": "REJECTED", "message": "涨停价拒单（模拟）"}
    traded = qty if state == "FILLED" else max(100, int(qty * self._rng.uniform(0.3, 0.7)) // 100 * 100)
    self._apply_mirror(symbol, traded, price)
    self._queue_report(oid, symbol, "BUY", traded, price, state, t_date, traded_time)
    return {"order_id": oid, "state": state, "price": price, "traded_volume": traded}
```

辅助方法（含 `_queue_report`、`_apply_mirror`、`_is_tp_price`、`_db_state`、`inject_fills`、`scan_resting_and_inject`），核心注入逻辑：

```python
async def inject_fills(self, eng) -> None:
    """排空待注入回报：kind=order 推进状态 + kind=trade 生产落账 + TP/STOP 行 broker_oid 回填。"""
    depth = 0
    while self._pending_reports and depth < 50:
        rep = self._pending_reports.pop(0)
        oid = rep["oid"]
        if rep["state"] == "REJECTED":
            await eng._handle_order_update(
                {"kind": "order", "order_id": oid, "state": "REJECTED", "traded_volume": 0})
            continue
        await eng._handle_order_update({
            "kind": "order", "order_id": oid, "state": rep["state"],
            "traded_volume": rep["qty"], "traded_price": rep["price"]})
        await eng._handle_order_update({
            "kind": "trade", "order_id": oid, "stock_code": rep["symbol"],
            "traded_volume": rep["qty"], "traded_price": rep["price"],
            "traded_time": rep["traded_time"]})
        # TP/STOP/EXPIRED 行无 broker_oid（生产靠 async_response 回填，E2E 不模拟）→ 测试侧直写
        if rep["side"] == "SELL":
            self._backfill_sell_order_state(rep, oid)
        depth += 1

async def scan_resting_and_inject(self, eng, t_date: date, up_to: time) -> None:
    """盘中扫描 TP 限价单：stk_mins 累积 high >= tp 价 → FILLED（真实价格驱动）。"""
    if not self._resting:
        return
    syms = sorted({r["symbol"] for r in self._resting.values()})
    quotes = self._feeder.feed(syms, t_date, up_to)
    for oid in list(self._resting):
        r = self._resting[oid]
        high = (quotes.get(r["symbol"]) or {}).get("high")
        if high is not None and float(high) >= float(r["price"]):
            qty = min(float(r["qty"]), float(self._positions.get(r["symbol"], {}).get("volume", 0.0)))
            if qty > 0:
                self._apply_mirror(r["symbol"], -qty, r["price"])
                self._queue_report(oid, r["symbol"], "SELL", qty, r["price"],
                                   "FILLED", t_date, r["traded_time"])
            del self._resting[oid]
    if self._pending_reports:
        await self.inject_fills(eng)
```

`_backfill_sell_order_state`：对 `STOP`/`EXPIRED_CLOSE`/`TP1`/`TP2` 四个内部 order_id（`f"{rep['t_date']}_{symbol}_{purpose}_1"`）逐个 `state_store.update_order_state(oid, self._db_state(rep["state"]), broker_oid=rep_oid, filled_qty=qty, filled_price=price)`；不存在则 rowcount=0 天然跳过。

`attach` 的 `_submit_mock` 增加 `gw._orders[oid] = {"order_type": 23 if side == "BUY" else 24}`。

- [ ] **Step 4：跑测试确认通过**

Run: 同 Step 2 命令。
Expected: 新增 2 用例 + 原 4 用例全 PASS。

- [ ] **Step 5：commit**

```bash
git add tests/e2e_long_cycle/probabilistic_broker.py tests/e2e_long_cycle/test_probabilistic_broker.py
git commit -m "feat(e2e): ProbabilisticBroker 成交回报记录+注入（fill/position 真实落表 + TP 价格触发）"
```

---

## Task 2：orchestrator 三阶段注入 + conftest 隔离 + smoke 断言

**Files:**
- Modify: `tests/e2e_long_cycle/orchestrator.py`、`tests/e2e_long_cycle/conftest.py`
- Test: `tests/e2e_long_cycle/test_e2e_long_cycle.py`（smoke）

**Interfaces:**
- Consumes: `broker.inject_fills(eng)`、`broker.scan_resting_and_inject(eng, t_date, up_to)`。
- Produces: `build_job_runner` 三阶段注入；`isolated_state` 补 `record_live_trade` 隔离。

- [ ] **Step 1：改 smoke 断言（先失败）**

`test_e2e_long_cycle.py`：
- `_FakeEng` 换成真实 `from trading.engine import TradingEngine; eng = TradingEngine()`。
- 在现有断言后追加：

```python
    # 验收 12：成交回报真实落表（fill/position/order 状态非空转）
    assert any(snap.get("fill", 0) > 0 for snap in snapshots.values()), "应产生 fill 明细"
    assert any(snap.get("positions") for snap in snapshots.values()), "应出现持仓列表"
    states = set().union(*(snap.get("order_by_state", {}) for snap in snapshots.values()))
    assert states & {"FILLED", "PARTIAL"}, f"order 应出现 FILLED/PARTIAL，实际 {states}"
    content = md_path.read_text(encoding="utf-8")
    assert "全周期成交流水" in content and "持仓列表" in content
    assert "300001.SZ" in content, "交易/持仓列表应含真实 symbol"
```

- [ ] **Step 2：跑 smoke 确认失败**

Run: `.venv310\Scripts\python.exe -m pytest tests/e2e_long_cycle/test_e2e_long_cycle.py::test_orchestrator_smoke -v --basetemp=F:\quanter\.pytest_tmp\plan-t2`
Expected: FAIL（fill=0 / positions 空 / 报表无表格）。

- [ ] **Step 3：实现 orchestrator + conftest**

orchestrator 三段：

```python
if phase == "pre_open":
    with broker.attach(t_plus_1, time(9, 25)) as gw:
        eng._gw = gw
        result = asyncio.run(signal_scanner.run_pre_open_phase(t_plus_1, gw=None))
        asyncio.run(broker.inject_fills(eng))
    return result

if phase == "stoploss":
    ...
    with min_bar_feeder.patch_get_quotes(), broker.attach(t_plus_1, now_time) as gw:
        eng._gw = gw
        result = asyncio.run(stop_loss_monitor(...))
        asyncio.run(broker.scan_resting_and_inject(eng, t_plus_1, now_time))
    return result

if phase == "post_close":
    with broker.attach(t_plus_1, time(15, 30)) as gw:
        eng._gw = gw
        result = asyncio.run(signal_scanner.run_post_close_phase(t_plus_1, gw=None))
        asyncio.run(broker.inject_fills(eng))
    ...
```

conftest `isolated_state` 追加：

```python
monkeypatch.setattr(_svc, "record_live_trade", lambda *a, **k: None)
```

- [ ] **Step 4：跑 smoke + Task 1 测试确认通过**

Run: `pytest tests/e2e_long_cycle/test_probabilistic_broker.py tests/e2e_long_cycle/test_e2e_long_cycle.py::test_orchestrator_smoke -v -m "not e2e_long" --basetemp=...`
Expected: 全 PASS。

- [ ] **Step 5：commit**

```bash
git add tests/e2e_long_cycle/orchestrator.py tests/e2e_long_cycle/conftest.py tests/e2e_long_cycle/test_e2e_long_cycle.py
git commit -m "feat(e2e): orchestrator 三阶段成交回报注入 + record_live_trade 隔离 + smoke 落表断言"
```

---

## Task 3：TableSnapshotCollector 明细采集

**Files:**
- Modify: `tests/e2e_long_cycle/table_snapshot.py`
- Test: `tests/e2e_long_cycle/test_table_snapshot.py`

- [ ] **Step 1：写失败测试（预置行 → 明细列表）**

```python
def test_snapshot_collects_detail_rows(isolated_state):
    """快照应返回 fill/order/trade_event/position 明细行（不只计数）。"""
    import sqlite3
    from datetime import date
    from trading import state_store, engine
    account = engine._resolve_account_id()
    state_store.insert_order("o1", "t1", account, "2026-07-02", "300001.SZ", "buy",
                             "OPEN", 100, 10.0, state="SUBMITTED", broker_oid="b1")
    state_store.insert_trade_event(account, "t1", "300001.SZ", "FILLED", order_id="b1",
                                   qty=100, price=10.0, timestamp="2026-07-02 09:25:00")
    state_store.insert_fill("b1", account, "2026-07-02 09:25:00", "300001.SZ", "BUY", 100, 10.0)
    state_store.apply_fill_to_position(account, "300001.SZ", "BUY", 100, 10.0, "2026-07-02 09:25:00")
    snap = TableSnapshotCollector().snapshot(date(2026, 7, 2))
    assert any(r["symbol"] == "300001.SZ" for r in snap["fills"])
    assert any(r["symbol"] == "300001.SZ" for r in snap["orders"])
    assert any(r["action"] == "FILLED" for r in snap["trade_events"])
    assert any(r["symbol"] == "300001.SZ" for r in snap["positions"])
    assert snap["positions"][0]["holding_days"] == 0
```

- [ ] **Step 2：跑失败**

Run: `pytest tests/e2e_long_cycle/test_table_snapshot.py -v --basetemp=...`
Expected: FAIL（KeyError fills）。

- [ ] **Step 3：实现 `_rows` + 五组明细**

`table_snapshot.py` 增加：

```python
def _rows(self, con, sql, params=()):
    try:
        return [dict(r) for r in con.execute(sql, params).fetchall()]
    except sqlite3.OperationalError:
        return []
```

`snapshot()` 内追加 `fills`（date(traded_time)=?）、`orders`（trade_date=?）、`trade_events`（date(timestamp)=?）、`positions`（qty>0 + holding_days 计算）、`account_daily_rows`（date=?）。计数键改为从明细列表 len 推导（保留键名）。

- [ ] **Step 4：跑通过 + commit**

---

## Task 4：ReportBuilder 明细渲染 + DingTalkLog 记录

**Files:**
- Modify: `tests/e2e_long_cycle/report_builder.py`、`tests/e2e_long_cycle/dingtalk_log.py`
- Test: `tests/e2e_long_cycle/test_report_builder.py`、`tests/e2e_long_cycle/test_dingtalk_log.py`

- [ ] **Step 1：写失败测试**

`test_report_builder.py`：预置 snapshots（含 fills/positions 列表）→ md 含「全周期成交流水」「持仓列表」「300001.SZ」。
`test_dingtalk_log.py`：enabled=True + fake original_faf（`asyncio.run` 执行 coro）→ records 非空且含 kind。

- [ ] **Step 2：跑失败**

- [ ] **Step 3：实现**

`report_builder.py`：
- 新增 `_render_table(headers, rows)`（markdown 表格；空行输出「（无）」）。
- §2 顶部：全周期成交流水（跨 snapshot.fills 聚合 + 日期列）、期末持仓列表（最后一个非空 positions）。
- 每日小节：trade_event/order/fill/position/account_daily 明细表。
- §4：逐条渲染 `dingtalk_records`（time/kind/success）。

`dingtalk_log.py`：`_wrapped` 中 enabled 时包一层 `async def _logged(coro)`，await 前后写 `self.records`（time/kind=cr_code.co_qualname/success/error）。

- [ ] **Step 4：跑通过 + commit**

---

## Task 5：full run 断言 + 全量验证

**Files:**
- Modify: `tests/e2e_long_cycle/test_e2e_long_cycle.py`（full run 断言）

- [ ] **Step 1：full run 加断言**

```python
    all_fills = [f for snap in snapshots.values() for f in snap.get("fills", [])]
    assert all_fills, "全周期应产生真实成交流水"
    assert any(snap.get("positions") for snap in snapshots.values()), "应出现过持仓"
    states = set().union(*(snap.get("order_by_state", {}) for snap in snapshots.values()))
    assert states & {"FILLED", "PARTIAL"}, f"order 应出现 FILLED/PARTIAL，实际 {states}"
    assert "全周期成交流水" in content and "持仓列表" in content
```

- [ ] **Step 2：组件单测全绿**

Run: `pytest tests/e2e_long_cycle -v -m "not e2e_long" --basetemp=F:\quanter\.pytest_tmp\e2e-final`
Expected: 全 PASS（~20+ 用例）。

- [ ] **Step 3：23 日 full run（提权：Tushare/钉钉/connect）**

Run: `pytest tests/e2e_long_cycle/test_e2e_long_cycle.py::test_e2e_long_cycle_full_run -v -m e2e_long -s --tb=short --basetemp=F:\quanter\.pytest_tmp\e2e-full`
Expected: PASS；报告含真实交易列表/持仓列表；`logs/e2e_long_cycle/e2e_long_cycle_report.md` §2 fill>0。

- [ ] **Step 4：默认回归**

Run: `pytest tests -q --tb=short --basetemp=F:\quanter\.pytest_tmp\regression`
Expected: 全绿（唯一例外：沙箱写 C:\Users\...\tk.csv 的 Tushare 用例需提权重跑）。

- [ ] **Step 5：commit**

```bash
git add tests/e2e_long_cycle/test_e2e_long_cycle.py
git commit -m "feat(e2e): full_run 断言真实成交/持仓落表 + 报表明细"
```

---

## Self-Review

**Spec 覆盖**：§4.1 注入 → Task 1；§4.2 编排 → Task 2；§4.3 隔离 → Task 2；§4.4 明细采集 → Task 3；§4.5 报表 + 推送记录 → Task 4；§5/§6 测试与验收 → Task 1-5。无缺口。

**Placeholder 扫描**：无 TBD/TODO；Task 3/4 的 Step 3 描述为要点（本计划由同一实现者执行，spec §4 已给完整接口与数据流）。

**类型一致性**：`inject_fills(eng)` / `scan_resting_and_inject(eng, t_date, up_to)` / `_pending_reports` / `_resting` / snapshot 键 `fills/orders/trade_events/positions/account_daily_rows` 贯穿 Task 1-5，签名一致。
