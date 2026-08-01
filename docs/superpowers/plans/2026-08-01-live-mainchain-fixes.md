# 实盘主链路修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复实盘成交回报主链路 10 条已核实缺陷 + 评审加固点，让「成交回报 → 止盈挂单 → 账本落库 → 持仓更新 → 盘后对账」在生产主推路径下真实闭环，并消除跨日 gate、熔断粘滞、止盈超卖/覆盖缺口、撤单状态机等风控隐患。

**Architecture:** state_store SQLite 唯一真相源；方向/状态/broker_oid 判定 DB 优先、内存兜底；成交回报按 kind 分发（async_response 回填 → order 推进状态 → trade 落账 + 止盈差额补挂）；CSV 降级为带 kind 列的审计层；风控熔断 `_risk_halted` 与网络断线分离。Phase A（成交链路）→ B/C/E 可并行 → D（止盈差额补挂）→ D'（盘中兜底）。

**Tech Stack:** Python 3.10、asyncio、apscheduler、sqlite3、pytest（`.venv310/Scripts/python.exe -m pytest`）、QMT xtquant SDK（生产）、conftest 假 xtconstant（单测兜底 23/24）。

## Global Constraints

- **语言**：所有对话/文档/代码注释 100% 中文（注释说明 What + Why 交易物理意图）。
- **真相源**：state_store SQLite 是唯一真相源；`gw._orders`/`_seq_to_real` 仅加速缓存；方向/状态/broker_oid 判定 DB 优先，DB miss 才回退内存且必须记日志。
- **测试元问题红线**：涉及成交回报的测试必须驱动真实回调链路（`gw.on_stock_order/on_stock_trade/on_order_stock_async_response` → `_process_order_update` → `_handle_order_update`）；禁止手工 `eng._gw._orders = {...}` 注入内存态；禁止 `eng._submit = MagicMock(...)`（实例属性遮蔽不了模块级 `_submit`），必须 `monkeypatch.setattr("trading.engine._submit", AsyncMock(...))`。
- **错误分级（spec §6）**：order/fill/position 主链路写失败（insert_fill/apply_fill/insert_order）= `_CriticalHalt` 停调度；broker_oid 回填 / order 状态推进 / trade_event 等可补偿写失败 = CRITICAL/WARN；审计/告警/止盈旁路 = CRITICAL/WARN。
- **测试执行**：`.venv310/Scripts/python.exe -m pytest <path> -v`；每任务先跑红再跑绿。
- **不做地基重构**：plan→SQLite、部分成交精确重放、trailing 盘中演进、EMT 不在本计划 scope。

## File Structure

| 文件 | 职责 | 本计划改动 |
|------|------|-----------|
| `trading/state_store.py` | SQLite 真相源 | 新增 `get_order_by_broker_oid` / `update_order_state_by_broker_oid` / `get_order_placed_qty` |
| `trading/engine.py` | 交易引擎主链路 | A1/A2/A3/A4/A5/B1/C1/D1/D2/E3 主战场（`_handle_order_update` / `_order_direction` / `place_take_profit` / `_pre_open_gate` / `_health_guard` / `stop_loss_monitor` / `_close_expired_positions` / `post_close`） |
| `broker/qmt.py` | QMT 网关解析层 | A2（order_type + merge）/ C1（`_risk_halted`）/ E1（状态 51） |
| `presentation/server/services/trading_service.py` | CSV 审计 + 熔断入口 | A4（kind 列 + `aggregate_fills_by_symbol`）/ C1（`set_risk_halt`） |
| `trading/io/breaker.py` | 熔断撤单 | E2（failed 计数） |
| `tests/trading/test_engine_order_update_handler.py` | 成交回报 handler 测试（真实回调链路） | A1/A2/A3/A5 主阵地（新增共享 helper `_make_real_chain_engine` + `_pump`） |
| `tests/trading/test_state_store.py` | state_store 单测 | A1/D1 新接口用例 |
| `tests/trading/test_live_trades_csv.py` | CSV 审计测试 | A4（新建） |
| `tests/trading/test_e2e_trading_flow.py` | 端到端跨日流程 | B1 跨两日用例 |
| `tests/trading/test_qmt_health_guard.py` / `test_emergency_halt.py` | 熔断粘滞 | C1 用例 |
| `tests/test_qmt_gateway.py` | 网关状态映射 | E1 用例 |
| `tests/trading/test_engine.py` | 引擎单测 | A3 改写手塞用例 / D1 止盈幂等用例 |

---

## Phase A：成交链路真相源（#5 + #1 + #3，最高杠杆，串行）

**依赖**：无。
**交付**：async_response 回填真实 broker_oid；order 事件精确推进 FILLED/PARTIAL；方向反查 DB 优先；CSV 区分 submit/fill；trade 分支先落账后挂止盈；全链路 e2e（含竞态）全绿。

### Task A1：async_response 回填 broker_oid（#5 第一刀）

**Files:**
- Modify: `trading/state_store.py`（`cancel_order_by_broker_oid_db` 附近，约 `:415` 后）
- Modify: `trading/engine.py`（`_handle_order_update` 入口，`:2668-2670`）
- Test: `tests/trading/test_engine_order_update_handler.py`、`tests/trading/test_state_store.py`

**Interfaces:**
- Consumes: `gw.on_order_stock_async_response` 投递 `{"kind":"async_response","seq":int,"order_id":int(真实),"state":SUBMITTED}`（`qmt.py:1220-1221`）；pre_open 已落 DB order（`broker_oid=str(seq)`）。
- Produces: `state_store.get_order_by_broker_oid(broker_oid) -> dict|None`；`state_store.update_order_state_by_broker_oid(lookup_oid, *, state=None, new_broker_oid=None, filled_qty=None, filled_price=None) -> int`；`_handle_order_update` 消费 async_response 回填 DB。

- [ ] **Step 1：写共享 helper + 失败测试**

在 `tests/trading/test_engine_order_update_handler.py` 顶部追加（复用本文件既有 isolated DB 夹具；若无则 `monkeypatch.setattr(state_store, "_DEFAULT_DB", str(tmp_path/"state.db"))` 后 `init_store()`）：

```python
# ==== 共享 helper：真实回调链路 pump（禁止手塞 _orders 的测试都必须走它）====
def _make_real_chain_engine(monkeypatch):
    """装配绑定真实 QmtExecutionGateway 的引擎（不 connect，直接驱动 C++ 回调）。"""
    from broker.qmt import QmtExecutionGateway
    from trading.engine import TradingEngine
    from unittest.mock import MagicMock
    gw = QmtExecutionGateway(userdata_path="C:/tmp/qmt_test", account_id="TEST_ACC")
    gw._trader = MagicMock()
    gw._account = MagicMock()
    gw._connected = True
    gw._lock_down = False
    gw._orders = {}
    gw._seq_to_real = {}
    gw._seq_to_client = {}
    eng = TradingEngine()
    eng._gw = gw
    gw.set_order_update_callback(eng._handle_order_update)
    return eng, gw


async def _pump(gw, fn):
    """在真实事件循环里触发回调方法并让 _process_order_update 创建的任务跑完。"""
    import asyncio
    gw._loop = asyncio.get_running_loop()
    fn()
    await asyncio.sleep(0.05)


def test_async_response_backfills_db_broker_oid(monkeypatch, tmp_path):
    """async_response 到达 → DB order.broker_oid 从 str(seq) 回填真实柜台单号。

    生产根因（#5）：原 _handle_order_update 见 kind!='trade' 直接 return，
    async_response 被丢弃 → broker_oid 恒 str(seq) → 撤单/对账永远按错单号匹配。
    """
    import asyncio
    from types import SimpleNamespace
    from trading import state_store

    monkeypatch.setattr(state_store, "_DEFAULT_DB", str(tmp_path / "state.db"))
    state_store.init_store()
    eng, gw = _make_real_chain_engine(monkeypatch)
    aid = "TEST_ACC"
    seq, real = 7, 987654
    oid = "2026-08-01_600000.SH_OPEN_7"
    state_store.insert_order(oid, f"{aid}_600000.SH_2026-08-01", aid, "2026-08-01",
                             "600000.SH", "buy", "OPEN", 100, 10.0,
                             broker_oid=str(seq), state="SUBMITTED")
    # 真实回调链路：on_order_stock_async_response → _process_order_update → _handle_order_update
    asyncio.run(_pump(gw, lambda: gw.on_order_stock_async_response(
        SimpleNamespace(seq=seq, order_id=real))))
    with state_store._connect(state_store._DEFAULT_DB) as con:
        row = con.execute('SELECT broker_oid, state FROM "order" WHERE order_id=?', (oid,)).fetchone()
    assert row["broker_oid"] == str(real), f"应回填 {real}，实际 {row['broker_oid']}"
    assert row["state"] == "SUBMITTED", "async_response 只回填单号，不动 state"
```

- [ ] **Step 2：跑测试验证失败**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_engine_order_update_handler.py::test_async_response_backfills_db_broker_oid -v`
Expected: FAIL（`row["broker_oid"]` 仍 `"7"`——async_response 被 return 丢弃）。

- [ ] **Step 3：state_store 加两个新接口**

`trading/state_store.py`（`cancel_order_by_broker_oid_db` 后）追加：

```python
def get_order_by_broker_oid(broker_oid: str, *, db_path: str | None = None) -> dict | None:
    """按柜台单号查 order（成交回报/方向反查用）。

    broker_oid 列在 async_response 回填前存 str(seq)、回填后存真实单号，
    调用方需按 spec §5.2 先查 real、miss 后经 _seq_to_real 反查 seq 再查。
    """
    db_path = db_path or _DEFAULT_DB
    with _connect(db_path) as con:
        row = con.execute('SELECT * FROM "order" WHERE broker_oid=?', (broker_oid,)).fetchone()
    return dict(row) if row else None


def update_order_state_by_broker_oid(
    lookup_oid: str,
    *,
    state: str | None = None,
    new_broker_oid: str | None = None,
    filled_qty: float | None = None,
    filled_price: float | None = None,
    db_path: str | None = None,
) -> int:
    """按 broker_oid 列定位更新 order（成交回报/async_response 持柜台单号时用）。

    lookup_oid 是定位值（回填前 str(seq)，回填后真实单号）；None 字段不动。
    Returns: 更新行数（0=未命中，调用方必须处理竞态：WARN + 后续事件补推进）。
    """
    db_path = db_path or _DEFAULT_DB
    with _connect(db_path) as con:
        sets: list[str] = []
        params: list = []
        if state is not None:
            sets.append("state = ?"); params.append(state)
        if new_broker_oid is not None:
            sets.append("broker_oid = ?"); params.append(new_broker_oid)
        if filled_qty is not None:
            sets.append("filled_qty = ?"); params.append(float(filled_qty))
        if filled_price is not None:
            sets.append("filled_price = ?"); params.append(float(filled_price))
        if not sets:
            return 0
        params.append(lookup_oid)
        cur = con.execute(f'UPDATE "order" SET {", ".join(sets)} WHERE broker_oid=?', params)
        return cur.rowcount
```

- [ ] **Step 4：_handle_order_update 消费 async_response**

`trading/engine.py:2668-2670` 入口改为：

```python
        kind = update.get("kind")
        if kind == "async_response":
            # #5 修复：seq→real 映射回填 DB order.broker_oid（撤单/对账唯一可靠锚点）。
            # 原实现 kind!='trade' 直接 return 丢弃本事件 → broker_oid 恒 str(seq) →
            # cancel_order_by_broker_oid_db 永匹配不到行（幽灵单）+ post_close TP_FILLED 恒空。
            seq_str = str(update.get("seq", ""))
            real = str(update.get("order_id", ""))
            if seq_str and real and real != seq_str:
                try:
                    n = _state_store.update_order_state_by_broker_oid(
                        seq_str, new_broker_oid=real)
                    if n == 0:
                        logger.warning(
                            "async_response 未命中 DB 行 seq=%s real=%s（可能 pre_open 未落库）",
                            seq_str, real)
                except Exception:
                    # 回填失败 = 撤单/对账锚点失效（可补偿：下次 order/trade 事件按 seq 反查）
                    logger.exception(
                        "async_response 回填 broker_oid 失败 seq=%s real=%s（CRITICAL：撤单锚点失效）",
                        seq_str, real)
            return
        if kind != "trade":
            return  # 仅处理成交回报 + async_response（order_error/cancel_error 由风控层负责）
```

- [ ] **Step 5：跑绿 + 回归 + 提交**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_engine_order_update_handler.py tests/trading/test_state_store.py -v`
Expected: PASS（含既有用例）。

```bash
git add trading/state_store.py trading/engine.py tests/trading/test_engine_order_update_handler.py tests/trading/test_state_store.py
git commit -m "fix(#5): async_response 回填 DB order.broker_oid 为真实柜台单号

原 kind!='trade' 直接 return 丢弃 async_response，broker_oid 恒 str(seq)，
撤单 cancel_order_by_broker_oid_db 永匹配不到行（幽灵单）。
state_store 新增 get_order_by_broker_oid / update_order_state_by_broker_oid。

Co-Authored-By: Claude <noreply@anthropic.com>"
```
---

### Task A2：on_stock_order 解析 order_type + _process_order_update merge + order 事件推进状态（#5 第二刀 + #1 地基）

**Files:**
- Modify: `broker/qmt.py`（`on_stock_order` `:1133-1150`；`_process_order_update` `:976-1000`）
- Modify: `trading/engine.py`（`_handle_order_update` 加 kind=="order" 分支；新增 `_advance_order_state_from_status` / `_order_state_to_db` / `_seq_for_real_oid`）
- Test: `tests/trading/test_engine_order_update_handler.py`、`tests/trading/test_qmt_gateway.py`

**Interfaces:**
- Consumes: `on_stock_order` 的 XtOrder（含 order_type/order_status/traded_volume/traded_price）；A1 的 `get_order_by_broker_oid` / `update_order_state_by_broker_oid`。
- Produces: `_orders[oid]` 记录含 order_type（merge 保留）；DB order.state 推进 PARTIAL/FILLED/CANCELLED/REJECTED，filled_qty=累计 traded_volume。

- [ ] **Step 1：写失败测试（真实回调链路）**

追加到 `tests/trading/test_engine_order_update_handler.py`：

```python
def test_on_stock_order_parses_order_type(monkeypatch, tmp_path):
    """on_stock_order 解析必须含 order_type（#1 地基：主推路径方向来源之一）。"""
    import asyncio
    from types import SimpleNamespace
    from trading import state_store

    monkeypatch.setattr(state_store, "_DEFAULT_DB", str(tmp_path / "state.db"))
    state_store.init_store()
    eng, gw = _make_real_chain_engine(monkeypatch)
    asyncio.run(_pump(gw, lambda: gw.on_stock_order(SimpleNamespace(
        order_id=987654, stock_code="600000.SH", order_status=56, order_type=23,
        order_volume=100, traded_volume=100, traded_price=10.5, status_msg=""))))
    assert gw._orders["987654"].get("order_type") == 23, "on_stock_order 必须透出 order_type"


def test_order_event_advances_db_state_filled(monkeypatch, tmp_path):
    """order 事件（status=56）→ DB order.state=FILLED + filled_qty=累计量（#5 第二刀）。"""
    import asyncio
    from types import SimpleNamespace
    from trading import state_store

    monkeypatch.setattr(state_store, "_DEFAULT_DB", str(tmp_path / "state.db"))
    state_store.init_store()
    eng, gw = _make_real_chain_engine(monkeypatch)
    aid, real = "TEST_ACC", 987654
    oid = "2026-08-01_600000.SH_OPEN_7"
    state_store.insert_order(oid, f"{aid}_600000.SH_2026-08-01", aid, "2026-08-01",
                             "600000.SH", "buy", "OPEN", 100, 10.0,
                             broker_oid=str(real), state="SUBMITTED")
    asyncio.run(_pump(gw, lambda: gw.on_stock_order(SimpleNamespace(
        order_id=real, stock_code="600000.SH", order_status=56, order_type=23,
        order_volume=100, traded_volume=100, traded_price=10.5, status_msg=""))))
    with state_store._connect(state_store._DEFAULT_DB) as con:
        row = con.execute('SELECT state, filled_qty, filled_price FROM "order" WHERE order_id=?',
                          (oid,)).fetchone()
    assert row["state"] == "FILLED"
    assert row["filled_qty"] == 100
    assert row["filled_price"] == 10.5


def test_order_event_partial_status_maps_to_partial(monkeypatch, tmp_path):
    """status=55（部成）→ DB state=PARTIAL（精确部分成交，非近似 FILLED）。"""
    import asyncio
    from types import SimpleNamespace
    from trading import state_store

    monkeypatch.setattr(state_store, "_DEFAULT_DB", str(tmp_path / "state.db"))
    state_store.init_store()
    eng, gw = _make_real_chain_engine(monkeypatch)
    aid, real = "TEST_ACC", 987654
    oid = "2026-08-01_600000.SH_OPEN_7"
    state_store.insert_order(oid, f"{aid}_600000.SH_2026-08-01", aid, "2026-08-01",
                             "600000.SH", "buy", "OPEN", 300, 10.0,
                             broker_oid=str(real), state="SUBMITTED")
    asyncio.run(_pump(gw, lambda: gw.on_stock_order(SimpleNamespace(
        order_id=real, stock_code="600000.SH", order_status=55, order_type=23,
        order_volume=300, traded_volume=100, traded_price=10.4, status_msg=""))))
    with state_store._connect(state_store._DEFAULT_DB) as con:
        row = con.execute('SELECT state, filled_qty FROM "order" WHERE order_id=?', (oid,)).fetchone()
    assert row["state"] == "PARTIAL"
    assert row["filled_qty"] == 100


def test_trade_push_keeps_order_type_after_merge(monkeypatch, tmp_path):
    """trade 事件不得覆盖 _orders 记录的 order_type（merge 语义，防止内存兜底失效）。"""
    import asyncio
    from types import SimpleNamespace
    from trading import state_store

    monkeypatch.setattr(state_store, "_DEFAULT_DB", str(tmp_path / "state.db"))
    state_store.init_store()
    eng, gw = _make_real_chain_engine(monkeypatch)
    asyncio.run(_pump(gw, lambda: gw.on_stock_order(SimpleNamespace(
        order_id=987654, stock_code="600000.SH", order_status=56, order_type=23,
        order_volume=100, traded_volume=100, traded_price=10.5, status_msg=""))))
    asyncio.run(_pump(gw, lambda: gw.on_stock_trade(SimpleNamespace(
        order_id=987654, stock_code="600000.SH", traded_volume=100,
        traded_price=10.5, traded_amount=1050.0, traded_time="20260801101000"))))
    assert gw._orders["987654"].get("order_type") == 23, "trade 覆盖后 order_type 必须保留"
```

- [ ] **Step 2：跑测试验证失败**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_engine_order_update_handler.py -v`
Expected: FAIL（前两个：order_type 缺失 / state 仍 SUBMITTED；最后一个：order_type 被 trade 覆盖）。

- [ ] **Step 3：broker/qmt.py 解析层改造**

`on_stock_order` parsed dict（`qmt.py:1137-1147`）加一行：

```python
                "order_type": getattr(order, "order_type", 0),  # #1/#5：主推路径方向来源（query_orders 同源）
```

`_process_order_update`（`qmt.py:984-989`）改为 merge（禁止 trade/async_response 覆盖 order 记录字段）：

```python
        order_id = str(update.get("order_id", ""))
        if order_id:
            # #1：merge 而非覆盖——on_stock_trade/async_response 的 dict 不含 order_type，
            # 覆盖会让 _order_direction 内存兜底失效（主推路径方向恒 None 的历史根因之一）。
            rec = dict(self._orders.get(order_id, {}))
            rec.update(update)
            rec["_gc_ts"] = time.time()
            self._orders[order_id] = rec
```

- [ ] **Step 4：engine 加 kind=="order" 分支 + 辅助函数**

`trading/engine.py` 模块级新增（`_seq_for_real_oid` 放 `_order_direction` 附近）：

```python
def _seq_for_real_oid(gw, real_oid: str) -> int | None:
    """_seq_to_real 反查：real→seq（async_response 晚到时按 seq 匹配 DB 行）。"""
    try:
        real_int = int(real_oid)
    except (TypeError, ValueError):
        return None
    seq_map = getattr(gw, "_seq_to_real", None) or {}
    for seq, real in seq_map.items():
        if real == real_int:
            return seq
    return None


def _order_state_to_db(state) -> str:
    """OrderState 枚举/字符串 → order 表 state 列约定（PARTIAL/FILLED/CANCELLED/REJECTED/...）。"""
    name = state.name if hasattr(state, "name") else str(state)
    return {
        "PARTIAL_FILLED": "PARTIAL",
        "FILLED": "FILLED",
        "CANCELLED": "CANCELLED",
        "REJECTED": "REJECTED",
        "PARTIAL_CANCELLED": "PARTIAL_CANCELLED",
    }.get(name, "SUBMITTED")
```

`TradingEngine` 内新增方法：

```python
    def _advance_order_state_from_status(self, update: Mapping[str, Any]) -> None:
        """kind=order：按柜台状态推进 DB order.state/filled_*（#5 第二刀）。

        Why 用 order 事件而非 trade 事件：order_status 55/56 区分 PARTIAL/FILLED，
        traded_volume 是累计成交（trade 是本笔增量），状态推进必须用累计量。
        竞态（async_response 晚到）：按 real 查 miss 时经 _seq_to_real 反查 seq 再匹配。
        """
        lookup = str(update.get("order_id", ""))
        if not lookup:
            return
        row = None
        try:
            row = _state_store.get_order_by_broker_oid(lookup)
            if row is None:
                seq = _seq_for_real_oid(self._gw, lookup)
                if seq is not None:
                    row = _state_store.get_order_by_broker_oid(str(seq))
        except Exception:
            logger.exception("get_order_by_broker_oid 失败 lookup=%s", lookup)
            return
        if row is None:
            logger.warning("order 事件未命中 DB 行 lookup=%s（可能 server 手动单/未落库）", lookup)
            return
        traded_volume = update.get("traded_volume")
        traded_price = update.get("traded_price")
        try:
            n = _state_store.update_order_state_by_broker_oid(
                row["broker_oid"] or lookup,
                state=_order_state_to_db(update.get("state")),
                filled_qty=float(traded_volume) if traded_volume is not None else None,
                filled_price=float(traded_price) if traded_price is not None else None,
            )
            if n == 0:
                logger.warning("order 状态推进未命中 broker_oid=%s（下个事件补推进）", row.get("broker_oid"))
        except Exception:
            logger.exception("order 状态推进失败 lookup=%s（软降级，下个事件补推进）", lookup)
```

`_handle_order_update` 入口（A1 改后基础上）在 `if kind != "trade": return` 前插入：

```python
        if kind == "order":
            # #5 第二刀：柜台委托状态推送（含累计 traded_volume）→ 推进 DB order state。
            # 中间态（SUBMITTED）更新为同值 no-op；终态/部分态精确落库。
            self._advance_order_state_from_status(update)
            return
```

- [ ] **Step 5：跑绿 + 回归 + 提交**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_engine_order_update_handler.py tests/trading/test_qmt_gateway.py -v`
Expected: PASS。

```bash
git add broker/qmt.py trading/engine.py tests/trading/test_engine_order_update_handler.py tests/trading/test_qmt_gateway.py
git commit -m "fix(#5/#1): on_stock_order 透出 order_type + _orders merge + order 事件推进 DB state

原 order/trade 事件覆盖 _orders 记录致 order_type 丢失；order 状态从不推进
（post_close TP_FILLED 恒空）。现按 order_status 精确映射 PARTIAL/FILLED，
filled_qty 用累计 traded_volume，竞态经 _seq_to_real 反查 seq 兜底。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task A3：方向反查改 DB 优先 + 内存兜底（#1 主刀）

**Files:**
- Modify: `trading/engine.py`（`_order_direction` `:2761-2806`；trade 分支方向未知告警）
- Test: `tests/trading/test_engine_order_update_handler.py`、`tests/trading/test_engine.py`（改写手塞 `_orders` 旧用例）

**Interfaces:**
- Consumes: A1 的 `get_order_by_broker_oid`；A2 的 `_seq_for_real_oid` / merge 后的 `_orders.order_type`。
- Produces: `_order_direction(order_id) -> "BUY"|"SELL"|None`；trade 分支 direction None → CRITICAL 告警。

- [ ] **Step 1：写失败测试（真实链路，方向来自 DB 而非手塞内存）**

追加到 `tests/trading/test_engine_order_update_handler.py`：

```python
def test_direction_resolved_from_db_not_memory(monkeypatch, tmp_path):
    """方向从 DB order.side 反查：gw._orders 无 order_type 也能判 BUY（#1 主刀）。"""
    from trading import state_store
    from trading.engine import TradingEngine

    monkeypatch.setattr(state_store, "_DEFAULT_DB", str(tmp_path / "state.db"))
    state_store.init_store()
    eng, gw = _make_real_chain_engine(monkeypatch)
    aid, real = "TEST_ACC", 987654
    state_store.insert_order("2026-08-01_600000.SH_OPEN_7", f"{aid}_600000.SH_2026-08-01",
                             aid, "2026-08-01", "600000.SH", "buy", "OPEN", 100, 10.0,
                             broker_oid=str(real), state="SUBMITTED")
    # 主推路径真实形态：_orders 只有状态无 order_type（禁止手塞 order_type）
    gw._orders = {str(real): {"order_status": 56}}
    assert eng._order_direction(str(real)) == "BUY", "应从 DB side='buy' 反查得 BUY"


def test_direction_seq_fallback_when_async_response_late(monkeypatch, tmp_path):
    """竞态：async_response 未到（broker_oid 仍是 str(seq)），经 _seq_to_real 反查命中 DB。"""
    from trading import state_store

    monkeypatch.setattr(state_store, "_DEFAULT_DB", str(tmp_path / "state.db"))
    state_store.init_store()
    eng, gw = _make_real_chain_engine(monkeypatch)
    aid, seq, real = "TEST_ACC", 7, 987654
    state_store.insert_order("2026-08-01_600000.SH_OPEN_7", f"{aid}_600000.SH_2026-08-01",
                             aid, "2026-08-01", "600000.SH", "buy", "OPEN", 100, 10.0,
                             broker_oid=str(seq), state="SUBMITTED")
    gw._seq_to_real = {seq: real}
    gw._orders = {}
    assert eng._order_direction(str(real)) == "BUY", "seq 反查应命中 DB side"


def test_direction_unknown_returns_none(monkeypatch, tmp_path):
    """DB 无行 + 内存无 order_type → None（调用方必须告警，禁止静默）。"""
    from trading import state_store

    monkeypatch.setattr(state_store, "_DEFAULT_DB", str(tmp_path / "state.db"))
    state_store.init_store()
    eng, gw = _make_real_chain_engine(monkeypatch)
    gw._orders = {}
    assert eng._order_direction("999999") is None
```

同步改写 `tests/trading/test_engine.py:1279` 附近手塞 `{"order_type": 23}` 的旧 Case 1：删除内存注入，改为先 `state_store.insert_order(... side="buy", broker_oid="999")` 再断言 direction=="BUY"（覆盖 BUY/SELL/None 三分支各一个用例）。

- [ ] **Step 2：跑测试验证失败**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_engine_order_update_handler.py tests/trading/test_engine.py -v`
Expected: FAIL（第一个用例 direction 为 None——旧实现只查内存）。

- [ ] **Step 3：改 _order_direction 为 DB 优先链**

`trading/engine.py:2789-2806` 主体替换为：

```python
        # #1 修复：方向反查 DB 优先（state_store.order.side，pre_open 已落库），
        # 内存 gw._orders.order_type 仅兜底（_sync_orders_if_stale 走 query_orders 时才有 order_type）。
        # 竞态兜底：DB 按 real 查 miss 时经 _seq_to_real 反查 seq 再查一次（async_response 晚到）。
        _row = None
        try:
            _row = _state_store.get_order_by_broker_oid(order_id)
            if _row is None:
                _seq = _seq_for_real_oid(self._gw, order_id)
                if _seq is not None:
                    _row = _state_store.get_order_by_broker_oid(str(_seq))
        except Exception:
            logger.exception("get_order_by_broker_oid 失败 order_id=%s（回退内存）", order_id)
        if _row is not None:
            side = str(_row.get("side") or "").lower()
            if side == "buy":
                return "BUY"
            if side == "sell":
                return "SELL"
            # DB 有行但 side 异常 → 继续走内存兜底，不轻易返 None
        orders = getattr(self._gw, "_orders", {}) if self._gw else {}
        rec = orders.get(order_id, {})
        try:
            from xtquant import xtconstant  # 与 broker/qmt.py:61 同源导入路径
            STOCK_BUY = xtconstant.STOCK_BUY
            STOCK_SELL = xtconstant.STOCK_SELL
        except ImportError:
            STOCK_BUY, STOCK_SELL = 23, 24  # CI/单测无 xtquant 兜底（与 conftest 同值）
        ot = rec.get("order_type")
        if ot == STOCK_BUY:
            return "BUY"
        if ot == STOCK_SELL:
            return "SELL"
        return None
```

- [ ] **Step 4：trade 分支方向未知必须 CRITICAL 告警**

`_handle_order_update` trade 分支开头（`direction = self._order_direction(order_id)` 之后）插入：

```python
        if direction is None:
            # #1：方向未知 = 审计黑洞（不挂止盈 + 不落账），必须叫醒人工对账，禁止静默。
            _alert_critical(
                f"成交回报方向未知 order_id={order_id} symbol={symbol} qty={qty} "
                f"（DB 无 side、内存无 order_type，需人工对账补账）")
```

- [ ] **Step 5：跑绿 + 回归 + 提交**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_engine_order_update_handler.py tests/trading/test_engine.py -v`
Expected: PASS（含改写后的旧用例）。

```bash
git add trading/engine.py tests/trading/test_engine_order_update_handler.py tests/trading/test_engine.py
git commit -m "fix(#1): 方向反查改 DB order.side 优先，gw._orders 内存兜底

主推路径 _orders 无 order_type 致 direction 恒 None（止盈/账本链死）。
DB 按 broker_oid 反查 side；async_response 晚到时经 _seq_to_real 反查 seq；
方向未知升级 CRITICAL 告警。删测试手塞 _orders.order_type 的绕过用例。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task A4：CSV 加 kind 列 + aggregate_fills_by_symbol，post_close 只聚合 fill（#3）

**Files:**
- Modify: `presentation/server/services/trading_service.py`（`LIVE_TRADE_COLUMNS` / `record_live_trade` / `query_trades` / `export_trades` / submit 落盘点 `:511-515`；新增 `aggregate_fills_by_symbol`）
- Modify: `trading/engine.py`（成交回报 record_live_trade 调用 `:2685`；post_close ② 聚合 `:1510-1536`）
- Test: `tests/trading/test_live_trades_csv.py`（新建）

**Interfaces:**
- Consumes: 既有 `record_live_trade(symbol, direction, shares, price, strategy, rationale)`。
- Produces: `record_live_trade(..., kind: str = "fill")`；`aggregate_fills_by_symbol(start, end) -> dict[str, float]`；post_close ② 用它做净持仓聚合。

- [ ] **Step 1：写失败测试**

新建 `tests/trading/test_live_trades_csv.py`：

```python
# -*- coding: utf-8 -*-
"""CSV 审计层 kind 列（#3）：submit/fill 分离，post_close 聚合只认 fill。"""
import csv as _csv

import pytest

from presentation.server.services import trading_service as ts


@pytest.fixture
def csv_log(tmp_path, monkeypatch):
    log = tmp_path / "live_trades.csv"
    monkeypatch.setattr(ts, "LIVE_TRADE_LOG", str(log))
    return log


def test_csv_kind_column_distinguishes_submit_and_fill(csv_log):
    """CSV 有 kind 列；submit 行与 fill 行并存且可区分。"""
    ts.record_live_trade("600000.SH", "BUY", 100, 10.0, kind="submit",
                         rationale="QmtExecutionGateway:REJECTED:资金不足")
    ts.record_live_trade("600000.SH", "BUY", 100, 10.5, kind="fill", rationale="成交回报")
    with open(csv_log, encoding="utf-8-sig") as f:
        rows = list(_csv.DictReader(f))
    assert "kind" in rows[0], "CSV 必须有 kind 列"
    assert [r["kind"] for r in rows] == ["submit", "fill"]


def test_old_rows_without_kind_default_submit(csv_log):
    """老格式行（无 kind）默认按 submit 处理：不产生幻影持仓（保守）。"""
    csv_log.write_text("timestamp,symbol,direction,shares,price,strategy,rationale\n"
                       "2026-08-01 09:30:00,600000.SH,BUY,100,10.0,neckline,audit\n",
                       encoding="utf-8-sig")
    with open(csv_log, encoding="utf-8-sig") as f:
        rows = list(_csv.DictReader(f))
    assert rows[0].get("kind", "submit") == "submit"


def test_aggregate_fills_only_kind_fill(csv_log):
    """aggregate_fills_by_symbol 只聚合 kind=fill 的 BUY/SELL，submit/拒单不计。"""
    ts.record_live_trade("600000.SH", "BUY", 100, 10.0, kind="submit",
                         rationale="QmtExecutionGateway:REJECTED:资金不足")
    ts.record_live_trade("600000.SH", "BUY", 100, 10.5, kind="fill", rationale="成交回报")
    ts.record_live_trade("600000.SH", "SELL", 40, 11.0, kind="fill", rationale="成交回报")
    net = ts.aggregate_fills_by_symbol("2026-08-01", "2026-08-01")
    assert net == {"600000.SH": 60.0}, f"只聚合 fill 行应得净 60，实际 {net}"
```

- [ ] **Step 2：跑测试验证失败**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_live_trades_csv.py -v`
Expected: FAIL（`record_live_trade() got an unexpected keyword argument 'kind'` / `aggregate_fills_by_symbol` 不存在）。

- [ ] **Step 3：trading_service 加 kind 列 + aggregate 函数**

`LIVE_TRADE_COLUMNS`（`:41-43`）改为：

```python
LIVE_TRADE_COLUMNS = [
    "timestamp", "symbol", "direction", "shares", "price", "strategy", "rationale", "kind",
]
```

`record_live_trade`（`:212-239`）加参数字段：

```python
def record_live_trade(
    symbol: str,
    direction: str,
    shares: float,
    price: float,
    strategy: str = "",
    rationale: str = "",
    kind: str = "fill",  # "submit"=下单审计（含 REJECTED/FAILED）/"fill"=真实成交回报
) -> None:
    """追加一笔实盘记录到 logs/live_trades.csv。

    kind 区分（#3 修复）：post_close 聚合净持仓只认 kind='fill'，避免 submit 行
    （拒单/重单）混入致幻影持仓。submit 行仍落盘满足审计合规（spec §6.3）。
    """
    os.makedirs(os.path.dirname(LIVE_TRADE_LOG), exist_ok=True)
    is_new = (not os.path.exists(LIVE_TRADE_LOG)) or os.path.getsize(LIVE_TRADE_LOG) == 0
    row = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "symbol": symbol, "direction": direction, "shares": shares, "price": price,
        "strategy": strategy, "rationale": rationale, "kind": kind,
    }
    with open(LIVE_TRADE_LOG, "a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=LIVE_TRADE_COLUMNS)
        if is_new:
            writer.writeheader()
        writer.writerow(row)
```

新增（`query_trades` 附近）：

```python
def aggregate_fills_by_symbol(start: str, end: str) -> dict[str, float]:
    """流式聚合 [start,end] 内 kind=fill 的 BUY/SELL 净持仓（post_close 对账用）。

    Why 不走 query_trades：其 limit=1000 分页会截断单日超 1000 行的聚合；
    本函数全量流式读 CSV，只认 kind=fill（老行无 kind 默认 submit，保守不计）。
    """
    net: dict[str, float] = {}
    if not os.path.exists(LIVE_TRADE_LOG):
        return net
    with open(LIVE_TRADE_LOG, "r", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            day = (r.get("timestamp") or "").split(" ")[0]
            if not (start <= day <= end):
                continue
            if (r.get("kind") or "submit") != "fill":
                continue
            sym = r.get("symbol")
            direction = (r.get("direction") or "").upper()
            shares = r.get("shares")
            if not sym or direction not in ("BUY", "SELL") or shares is None:
                continue
            net[sym] = net.get(sym, 0.0) + (float(shares) if direction == "BUY" else -float(shares))
    return net
```

`query_trades`（`:298` 附近）与 `export_trades`（`:252` 附近）读行时统一用 `r.get("kind", "submit")` 透出 kind（老行保守 submit）。

submit 落盘点（`:511-515`）显式传 `kind="submit"`：

```python
    record_live_trade(
        order.symbol, direction, order.qty, order.price or 0.0,
        rationale=f"{gw.__class__.__name__}:{result.state.name}:{result.message}",
        kind="submit",  # 下单审计行（含 REJECTED/FAILED），post_close 不计入净持仓
    )
```

- [ ] **Step 4：engine 成交回报落点 + post_close 换源**

`_handle_order_update` record_live_trade 调用（A5 重排后的 a 段）显式传 `kind="fill"`。

`post_close` ② 段（`:1510-1536`）整段替换为：

```python
    if gw is not None:
        try:
            from presentation.server.services.trading_service import \
                aggregate_fills_by_symbol as _svc_agg_fills
            # C-6 V2：业务日期 key（当日成交流水口径）走 clock.today。
            today_eq = clock.today()
            # #3 修复：只聚合 kind=fill 的真实成交（submit 审计行/拒单不计入净持仓）。
            net = _svc_agg_fills(today_eq, today_eq)
            local = _position_book.get_local_positions()
            drifts: list[tuple[str, float, float]] = []
            for sym, net_qty in net.items():
                if abs(net_qty - local.get(sym, 0.0)) > 0.01:
                    _position_book.reconcile_qty(sym, net_qty)
                    drifts.append((sym, local.get(sym, 0.0), net_qty))
            for sym, local_qty in local.items():
                if sym not in net and abs(local_qty) > 0.01:
                    _position_book.reconcile_qty(sym, 0.0)
                    drifts.append((sym, local_qty, 0.0))
            if drifts:
                result["trades_reconciled"] = len(drifts)
                msg = "【盘后兜底】aggregate_fills vs position_book drift " + ", ".join(
                    f"{s}({lo}→{n})" for s, lo, n in drifts)
                logger.warning(msg)
                try:
                    from infra.notifier import NotificationManager, fire_and_forget
                    fire_and_forget(NotificationManager.get_default().notify_risk_event(msg, "WARN"))
                except Exception:
                    logger.exception("盘后兜底告警推送失败（不阻塞）")
        except Exception:
            logger.exception("post_close aggregate_fills 兜底异常（不阻塞熔断/清白名单）")
```

- [ ] **Step 5：跑绿 + 回归 + 提交**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_live_trades_csv.py tests/trading/test_trading_service.py tests/trading/test_engine.py -v`
Expected: PASS。

```bash
git add presentation/server/services/trading_service.py trading/engine.py tests/trading/test_live_trades_csv.py tests/trading/test_trading_service.py
git commit -m "fix(#3): CSV 加 kind 列区分 submit/fill，post_close 只聚合 fill

拒单 submit 行与成交 fill 行分离，消除幻影持仓；老 CSV 无 kind 默认 submit 保守；
aggregate_fills_by_symbol 全量流式聚合，绕开 query_trades limit=1000 截断。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task A5：trade 分支重排（先落账后挂止盈）+ 错误分级 + 全链路 e2e（含竞态）

**Files:**
- Modify: `trading/engine.py`（`_handle_order_update` trade 分支重排为 d→c→a→b；错误分级）
- Test: `tests/trading/test_engine_order_update_handler.py`

**Interfaces:**
- Consumes: A1-A4 全部产出；`place_take_profit`（D1 前先用既有 `_place_take_profit` 方法，D1 再提为模块级）。
- Produces: trade 分支最终形态（下方完整代码）；两个 e2e 用例（正常 + 竞态）。

- [ ] **Step 1：写全链路 e2e 失败测试（真实回调链路）**

追加到 `tests/trading/test_engine_order_update_handler.py`：

```python
def test_e2e_real_callback_chain_fills_and_places_tp(monkeypatch, tmp_path):
    """真实回调链路：OPEN 单 → async_response → order(FILLED) → trade → 落账 + 挂 TP。

    守元问题：全程驱动 on_* 回调，禁止手塞 _orders；_submit 用模块级 patch。
    """
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from trading import state_store, trading_plan

    monkeypatch.setattr(state_store, "_DEFAULT_DB", str(tmp_path / "state.db"))
    state_store.init_store()
    eng, gw = _make_real_chain_engine(monkeypatch)
    aid, seq, real = "TEST_ACC", 7, 987654
    oid = "2026-08-01_600000.SH_OPEN_7"
    state_store.insert_order(oid, f"{aid}_600000.SH_2026-08-01", aid, "2026-08-01",
                             "600000.SH", "buy", "OPEN", 100, 10.0,
                             broker_oid=str(seq), state="SUBMITTED")
    monkeypatch.setattr("trading.engine.trading_plan.load_plan",
                        lambda d: {"orders": [{"order": {"symbol": "600000.SH"},
                                               "take_profit": 11.0, "tp1": 10.8,
                                               "tp1_portion": 0.0}]})
    monkeypatch.setattr("trading.engine._submit",
                        AsyncMock(return_value={"order_id": "tp_seq", "state": "SUBMITTED"}))
    # ① async_response 回填 real
    asyncio.run(_pump(gw, lambda: gw.on_order_stock_async_response(
        SimpleNamespace(seq=seq, order_id=real))))
    # ② order 事件推进 FILLED（累计量）
    asyncio.run(_pump(gw, lambda: gw.on_stock_order(SimpleNamespace(
        order_id=real, stock_code="600000.SH", order_status=56, order_type=23,
        order_volume=100, traded_volume=100, traded_price=10.5, status_msg=""))))
    # ③ trade 事件落账 + 挂 TP
    asyncio.run(_pump(gw, lambda: gw.on_stock_trade(SimpleNamespace(
        order_id=real, stock_code="600000.SH", traded_volume=100, traded_price=10.5,
        traded_amount=1050.0, traded_time="20260801101000"))))
    with state_store._connect(state_store._DEFAULT_DB) as con:
        row = con.execute('SELECT state, filled_qty FROM "order" WHERE order_id=?', (oid,)).fetchone()
        tp_row = con.execute("SELECT purpose, qty FROM \"order\" WHERE purpose='TP2'").fetchone()
        fill_row = con.execute("SELECT COUNT(*) c FROM fill").fetchone()
        pos_row = con.execute("SELECT qty FROM position WHERE symbol='600000.SH'").fetchone()
    assert row["state"] == "FILLED" and row["filled_qty"] == 100
    assert tp_row is not None and tp_row["qty"] == 100, "BUY 成交后应挂 TP2 100"
    assert fill_row["c"] == 1 and pos_row["qty"] == 100, "成交应落 fill + position"


def test_e2e_trade_before_async_response_race(monkeypatch, tmp_path):
    """竞态：trade 先于 async_response → seq 反查兜底落账，随后回填不覆盖。"""
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from trading import state_store

    monkeypatch.setattr(state_store, "_DEFAULT_DB", str(tmp_path / "state.db"))
    state_store.init_store()
    eng, gw = _make_real_chain_engine(monkeypatch)
    aid, seq, real = "TEST_ACC", 7, 987654
    oid = "2026-08-01_600000.SH_OPEN_7"
    state_store.insert_order(oid, f"{aid}_600000.SH_2026-08-01", aid, "2026-08-01",
                             "600000.SH", "buy", "OPEN", 100, 10.0,
                             broker_oid=str(seq), state="SUBMITTED")
    gw._seq_to_real = {seq: real}
    monkeypatch.setattr("trading.engine._submit",
                        AsyncMock(return_value={"order_id": "tp_seq", "state": "SUBMITTED"}))
    # trade 先到（async_response 未回填）：方向经 seq 反查 DB side 仍应落账
    asyncio.run(_pump(gw, lambda: gw.on_stock_trade(SimpleNamespace(
        order_id=real, stock_code="600000.SH", traded_volume=100, traded_price=10.5,
        traded_amount=1050.0, traded_time="20260801101000"))))
    with state_store._connect(state_store._DEFAULT_DB) as con:
        fill_row = con.execute("SELECT COUNT(*) c FROM fill").fetchone()
    assert fill_row["c"] == 1, "trade 先到也必须落账（seq 反查兜底）"
    # async_response 后到：回填 real，不覆盖任何东西
    asyncio.run(_pump(gw, lambda: gw.on_order_stock_async_response(
        SimpleNamespace(seq=seq, order_id=real))))
    with state_store._connect(state_store._DEFAULT_DB) as con:
        row = con.execute('SELECT broker_oid FROM "order" WHERE order_id=?', (oid,)).fetchone()
    assert row["broker_oid"] == str(real), "async_response 应回填 real"
```

- [ ] **Step 2：跑测试验证失败**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_engine_order_update_handler.py -v`
Expected: FAIL（方向 None → fill 未落 / TP 未挂）。

- [ ] **Step 3：trade 分支重排 + 错误分级（最终形态）**

`_handle_order_update` trade 分支整体替换为（顺序 d→c→a→b）：

```python
        direction = self._order_direction(order_id)
        if direction is None:
            _alert_critical(
                f"成交回报方向未知 order_id={order_id} symbol={symbol} qty={qty} "
                f"（DB 无 side、内存无 order_type，需人工对账补账）")

        # ── d. 成交账本写入（真相源，最先做——先落账再挂止盈，防 crash 窗口账账不符）──
        if direction in ("BUY", "SELL"):
            try:
                if _state_store.get_account(_account_id) is None:
                    _state_store.upsert_account(_account_id, broker="qmt")
                traded_time = str(update.get("traded_time", ""))
                if _state_store.insert_fill(
                        order_id, _account_id, traded_time, symbol, direction,
                        float(qty), float(price)):
                    # insert_fill 首次入账才更新 position（避免重推重复累加）
                    _state_store.apply_fill_to_position(
                        _account_id, symbol, direction, float(qty), float(price), traded_time)
                _state_store.insert_trade_event(
                    _account_id, _trade_id, symbol, "FILLED",
                    order_id=order_id, qty=float(qty), price=float(price))
            except Exception:
                # C-4：敞口真相失真 = L1 停调度（宁可停不可带病跑）
                raise _CriticalHalt(
                    f"成交回报落账失败 symbol={symbol} order_id={order_id}（fill/position 真相源失真）") from None

        # ── c. 买单成交 → 止盈差额补挂（D1 前调方法，D1 后调模块级 place_take_profit）──
        if direction == "BUY" and not _tp_already:
            try:
                await self._place_take_profit(symbol, qty, price, order_id)
            except Exception:
                logger.exception("挂止盈失败 symbol=%s（CRITICAL：需人工补挂）", symbol)

        # ── a. 成交日志（CSV 审计旁路，失败不阻断）──
        try:
            from presentation.server.services.trading_service import record_live_trade
            record_live_trade(
                symbol, direction or "TRADE", float(qty), float(price),
                strategy="neckline",
                rationale=f"成交回报@{update.get('traded_time')}",
                kind="fill",  # #3：真实成交，post_close 据此聚合净持仓
            )
        except Exception:
            logger.exception("成交日志补写失败 symbol=%s（不影响后续通知）", symbol)

        # ── b. 钉钉成交通知（fire_and_forget 不阻塞回调链）──
        try:
            from infra.notifier import NotificationManager, fire_and_forget
            fire_and_forget(NotificationManager.get_default().notify_trade_event(
                symbol, direction or "TRADE", float(qty), float(price),
            ))
        except Exception:
            logger.exception("成交通知发送失败 symbol=%s", symbol)
```

> 说明：`_tp_already`（TP1 幂等查询）与 `_account_id/_trade_id` 保持 A3 前既有计算位置不变（`clock.today()` 口径）；`raise _CriticalHalt ... from None` 保留原异常日志（先 `logger.exception` 再 raise，或直接 `raise _CriticalHalt(...) from e`）。

- [ ] **Step 4：跑绿 + 回归 + 提交**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_engine_order_update_handler.py tests/trading/test_engine.py -v`
Expected: PASS。

```bash
git add trading/engine.py tests/trading/test_engine_order_update_handler.py
git commit -m "fix(#1/#5): trade 分支先落账后挂止盈 + 错误分级 + 全链路 e2e（含竞态）

真相源写失败升 _CriticalHalt；CSV/通知旁路软降级；方向未知 CRITICAL。
e2e 走真实回调链路；竞态用例覆盖 trade 先于 async_response 的 seq 反查兜底。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Phase B：data_ready 跨日 gate（#2，独立）

### Task B1：pre_open gate ③ 改查 expected_latest_trade_day

**Files:**
- Modify: `trading/engine.py`（`_pre_open_gate` ③，`:1956-1962`）
- Test: `tests/trading/test_e2e_trading_flow.py`（新增跨两日用例，旧同日用例保留）

**Interfaces:**
- Consumes: `data.freshness` 无关；`trading.calendar.expected_latest_trade_day(now)`（已存在）。
- Produces: `_pre_open_gate` ③ 命中 `data_ready(最近已收盘交易日)`。

- [ ] **Step 1：写跨两日失败测试**

追加到 `tests/trading/test_e2e_trading_flow.py`：

```python
def test_pre_open_next_day_gate_hits_prev_day_data_ready(isolated, monkeypatch):
    """跨日：T 日落 data_ready(T)，T+1 日 pre_open gate③ 应命中（#2）。

    生产根因：原 gate③ 查 get_data_ready(date=T+1) → 永远 None → 整天不挂单；
    旧同日冻结用例掩盖了跨日错位，本用例真实跨两日。
    """
    from datetime import datetime as _dt
    from trading import state_store, clock
    from trading.engine import TradingEngine

    PIPE_T, PREOPEN_T1 = "2026-07-30", "2026-07-31"
    monkeypatch.setattr(clock, "now", lambda: _dt(2026, 7, 30, 18, 0, 0))
    monkeypatch.setattr(clock, "today", lambda: PIPE_T)
    state_store.upsert_data_ready(PIPE_T, "daily", ok=True, melted=False,
                                  latest_date=PIPE_T, expected_date=PIPE_T, message="ok")
    # T+1 日 09:22 pre_open
    monkeypatch.setattr(clock, "now", lambda: _dt(2026, 7, 31, 9, 22, 0))
    monkeypatch.setattr(clock, "today", lambda: PREOPEN_T1)
    monkeypatch.setattr(engine.calendar, "is_trading_day", lambda d: True)
    eng = TradingEngine()
    monkeypatch.setattr(engine, "_ACTIVE_ENGINE", eng)
    fake_gw = MagicMock()
    fake_gw._connected = True
    fake_gw.is_client_ready = lambda *a, **kw: True
    # 确认计划：gate ① 段需通过
    from trading import trading_plan
    trading_plan.save_plan(PREOPEN_T1, [{
        "order": {"symbol": "300077.SZ", "qty": 100, "side": "BUY", "price": 10.0},
        "stop_price": 9.5, "take_profit": 11.0, "neckline": 10.0, "atr": 0.25,
        "formed_at": PIPE_T, "max_wait": 5, "tp1": None, "tp1_portion": 0.0,
        "cancel_on": None, "experiment_id": None, "experiment_weight": None, "rr": 2.0,
    }], confirmed=True)
    ok, reason = eng._pre_open_gate(PREOPEN_T1, fake_gw)
    assert ok, f"T+1 pre_open gate 应放行（命中 T 日 data_ready），reason={reason}"
```

- [ ] **Step 2：跑测试验证失败**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_e2e_trading_flow.py::test_pre_open_next_day_gate_hits_prev_day_data_ready -v`
Expected: FAIL（`reason` 含“数据 daily 未就绪（未采集）”）。

- [ ] **Step 3：改 _pre_open_gate ③**

`trading/engine.py:1956-1962` 替换为：

```python
        # ③ 数据就绪（DB 查询；防御性双检）
        # #2 修复：改查 expected_latest_trade_day(now)——T 日盘后落 data_ready(T)，
        # T+1 日盘前 pre_open 查“最近已收盘交易日”=T 命中。原查 get_data_ready(date=T+1)
        # 永远 None（data_ready 只落 T）→ 整天不挂单。与 _eod next_trading_day 同源口径。
        from trading.calendar import expected_latest_trade_day
        _data_date = expected_latest_trade_day(clock.now())
        for k in self._plan_data_keys(plan):
            ready = get_data_ready(_data_date, k)
            if ready is None or not ready.get("ok"):
                msg = ready["message"] if ready else "未采集"
                return False, f"数据 {k} 未就绪（{_data_date}：{msg}）"
        return True, ""
```

- [ ] **Step 4-5：跑绿 + 回归 + 提交**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_e2e_trading_flow.py -v`
Expected: PASS（新用例 + 旧同日冻结用例不破）。

```bash
git add trading/engine.py tests/trading/test_e2e_trading_flow.py
git commit -m "fix(#2): pre_open gate③ 改查 expected_latest_trade_day 命中 T 日 data_ready

原查 get_data_ready(T+1) 永远 None 致整天不挂单；与 _eod next_trading_day 同源口径。
补真实跨两日用例（旧同日冻结用例保留）。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Phase C：熔断 lock_down 粘滞（#6，独立）

### Task C1：gw 加 _risk_halted，网关入口统一 is_blocked

**Files:**
- Modify: `broker/qmt.py`（`__init__` / `connect()` / `_on_account_status_change` / 新增 `set_risk_halt` / `clear_risk_halt` / `is_blocked`；submit/cancel/query 入口）
- Modify: `presentation/server/services/trading_service.py`（`emergency_halt`）
- Modify: `trading/engine.py`（`_health_guard`）
- Test: `tests/trading/test_qmt_health_guard.py`、`tests/trading/test_emergency_halt.py`、`tests/trading/test_qmt_gateway.py`

**Interfaces:**
- Consumes: 既有 `_lock_down` / `_connected`。
- Produces: `gw._risk_halted: bool`；`gw.set_risk_halt(halted=True)` / `gw.clear_risk_halt()`；`gw.is_blocked -> bool`（= `_risk_halted or _lock_down`）。

- [ ] **Step 1：写失败测试**

`tests/trading/test_qmt_health_guard.py` 追加：

```python
def test_risk_halt_not_cleared_by_health_guard_reconnect(monkeypatch):
    """risk_halt 置位后，health_guard 重连成功也不解锁（风控粘滞，#6）。"""
    import asyncio
    from unittest.mock import MagicMock
    from broker.qmt import QmtExecutionGateway
    from trading.engine import TradingEngine

    gw = QmtExecutionGateway(userdata_path="C:/tmp/qmt_test", account_id="TEST_ACC")
    gw._connected = False
    gw._lock_down = False
    gw._risk_halted = False
    gw.set_risk_halt(True)
    assert gw._risk_halted is True and gw._lock_down is True
    eng = TradingEngine()
    eng._gw = gw
    monkeypatch.setattr(engine_mod := __import__("trading.engine", fromlist=["get_gateway"]),
                        "get_gateway", lambda: gw)
    monkeypatch.setattr(gw, "is_client_ready", lambda **kw: True)
    monkeypatch.setattr(gw, "connect", MagicMock())
    asyncio.run(eng._health_guard())
    assert gw._risk_halted is True, "risk_halt 必须粘滞，health_guard 不得自动解除"
    assert gw._lock_down is True, "risk_halt 期间 lock_down 不得被重连清掉"


def test_account_status_ok_does_not_clear_risk_halt(monkeypatch):
    """账号状态 OK 推送不得清 risk_halt 的锁（#6 补强：_on_account_status_change 同闸）。"""
    from broker.qmt import QmtExecutionGateway

    gw = QmtExecutionGateway(userdata_path="C:/tmp/qmt_test", account_id="TEST_ACC")
    gw.set_risk_halt(True)
    gw._on_account_status_change(0)  # ACCOUNT_STATUS_OK
    assert gw._lock_down is True, "risk_halt 期间账号 OK 不得清 lock_down"
```

`tests/trading/test_emergency_halt.py` 追加：

```python
def test_emergency_halt_sets_risk_halt(monkeypatch):
    """emergency_halt → set_risk_halt(True)；重复调用幂等。"""
    from presentation.server.services.trading_service import emergency_halt
    from unittest.mock import MagicMock

    gw = MagicMock()
    gw._lock_down = False
    monkeypatch.setattr("presentation.server.services.trading_service.get_gateway", lambda: gw)
    r1 = emergency_halt()
    gw.set_risk_halt.assert_called_once_with(True)
    assert r1["halted"] is True
```

- [ ] **Step 2：跑测试验证失败**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_qmt_health_guard.py tests/trading/test_emergency_halt.py -v`
Expected: FAIL（`AttributeError: 'QmtExecutionGateway' object has no attribute '_risk_halted'`）。

- [ ] **Step 3：broker/qmt.py 实现**

`__init__`（`:284` 后）加：

```python
        # 风控熔断粘滞标志（#6）：emergency_halt/日内-3% 熔断置 True，
        # health_guard/账号 OK 均不得自动解除；解锁必须显式 clear_risk_halt()。
        self._risk_halted: bool = False
```

新增方法（`connect` 附近）：

```python
    @property
    def is_blocked(self) -> bool:
        """网关拒单总闸（#6）：风控熔断或断线锁任一生效即拒。"""
        return self._risk_halted or self._lock_down

    def set_risk_halt(self, halted: bool = True) -> None:
        """风控熔断锁（emergency_halt/日内-3% 触发）。halted=True 置粘滞锁 + _lock_down。"""
        self._risk_halted = halted
        if halted:
            self._lock_down = True
            self._connected = False

    def clear_risk_halt(self) -> None:
        """显式解除风控熔断（人工/次日盘前）。仅清 risk_halt，_lock_down 由 connect 自然恢复。"""
        self._risk_halted = False
```

`connect()` 清锁处（`:405-406`）改条件：

```python
        self._connected = True
        if not self._risk_halted:
            self._lock_down = False  # 仅网络断线重连清锁；风控熔断(_risk_halted)粘滞不清
        logger.info("QMT 网关已连接 account=%s session=%s risk_halted=%s",
                    self._account_id, self._session_id, self._risk_halted)
```

`_on_account_status_change` OK 分支（`:1126-1128`）改：

```python
        elif status_int == _QMT_ACC_OK:
            # #6：risk_halted 期间账号 OK 只记日志，不得清锁（风控熔断需人工解除）
            if not self._risk_halted:
                self._lock_down = False
            logger.info("QMT 账号状态 OK account=%s，已清锁" if not self._risk_halted
                        else "QMT 账号状态 OK account=%s（risk_halted 粘滞，锁保持）",
                        self._account_id)
```

`submit_order`/`cancel_order`/`query_asset`/`query_orders`/`_fetch_broker_positions` 入口的 `if self._lock_down:` 全部改为 `if self.is_blocked:`（断线锁与风控锁同闸）。

- [ ] **Step 4：trading_service + engine**

`emergency_halt`（`:347-352`）改为：

```python
    # 置风控熔断粘滞锁（#6：health_guard 不得自动重连解除）
    if hasattr(gw, "set_risk_halt"):
        gw.set_risk_halt(True)   # 置 _risk_halted=True + _lock_down=True + _connected=False
    else:
        gw._lock_down = True
        gw._connected = False
```

`_health_guard`（`engine.py:2079` gw is None 检查之后）插入：

```python
        # #6 修复：风控熔断粘滞——risk_halted 时只告警不重连（熔断应全场停摆，次日人工接管）。
        # 与 on_disconnected 网络断线区分（后者 _risk_halted=False，允许 health_guard 自愈）。
        if getattr(gw, "_risk_halted", False):
            logger.warning("网关处于风控熔断态（risk_halted），health_guard 跳过重连（需人工 clear_risk_halt）")
            return
```

- [ ] **Step 5：跑绿 + 回归 + 提交**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_qmt_health_guard.py tests/trading/test_emergency_halt.py tests/trading/test_qmt_gateway.py tests/test_qmt_gateway.py -v`
Expected: PASS。

```bash
git add broker/qmt.py presentation/server/services/trading_service.py trading/engine.py tests/trading/test_qmt_health_guard.py tests/trading/test_emergency_halt.py
git commit -m "fix(#6): 风控熔断加 _risk_halted 粘滞标志，网关入口统一 is_blocked

emergency_halt/日内-3% 熔断置 risk_halted；connect/账号 OK 均不清风控锁；
health_guard 不重连；submit/cancel/query 统一 is_blocked 总闸。解锁须显式 clear_risk_halt。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Phase D：止盈差额补挂（#4，依赖 Phase A）

### Task D1：place_take_profit 提为模块级 + 差额补挂算法 + 冲突告警

**Files:**
- Modify: `trading/state_store.py`（新增 `get_order_placed_qty`）
- Modify: `trading/engine.py`（`_place_take_profit` 方法体迁为模块级 `place_take_profit`；方法留薄包装；`_record_tp` 冲突升级 ERROR）
- Test: `tests/trading/test_engine.py`、`tests/trading/test_state_store.py`

**Interfaces:**
- Consumes: `get_order_by_broker_oid`（OPEN 行 filled_qty）；`has_order`；`insert_order`；`_submit`（模块级）。
- Produces: `async def place_take_profit(symbol, filled_qty, fill_price, order_id) -> None`（模块级）；`state_store.get_order_placed_qty(account_id, trade_date, symbol, purpose) -> float`；`TradingEngine._place_take_profit` 薄包装。

- [ ] **Step 1：写失败测试**

`tests/trading/test_state_store.py` 追加：

```python
def test_get_order_placed_qty_excludes_terminal(monkeypatch, tmp_path):
    """get_order_placed_qty：只合计未终态 TP 行（REJECTED/CANCELLED 不计）。"""
    from trading import state_store

    monkeypatch.setattr(state_store, "_DEFAULT_DB", str(tmp_path / "state.db"))
    state_store.init_store()
    aid, d, sym = "TEST_ACC", "2026-08-01", "600000.SH"
    state_store.insert_order(f"{d}_{sym}_TP2_1", f"{aid}_{sym}_{d}", aid, d, sym, "sell", "TP2", 100, 11.0, state="SUBMITTED")
    state_store.insert_order(f"{d}_{sym}_TP2_2", f"{aid}_{sym}_{d}", aid, d, sym, "sell", "TP2", 100, 11.0, state="REJECTED")
    assert state_store.get_order_placed_qty(aid, d, sym, "TP2") == 100.0
```

`tests/trading/test_engine.py` 追加（**必须装配 plan/account/OPEN 行 + 模块级 patch `_submit`**，防“无计划早退”假绿）：

```python
def test_partial_fill_tp_diff_no_oversell_no_gap(monkeypatch, tmp_path):
    """3 笔部分成交（300 股）：TP 差额补挂，总量=目标量，不超卖、无覆盖缺口（#4）。"""
    import asyncio
    from unittest.mock import AsyncMock
    from trading import state_store
    from trading.engine import place_take_profit

    monkeypatch.setattr(state_store, "_DEFAULT_DB", str(tmp_path / "state.db"))
    state_store.init_store()
    aid, today, sym = "TEST_ACC", "2026-08-01", "600000.SH"
    state_store.upsert_account(aid, broker="qmt")
    state_store.insert_order(f"{today}_{sym}_OPEN_7", f"{aid}_{sym}_{today}", aid, today, sym,
                             "buy", "OPEN", 300, 10.0, broker_oid="987654", state="SUBMITTED")
    monkeypatch.setattr("trading.engine.trading_plan.load_plan",
                        lambda d: {"orders": [{"order": {"symbol": sym},
                                               "take_profit": 12.0, "tp1": 11.0,
                                               "tp1_portion": 0.5}]})
    submit_calls = []
    async def _fake_submit(order, **kw):
        submit_calls.append((order.symbol, order.side, order.qty, order.price))
        return {"order_id": f"seq{len(submit_calls)}", "state": "SUBMITTED"}
    monkeypatch.setattr("trading.engine._submit", _fake_submit)
    # 分 3 笔成交：累计 100/200/300
    for filled in (100, 200, 300):
        state_store.update_order_state_by_broker_oid(
            "987654", state="PARTIAL" if filled < 300 else "FILLED",
            filled_qty=float(filled), filled_price=10.5)
        asyncio.run(place_take_profit(sym, float(filled), 10.5, "987654"))
    tp_sells = [q for _, side, q, _ in submit_calls if side == "sell"]
    assert sum(tp_sells) == 300, f"TP 卖单总量应=持仓 300，实际 {sum(tp_sells)}（{tp_sells}）"
    assert tp_sells == [100, 100, 100], f"差额补挂应逐笔补 100，实际 {tp_sells}"
    # 已挂量 = 目标量后再触发（重推）→ 零 submit
    before = len(submit_calls)
    asyncio.run(place_take_profit(sym, 300.0, 10.5, "987654"))
    assert len(submit_calls) == before, "已挂满后再触发不得重复 submit"


def test_tp_single_leg_portion_zero_incremental(monkeypatch, tmp_path):
    """tp1_portion=0 退化单腿 TP2：分 3 笔补挂合计 300，不重复。"""
    import asyncio
    from trading import state_store
    from trading.engine import place_take_profit

    monkeypatch.setattr(state_store, "_DEFAULT_DB", str(tmp_path / "state.db"))
    state_store.init_store()
    aid, today, sym = "TEST_ACC", "2026-08-01", "600000.SH"
    state_store.upsert_account(aid, broker="qmt")
    state_store.insert_order(f"{today}_{sym}_OPEN_7", f"{aid}_{sym}_{today}", aid, today, sym,
                             "buy", "OPEN", 300, 10.0, broker_oid="987654", state="SUBMITTED")
    monkeypatch.setattr("trading.engine.trading_plan.load_plan",
                        lambda d: {"orders": [{"order": {"symbol": sym},
                                               "take_profit": 12.0, "tp1": None,
                                               "tp1_portion": 0.0}]})
    submit_calls = []
    async def _fake_submit(order, **kw):
        submit_calls.append(order.qty)
        return {"order_id": f"seq{len(submit_calls)}", "state": "SUBMITTED"}
    monkeypatch.setattr("trading.engine._submit", _fake_submit)
    for filled in (100, 200, 300):
        asyncio.run(place_take_profit(sym, float(filled), 10.5, "987654"))
    assert submit_calls == [100, 100, 100] and sum(submit_calls) == 300
```

- [ ] **Step 2：跑测试验证失败**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_state_store.py tests/trading/test_engine.py -v`
Expected: FAIL（`place_take_profit` 不存在 / 全量重挂导致 submit 超出目标）。

- [ ] **Step 3：state_store 加 get_order_placed_qty**

```python
def get_order_placed_qty(account_id: str, trade_date: str, symbol: str, purpose: str, *,
                         db_path: str | None = None) -> float:
    """已挂委托量合计（未终态 state）：止盈差额补挂用。

    终态（REJECTED/FAILED/CANCELLED/PARTIAL_CANCELLED）不算已挂——被拒的腿
    允许后续事件补挂（与 has_order 排除集同口径）。
    """
    db_path = db_path or _DEFAULT_DB
    with _connect(db_path) as con:
        row = con.execute(
            "SELECT COALESCE(SUM(qty), 0) AS total FROM \"order\" "
            "WHERE account_id=? AND trade_date=? AND symbol=? AND purpose=? "
            "AND state NOT IN ('REJECTED','FAILED','CANCELLED','PARTIAL_CANCELLED')",
            (account_id, trade_date, symbol, purpose)).fetchone()
    return float(row["total"]) if row else 0.0
```

- [ ] **Step 4：engine 迁出模块级 place_take_profit + 差额算法**

`trading/engine.py` 新增模块级函数（原 `_place_take_profit` 方法体迁入，`self` 依赖全部换成模块级依赖；方法体保留在类内作为薄包装）：

```python
async def place_take_profit(symbol: str, filled_qty: float, fill_price: float,
                            order_id: str) -> None:
    """挂限价止盈卖单（#4 差额补挂：目标量 − 已挂量，防超卖/防覆盖缺口）。

    Why 模块级：stop_loss_monitor（模块级函数）盘中 TP 漏挂兜底也要调它（#10），
    实例方法无法被模块级函数引用（原 plan E4 的 self 错误根因）。
    """
    today = clock.today()
    plan = trading_plan.load_plan(today)
    if not plan:
        logger.warning("挂止盈跳过：无活跃计划 symbol=%s（计划未落盘/已失效）", symbol)
        return
    tp2 = tp1 = None
    tp1_portion = 0.0
    for o in plan.get("orders", []):
        if (o.get("order") or {}).get("symbol") == symbol:
            tp2 = o.get("take_profit")
            tp1 = o.get("tp1")
            tp1_portion = float(o.get("tp1_portion") or 0.0)
            break
    if tp2 is None or tp2 <= 0:
        logger.warning("挂止盈跳过：无止盈价配置 symbol=%s（计划缺 take_profit）", symbol)
        return
    filled_int = int(filled_qty)
    if filled_int <= 0:
        logger.warning("挂止盈跳过：成交量非正 symbol=%s filled_qty=%s", symbol, filled_qty)
        return

    from trading.compute.types import OrderRequest
    _aid = _resolve_account_id()
    _tid = f"{_aid}_{symbol}_{today}"

    # 已成交总量：OPEN 行 filled_qty（order 事件累计）优先，入参兜底
    total_filled = float(filled_int)
    if order_id:
        try:
            _open = _state_store.get_order_by_broker_oid(str(order_id))
            if _open is not None and _open.get("filled_qty"):
                total_filled = float(_open["filled_qty"])
        except Exception:
            logger.warning("读 OPEN filled_qty 失败 symbol=%s（用入参兜底）", symbol)

    def _placed(purpose: str) -> float:
        """已挂未终态量（差额基准）。"""
        try:
            return _state_store.get_order_placed_qty(_aid, today, symbol, purpose)
        except Exception:
            logger.exception("get_order_placed_qty(%s) 失败 symbol=%s（保守视为 0 补挂）", purpose, symbol)
            return 0.0

    def _record_tp(purpose: str, qty: int, price: float) -> None:
        """挂止盈单后落 DB order（幂等）；冲突=已发单但 DB 已有 → 升级 ERROR 人工复核。"""
        try:
            if _state_store.get_account(_aid) is None:
                _state_store.upsert_account(_aid, broker="qmt")
            oid = f"{today}_{symbol}_{purpose}_1"
            ok = _state_store.insert_order(
                oid, _tid, _aid, today, symbol, "sell", purpose,
                float(qty), float(price), state="SUBMITTED")
            if not ok:
                # #4：UNIQUE 冲突=该 purpose 已挂但本次 _submit 已发出 → 柜台可能多挂。
                logger.error("【止盈幂等冲突】%s %s 已落 DB 但本次 _submit 已发柜台，"
                             "需人工复核是否多挂超卖", symbol, purpose)
        except Exception:
            logger.exception("insert_order(%s) 失败 symbol=%s", purpose, symbol)

    use_two_legs = (tp1 is not None and tp1 > 0 and tp1_portion > 0.0 and tp1 < tp2)
    if not use_two_legs:
        # 单腿全平 tp2：差额 = 总持仓 − 已挂 TP2
        need2 = int(total_filled) - int(_placed("TP2"))
        if need2 <= 0:
            return
        result = await _submit(
            OrderRequest(symbol=symbol, qty=need2, side="sell", price=tp2), confirm=True)
        if result.get("state") not in ("REJECTED", "FAILED"):
            logger.info("【止盈单已挂】%s %s股 @%s（单笔全平 tp2 差额补挂）", symbol, need2, tp2)
            _record_tp("TP2", need2, tp2)
        else:
            logger.warning("止盈单挂失败 symbol=%s state=%s msg=%s（人工补挂）",
                           symbol, result.get("state"), result.get("message"))
        return

    # 分级两腿：目标量 − 已挂量，各腿独立补挂（防超卖 + 防覆盖缺口）
    tp1_target = int(total_filled * tp1_portion / 100) * 100
    tp2_target = int(total_filled) - tp1_target
    need1 = tp1_target - int(_placed("TP1"))
    need2 = tp2_target - int(_placed("TP2"))
    if need1 > 0:
        r1 = await _submit(
            OrderRequest(symbol=symbol, qty=need1, side="sell", price=tp1), confirm=True)
        if r1.get("state") not in ("REJECTED", "FAILED"):
            _record_tp("TP1", need1, tp1)
        else:
            logger.warning("止盈单挂失败 symbol=%s leg=tp1 state=%s msg=%s（人工补挂）",
                           symbol, r1.get("state"), r1.get("message"))
    if need2 > 0:
        r2 = await _submit(
            OrderRequest(symbol=symbol, qty=need2, side="sell", price=tp2), confirm=True)
        if r2.get("state") not in ("REJECTED", "FAILED"):
            _record_tp("TP2", need2, tp2)
        else:
            logger.warning("止盈单挂失败 symbol=%s leg=tp2 state=%s msg=%s（人工补挂）",
                           symbol, r2.get("state"), r2.get("message"))
```

类内 `_place_take_profit`（`:2808`）整体替换为薄包装：

```python
    async def _place_take_profit(self, symbol: str, filled_qty: float,
                                 fill_price: float, order_id: str) -> None:
        """薄包装：成交回报链路调模块级 place_take_profit（#4 差额补挂）。"""
        return await place_take_profit(symbol, filled_qty, fill_price, order_id)
```

- [ ] **Step 5：跑绿 + 回归 + 提交**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_engine.py tests/trading/test_state_store.py tests/trading/test_engine_order_update_handler.py -v`
Expected: PASS（A5 e2e 的 TP 断言仍绿——单腿场景差额=100）。

```bash
git add trading/state_store.py trading/engine.py tests/trading/test_engine.py tests/trading/test_state_store.py
git commit -m "fix(#4): 止盈改差额补挂（目标量-已挂量），提为模块级 place_take_profit

部分成交/重推不再重复 submit 超卖，也不留未覆盖仓；UNIQUE 冲突升级 ERROR 告警。
模块级函数供 stop_loss_monitor 盘中兜底（#10）复用。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Phase D'：盘中 TP 漏挂兜底（#10，依赖 D）

### Task D2：stop_loss_monitor TP 分支补挂 + WARNING

**Files:**
- Modify: `trading/engine.py`（`stop_loss_monitor` TP 分支 `:1101-1109`）
- Test: `tests/trading/test_stop_loss_monitor_decide_exit.py`

**Interfaces:**
- Consumes: D1 的模块级 `place_take_profit`；`_state_store.has_order`。
- Produces: TP 漏挂时盘中补挂 + WARNING（不再静默 continue）。

- [ ] **Step 1：写失败测试**

`tests/trading/test_stop_loss_monitor_decide_exit.py` 追加：

```python
def test_tp_missing_places_fallback(monkeypatch):
    """decide_exit=TAKE_PROFIT 且 DB 无 TP1/TP2 → 盘中补挂（#10）。

    复用本文件既有 _holding_ctx/_run_monitor 夹具：autouse _isolate_state_db 保证
    DB 无任何 TP 行 → has_order(TP1/TP2) 均 False → 必须触发 place_take_profit 补挂。
    """
    from unittest.mock import patch as _patch

    ctx = _holding_ctx(stop=9.5, tp1=11.0, tp2=12.0, holding_days=3,
                       is_last=False, max_holding=15, tp1_portion=0.5)
    # high=11.2 ≥ tp1=11.0 → decide_exit priority 3 → CLOSE/TAKE_PROFIT/portion=0.5
    quotes = {SYM: {"last_price": 11.2, "high": 11.2, "low": 9.6}}
    positions = {SYM: {"volume": 100, "avg_price": 10.0}}
    placed = {"n": 0}

    async def _fake_place(symbol, filled_qty, fill_price, order_id):
        placed["n"] += 1

    with _patch("trading.engine.place_take_profit", new=_fake_place):
        result = _run_monitor({SYM: ctx}, positions, quotes)
    assert placed["n"] == 1, "TP 漏挂必须触发盘中补挂"
    assert result["stop_triggered"] == 0, "补挂走限价单路径，monitor 不发市价单"
```

- [ ] **Step 2：跑测试验证失败**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_stop_loss_monitor_decide_exit.py::test_tp_missing_places_fallback -v`
Expected: FAIL（`placed["n"] == 0`——现状 TP 分支直接 continue）。

- [ ] **Step 3：TP 分支补挂**

`stop_loss_monitor` 的 `CLOSE/TAKE_PROFIT` 分支（`:1101-1109`）改为：

```python
                if dec.action is ExitAction.CLOSE:
                    if dec.reason is ExitReason.TAKE_PROFIT:
                        # I-1：TP 主路径交 _place_take_profit 预挂限价单，monitor 不发市价单
                        # （D10 物理边界）。#10：漏挂兜底——DB 无 TP1/TP2 时盘中补挂，
                        # 否则止盈永远不执行（拖到止损/超时）。
                        _tp_ok = False
                        try:
                            _tp_ok = (_state_store.has_order(_aid, _today, sym, "TP1")
                                      or _state_store.has_order(_aid, _today, sym, "TP2"))
                        except Exception:
                            _tp_ok = True  # DB 查失败保守视为已挂（防重复挂超卖）
                        if not _tp_ok:
                            logger.warning("【TP 漏挂兜底】%s decide_exit=TAKE_PROFIT 但 DB 无 TP1/TP2，盘中补挂", sym)
                            try:
                                await place_take_profit(sym, qty, price, "")
                            except Exception:
                                logger.exception("TP 盘中补挂失败 symbol=%s（需人工补挂）", sym)
                        continue   # TP 交预挂限价单，不走 fallback
```

- [ ] **Step 4-5：跑绿 + 回归 + 提交**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_stop_loss_monitor_decide_exit.py tests/trading/test_engine.py -v`
Expected: PASS。

```bash
git add trading/engine.py tests/trading/test_stop_loss_monitor_decide_exit.py
git commit -m "fix(#10): stop_loss_monitor TP 漏挂盘中补挂 + WARNING

decide_exit=TAKE_PROFIT 且 DB 无 TP1/TP2 时调模块级 place_take_profit 补挂，
不再静默 continue（防止盈永不执行拖到止损/超时）。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Phase E：收尾硬化（#7/#8/#9，独立小改）

### Task E1（#9）：状态 51 改映射 SUBMITTED

**Files:** `broker/qmt.py:125-144`（`_map_qmt_status`）；测试 `tests/test_qmt_gateway.py`

- [ ] **Step 1：写失败测试**

```python
def test_status_51_reported_cancel_maps_to_submitted():
    """51（已报待撤）保守映射 SUBMITTED：撤单刚受理不当终态（#9）。"""
    from broker.qmt import _map_qmt_status
    from trading.types.order_state import OrderState

    assert _map_qmt_status(51) is OrderState.SUBMITTED
```

- [ ] **Step 2：跑红**：`.venv310/Scripts/python.exe -m pytest tests/test_qmt_gateway.py::test_status_51_reported_cancel_maps_to_submitted -v` → FAIL（现为 CANCELLED）。

- [ ] **Step 3：改 `qmt.py:139`**：CANCELLED 分支从 `(54, 51)` 收窄为 `(54,)`，51 落到末尾 `return OrderState.SUBMITTED`；同步更新 `:128-129` 注释（51=已报待撤，撤单指令已受理但未到终态，等 54 或 query_orders 推进）。

- [ ] **Step 4-5：跑绿 + 回归 + 提交**

Run: `.venv310/Scripts/python.exe -m pytest tests/test_qmt_gateway.py tests/trading/test_qmt_cancel_confirm.py -v` → PASS。

```bash
git add broker/qmt.py tests/test_qmt_gateway.py
git commit -m "fix(#9): QMT 状态51(已报待撤)保守映射 SUBMITTED，等真终态推进

原映射 CANCELLED 致撤单刚受理即当终态，pre_open unconfirmed 漏报。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

### Task E2（#8）：撤单计数检查 OrderResult.state

**Files:** `trading/io/breaker.py:111-137`（`_cancel_via_broker_query`）与 `:144-177`（`_cancel_via_memory`）；测试 `tests/trading/test_breaker_cancel_confirm.py`

- [ ] **Step 1：写失败测试**

```python
def test_cancel_via_broker_query_counts_failed(monkeypatch):
    """撤单返回 FAILED 计 failed，不计 cancelled（#8）。"""
    import asyncio
    from unittest.mock import AsyncMock
    from trading.io.breaker import _cancel_via_broker_query
    from trading.types.order_state import OrderState
    from broker.base import OrderResult

    gw = AsyncMock()
    gw.query_orders = AsyncMock(return_value=[{"order_id": 1001}])
    gw.cancel_order_by_broker_oid = AsyncMock(
        return_value=OrderResult(order_id="1001", state=OrderState.FAILED, message="撤单失败"))
    res = asyncio.run(_cancel_via_broker_query(gw, gw.query_orders,
                                               gw.cancel_order_by_broker_oid, None, None))
    assert res["cancelled"] == 0, "FAILED 不得计 cancelled"
    assert res["failed"] == 1
```

- [ ] **Step 2：跑红**：`pytest tests/trading/test_breaker_cancel_confirm.py::test_cancel_via_broker_query_counts_failed -v` → FAIL（`KeyError: 'failed'` 或 cancelled=1）。

- [ ] **Step 3：改 breaker**：两个路径各加 `n_failed = 0`；`res = await cancel_by_oid_fn(...)` 后：

```python
            res = await cancel_by_oid_fn(broker_oid)
            # #8：只对成功发出（非 FAILED/REJECTED）计数；失败单独计 failed 告警
            _st = getattr(res, "state", None)
            if _st in (OrderState.FAILED, OrderState.REJECTED):
                n_failed += 1
                logger.warning("熔断撤单被拒/失败 broker_oid=%s state=%s（计入 failed）", broker_oid, _st)
            else:
                n_cancelled += 1
                # ...（保留既有 DB 回写 + _confirm_cancelled 逻辑）
```

内存路径 `_cancel_via_memory` 同款：`gw.cancel_order(oid)` 返 OrderResult 后判 state；返回 dict 统一加 `"failed": n_failed`，告警消息加 failed 计数。

- [ ] **Step 4-5：跑绿 + 回归 + 提交**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_breaker_cancel_confirm.py tests/trading/test_engine.py -v` → PASS。

```bash
git add trading/io/breaker.py tests/trading/test_breaker_cancel_confirm.py
git commit -m "fix(#8): 熔断撤单计数区分 failed，cancelled 只计成功发出

FAILED/REJECTED 撤单计入 failed 并告警，不再虚增 cancelled。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

### Task E3（#7）：超期平仓 DB 幂等防重

**Files:** `trading/engine.py:_close_expired_positions`（`:1338-1402`）；测试 `tests/trading/test_engine.py`

- [ ] **Step 1：写失败测试**

```python
def test_close_expired_positions_skips_already_placed(monkeypatch, tmp_path):
    """已挂 EXPIRED_CLOSE 的 sym 不再重复 submit（#7 窄窗口卖超）。"""
    import asyncio
    from unittest.mock import AsyncMock
    from trading import state_store

    monkeypatch.setattr(state_store, "_DEFAULT_DB", str(tmp_path / "state.db"))
    state_store.init_store()
    aid, today, sym = "TEST_ACC", "2026-08-01", "600000.SH"
    state_store.upsert_account(aid, broker="qmt")
    state_store.insert_order(f"{today}_{sym}_EXPIRED_CLOSE_1", f"{aid}_{sym}_{today}",
                             aid, today, sym, "sell", "EXPIRED_CLOSE", 100, 9.5,
                             state="SUBMITTED")
    gw = AsyncMock()
    gw._fetch_broker_positions = AsyncMock(return_value={sym: {"volume": 100, "avg_price": 10.0}})
    monkeypatch.setattr("trading.engine.qmt_market_data.get_quotes",
                        AsyncMock(return_value={sym: {"low_limit": 9.5, "last_price": 9.8}}))
    submit_calls = []
    async def _fake_submit(order, **kw):
        submit_calls.append(order.symbol)
        return {"order_id": "x", "state": "SUBMITTED"}
    monkeypatch.setattr("trading.engine._submit", _fake_submit)
    from trading.engine import _close_expired_positions
    res = asyncio.run(_close_expired_positions(gw, [{"symbol": sym, "entry_date": "2026-06-15",
                                                     "holding_days": 20, "max_holding": 15}]))
    assert res["closed"] == 0
    assert submit_calls == [], "已挂 EXPIRED_CLOSE 不得重复 submit"
```

- [ ] **Step 2：跑红**：`pytest tests/trading/test_engine.py::test_close_expired_positions_skips_already_placed -v` → FAIL（submit 被调 1 次）。

- [ ] **Step 3：改 `_close_expired_positions`**：循环内每只挂卖前查幂等、成功后落 DB：

```python
    today_close = clock.today()
    _aid = _resolve_account_id()
    for e in expired:
        sym = e["symbol"]
        pos = positions.get(sym)
        qty = pos["volume"] if isinstance(pos, dict) else pos  # 兼容老 mock 返 float
        if not qty or qty <= 0:
            continue
        # #7：DB 幂等防重——已挂 EXPIRED_CLOSE（未终态）跳过，兜住“提交后、消费标记前崩溃”
        try:
            if _state_store.has_order(_aid, today_close, sym, "EXPIRED_CLOSE"):
                logger.info("跳过已挂 EXPIRED_CLOSE symbol=%s（DB 幂等）", sym)
                continue
        except Exception:
            logger.exception("has_order(EXPIRED_CLOSE) 查询失败 symbol=%s（保守跳过）", sym)
            continue
        quote = quotes.get(sym)
        low_limit = (quote or {}).get("low_limit")
        last_price = (quote or {}).get("last_price")
        price = low_limit if low_limit else last_price
        if price is None or price != price:  # NaN check
            logger.warning("跳过平超期 %s：无跌停价/现价（拒发盲单）", sym)
            continue
        try:
            result = await _submit(
                OrderRequest(symbol=sym, qty=qty, side="sell", price=price),
                confirm=True)
        except Exception as exc:
            logger.warning("平超期持仓失败 symbol=%s qty=%s 原因=%s", sym, qty, exc)
            continue
        if result.get("state") not in ("REJECTED", "FAILED"):
            n_closed += 1
            logger.warning("【超期平仓】%s 卖出 %s 股 @%s（holding_days=%s max_holding=%s mode=%s）",
                           sym, qty, price, e.get("holding_days"), e.get("max_holding"), _mode())
            try:
                if _state_store.get_account(_aid) is None:
                    _state_store.upsert_account(_aid, broker="qmt")
                _state_store.insert_order(
                    f"{today_close}_{sym}_EXPIRED_CLOSE_1",
                    f"{_aid}_{sym}_{today_close}", _aid, today_close, sym, "sell",
                    "EXPIRED_CLOSE", float(qty), float(price), state="SUBMITTED")
            except Exception:
                logger.exception("insert_order(EXPIRED_CLOSE) 失败 symbol=%s（告警人工复核）", sym)
```

标记消费时机不变（循环后 `_consume_expired_positions()`）——DB 幂等兜住窄窗口重挂。

- [ ] **Step 4-5：跑绿 + 回归 + 提交**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_engine.py -v` → PASS。

```bash
git add trading/engine.py tests/trading/test_engine.py
git commit -m "fix(#7): 超期平仓 DB 幂等防重（EXPIRED_CLOSE）

挂卖前 has_order 预检 + 成功后 insert_order 落库；标记消费窗口崩溃不再重复卖。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Self-Review

**1. Spec coverage（spec 10 缺陷 + 加固点 → 任务映射）：**
- #1 方向反查 → Task A3（+A2 order_type/merge 地基）✓
- #2 data_ready 跨日 → Task B1 ✓
- #3 CSV kind → Task A4 ✓
- #4 止盈差额补挂 → Task D1 ✓
- #5 broker_oid 回填 + 状态推进 → Task A1 + A2（+A5 落账）✓
- #6 熔断粘滞 → Task C1 ✓
- #7 超期平仓幂等 → Task E3 ✓
- #8 撤单计数 → Task E2 ✓
- #9 状态 51 → Task E1 ✓
- #10 TP 盘中兜底 → Task D2（依赖 D1 模块级函数）✓
- 竞态（async_response 晚到）→ A3 Step 1 + A5 Step 1（e2e 竞态用例）✓
- 测试元问题 → 每任务真实回调链路 + 模块级 `_submit` patch + A5 全链路 e2e ✓

**2. Placeholder scan：** 无 TBD/TODO/`...` 占位；D2 测试复用文件内 `_holding_ctx`/`_run_monitor` 夹具的完整代码；A5 说明块标注了 `_tp_already/_account_id/_trade_id` 保持既有计算位置（非占位）。

**3. Type consistency：**
- `update_order_state_by_broker_oid(lookup_oid, *, state, new_broker_oid, filled_qty, filled_price)`：A1 定义，A2/A5/D1 测试消费，参数名一致。
- `get_order_by_broker_oid(broker_oid) -> dict|None`：A1 定义，A3/A5/D1 消费。
- `get_order_placed_qty(account_id, trade_date, symbol, purpose) -> float`：D1 定义并消费。
- `place_take_profit(symbol, filled_qty, fill_price, order_id)`：D1 定义，A5（经 `_place_take_profit` 薄包装）与 D2 消费。
- `aggregate_fills_by_symbol(start, end) -> dict[str, float]`：A4 定义并消费。
- `set_risk_halt/clear_risk_halt/_risk_halted/is_blocked`：C1 定义，emergency_halt/connect/account-status/health_guard/网关入口消费一致。
- 测试纪律：所有 `_submit` 拦截均为 `monkeypatch.setattr("trading.engine._submit", ...)`，无实例属性遮蔽。

**4. 依赖顺序：** A1→A2→A3→A4→A5 串行（同文件强耦合）；B1/C1/E1/E2/E3 独立可并行；D1 依赖 A；D2 依赖 D1。执行序：A → (B‖C‖E) → D → D'。

**5. 风险点（执行时注意）：**
- D1 差额补挂依赖 OPEN 行 filled_qty（order 事件累计）；若某单缺 order 事件（仅 trade 推送），total_filled 回落入参（本笔量），可能低估——已由 E4/D2 盘中兜底 + 人工对账覆盖，follow-up 可加 query_orders 补全。
- C1 改 connect/账号状态清锁条件，影响所有重连路径——必须回归 `test_qmt_health_guard.py` + `test_qmt_cancel_confirm.py` + live smoke。
- A3 改写 `test_engine.py:1279` 手塞用例时，确保 DB 反查用例覆盖 BUY/SELL/None 三分支。

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-08-01-live-mainchain-fixes.md`. Two execution options:**

**1. Subagent-Driven（推荐）** — 每个 Task 派一个新 subagent 实现，两阶段评审（TDD 红绿 + 风控复核）。Phase A 必须串行（A1→A5），B/C/E 可并行 subagent。

**2. Inline Execution** — 本会话内按 executing-plans 批量执行 + 检查点评审。

**建议：选 1（Subagent-Driven）**。Phase A 改 `_handle_order_update`/`_order_direction` 是实盘成交核心，Task 间需要“红绿验证 + 风控拷问”硬闸。执行前建议先开 worktree 隔离（`superpowers:using-git-worktrees`），避免污染当前工作树。

**Which approach?**
