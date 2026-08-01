# 实盘主链路修复设计（live-mainchain-fixes）

- **日期**：2026-08-01
- **分支**：master 审查产出（在 feat/e2e-long-cycle 上落盘）
- **状态**：待审（spec review gate）
- **关联**：`docs/superpowers/plans/2026-08-01-live-mainchain-fixes.md`（旧版 plan，本 spec 重设计后以其为设计依据，plan 待重写）；master 深度审查结论（10 条已核实缺陷 + 测试元问题）

---

## 1. 背景与现状

### 1.1 痛点（10 条已核实缺陷）

| # | 缺陷 | 根因 | 影响 |
|---|---|---|---|
| 1 | 成交回报方向恒 None | 主推路径 `_orders` 无 `order_type`（`on_stock_order/trade` 解析不含该字段；`submit_order` 不写 `_orders`），`_order_direction` 只查内存 | 止盈不挂、fill/position 不落库、账本链死 |
| 2 | data_ready 跨日 key 错位 | `pipeline_then_eod`/`run_data_check t2` 落 `data_ready(T)`，`pre_open(T+1)` gate③ 查 `get_data_ready(T+1)` | pre_open 每天被 gate 拦截，无单可挂 |
| 3 | CSV 混写 submit/fill | `submit_order` 对 REJECTED/FAILED 也落 BUY/SELL 行；成交回报再落一次；post_close ② 全量聚合 | 拒单计入持仓 + 双计数 = 幻影持仓 |
| 4 | 止盈重复挂单超卖 | 调度点只查 `has_order(TP1)`；`_submit` 在 `_record_tp`（UNIQUE 幂等）之前；冲突仅 log | 部分成交/重推重复 submit TP2 → 柜台卖单 > 持仓 |
| 5 | order 状态/柜台单号不推进 | `kind!='trade'` 全丢弃（async_response 被丢）；trade 分支不 `update_order_state`；DB `broker_oid` 恒为 `str(seq)` | 撤单回写 0 行（幽灵单）、`TP1_FILLED/TP2_FILLED`/realized_pnl 永不落、pending 永含已成交单 |
| 6 | 熔断不粘滞 | `emergency_halt`/`on_disconnected` 共用 `_lock_down`；`connect()` 无条件清锁；`_health_guard` 60s 自动重连 | 紧急熔断/日内 -3% 熔断约 1 分钟被自动解除 |
| 7 | 超期平仓崩溃重复卖 | 卖单提交后、消费标记前崩溃 → 标记还在 → 重启重复挂卖 | 窄窗口卖超 |
| 8 | 撤单计数失真 | `_cancel_via_broker_query` 不检查 `OrderResult.state`，失败也计 `cancelled` | 撤单质量告警误导 |
| 9 | 状态 51 过早终态 | `_map_qmt_status(51)` 映射 CANCELLED，`_confirm_cancelled` 终态集合含 CANCELLED | 撤单刚受理即当已撤，漏报后续成交/撤单失败 |
| 10 | TP 漏挂盘中无兜底 | `stop_loss_monitor` 对 `CLOSE/TAKE_PROFIT` 直接 `continue`，不校验 TP 是否真挂 | 止盈漏挂后盘中不补发，持仓拖到止损/超时 |

### 1.2 元问题（本 spec 的第一根因）

现有测试大量手工注入内存态（如 `eng._gw._orders = {"1": {"order_type": 23}}`）绕过真实回调链路，导致 #1/#5 这类“生产主推路径必现、测试全绿”的缺陷长期存活。

**红线：** 凡涉及成交回报的测试，必须驱动真实链路（构造 XtOrder/XtTrade 对象 → `gw.on_stock_order/on_stock_trade/on_order_stock_async_response` → `_process_order_update` → `_handle_order_update`），禁止手工塞 `_orders`/`_submit` 内存态绕过。

### 1.3 根因归纳（三类）

1. **回调解析与时序竞态**：网关解析层缺字段（order_type）、内存记录被覆盖（trade 覆盖 order 记录）、async_response 与 trade/order 到达顺序不保证。
2. **跨日口径**：data_ready 落 T、读 T+1；eod 已修（next_trading_day），data_ready 链漏修。
3. **双真相源与账本回写**：CSV/内存/DB 三处记成交且互相回写；order 行从不推进终态；broker_oid 用错命名空间。
4. **风控状态粘滞缺失**：熔断与断线共用一标志，自动自愈路径把人工/机器风控动作一并解除。

---

## 2. 目标与非目标

### 目标

1. 成交回报 → 止盈挂单 → 账本落库 → 持仓更新 → 盘后对账整条链在生产主推路径下真实闭环（#1/#3/#5）。
2. 跨日 gate 对齐（#2）：T+1 日 pre_open 命中 T 日 data_ready。
3. 熔断粘滞（#6）：风控锁只能显式解除，网络断线仍可自愈。
4. 止盈幂等且覆盖完整（#4）：部分成交/重推不超卖、不遗漏未覆盖仓位。
5. 收尾硬化（#7/#8/#9/#10）：超期平仓幂等、撤单计数诚实、状态 51 保守、TP 盘中兜底。
6. 消灭测试元问题：全部关键用例走真实回调链路 + 真实跨日，禁手塞内存态；补竞态用例。

### 非目标（显式 out of scope）

- plan→SQLite 全量改造、live P0 四项（部分成交精确重放/熔断阈值调参/trailing 盘中演进/EMT 急诊平仓）不在本 spec 范围。
- 多账户支持、前端展示改造、CSV 历史数据回填清洗。
- `_orders` 内存态的彻底删除（保留为加速缓存，不再承担真相源职责）。

---

## 3. 总体架构

### 3.1 真相源原则

- **state_store SQLite 是唯一真相源**：order/fill/position/trade_event/data_ready 的读写判定一律以 DB 为准。
- 内存态（`gw._orders` / `_seq_to_real` / `_seq_to_client`）仅作加速缓存；**方向/状态判定 DB 优先，DB miss 才回退内存**，且回退必须显式记日志。
- CSV 是审计导出层，不再作为对账聚合的输入（post_close 改读 DB/专用聚合函数，见 4.7）。
- 所有新写库失败按 C-4 分级：order/fill/position 主链路写失败（insert_fill/apply_fill/insert_order） = L1 停调度；broker_oid 回填 / order 状态推进 / trade_event 等可补偿写失败按 §6 分级（CRITICAL/WARN，靠后续事件补推进）；审计/告警/止盈旁路失败 = CRITICAL 告警；通知失败 = WARN。

### 3.2 订单生命周期状态机（真相源）

```
pre_open
  insert_order(OPEN, state=PENDING, broker_oid=None)
  → _submit 成功 → update_order_state(SUBMITTED, broker_oid=str(seq), submitted_at)
  → _submit 失败/拒 → update_order_state(REJECTED/FAILED)

on_order_stock_async_response（kind=async_response）
  → 按 broker_oid=str(seq) 定位行，回填 broker_oid=str(real_order_id)
  → 行不存在（异常路径）→ WARN，不 halt（pre_open 未落库/已被清理）

on_stock_order（kind=order，柜台状态推送，含累计 traded_volume）
  → 按 broker_oid=real（miss 时经 _seq_to_real 反查 seq 再匹配）推进：
     55 部成      → PARTIAL
     56 已成      → FILLED
     54 已撤      → CANCELLED
     53/52 部撤   → PARTIAL_CANCELLED
     57 废单      → REJECTED
     48/49/50/51/255 → SUBMITTED（51 已报待撤保守不终态）
  → filled_qty = 累计 traded_volume；filled_price = traded_price

on_stock_trade（kind=trade，本笔增量成交）
  → insert_fill（幂等 UNIQUE(order_id, traded_time)）
  → apply_fill_to_position（加权 avg、entry_date 锁定、归零删除）
  → insert_trade_event(FILLED)（幂等 UNIQUE(account_id, trade_id, action)）
  → record_live_trade(kind="fill")（CSV 审计）
  → 止盈差额补挂（4.5）

post_close
  → 查 state='FILLED' 的 TP1/TP2 行 → TP1_FILLED/TP2_FILLED + realized_pnl
  → 对账（local_positions 来自 position 表，broker 来自 _fetch_broker_positions）
```

### 3.3 handler 事件流与顺序

`_handle_order_update` 按 kind 分发三支：

1. `async_response`：只回填 broker_oid，不动 state。
2. `order`：推进 order state/filled_*（本 spec 新增，见 4.3）。
3. `trade`：**先落真相源 → 再挂止盈 → 再写审计 CSV → 最后通知**（顺序 d→c→a→b，解决“TP 已挂但账本没记”的 crash 窗口）。

其余 kind（order_error/cancel_error）仍由风控层负责，本 handler 不处理。

---

## 4. 组件设计

### 4.1 broker/qmt.py 解析层

- `on_stock_order` 的 parsed dict **新增 `order_type` 字段**（`getattr(order, "order_type", 0)`），与 `query_orders` 同源。
- `_process_order_update` 改为 **merge 语义**（`rec = {**self._orders.get(order_id, {}), **dict(update)}`），禁止 trade/async_response 事件覆盖掉 order 事件的 order_type 等字段。
- `_map_qmt_status(51)` 从 CANCELLED 分支移除，落末尾 `return OrderState.SUBMITTED`（等 54 真撤或 query_orders 推进）。
- `connect()` 清锁逻辑改造见 4.6。

### 4.2 state_store 新接口

```python
def get_order_by_broker_oid(broker_oid: str, *, db_path: str | None = None) -> dict | None:
    """按 broker_oid 列查 order（成交回报/方向反查用）。"""

def update_order_state_by_broker_oid(
    lookup_oid: str,
    *,
    state: str | None = None,
    new_broker_oid: str | None = None,
    filled_qty: float | None = None,
    filled_price: float | None = None,
    db_path: str | None = None,
) -> int:
    """按 broker_oid 列定位更新 order；lookup_oid 为定位值（回填前是 seq，回填后是 real）。
    返回 rowcount（0=未命中，调用方必须处理竞态，见 5.2）。"""
```

- 幂等语义不变：`has_order` 排除 REJECTED/FAILED/CANCELLED；`insert_order` UNIQUE 冲突返 False；`cancel_order_by_broker_oid_db` 复用。
- `update_order_state`（按主键）与 `update_order_state_by_broker_oid`（按柜台单号）职责分离，前者保留。

### 4.3 engine._handle_order_update（主战场）

入口按 kind 分发（替代现在的 `kind != "trade" 直接 return`）：

```python
kind = update.get("kind")
if kind == "async_response":
    seq_str = str(update.get("seq", ""))
    real = str(update.get("order_id", ""))
    if seq_str and real and real != seq_str:
        n = _state_store.update_order_state_by_broker_oid(
            seq_str, new_broker_oid=real)
        if n == 0:
            logger.warning("async_response 未命中 DB 行 seq=%s real=%s（可能 pre_open 未落库）", seq_str, real)
    return

if kind == "order":
    _advance_order_state_from_status(update)   # 见下
    return

if kind != "trade":
    return
```

`_advance_order_state_from_status`：

1. `lookup = str(update.get("order_id"))`（柜台真实单号）。
2. 先按 `get_order_by_broker_oid(lookup)` 定位；miss 时经 `gw._seq_to_real` 反查 `real→seq`，按 `broker_oid=str(seq)` 再试（竞态兜底，见 5.2）。
3. 命中 → `state = _map_db_state(update["state"])`（PARTIAL_FILLED→"PARTIAL"、FILLED→"FILLED"、CANCELLED/REJECTED 透传字符串），`filled_qty = 累计 traded_volume`（非本笔增量）、`filled_price = traded_price`，调 `update_order_state_by_broker_oid`。
4. rowcount==0 → WARN + 记 degraded 计数（供 ReportBuilder/告警），不 halt（订单可能来自 server 手动路径）。

trade 分支顺序（d→c→a→b）：

- d：`insert_fill`（幂等 False 即重推，跳过 apply）→ `apply_fill_to_position` → `insert_trade_event(FILLED)`。`insert_fill/apply_fill` 异常 = **L1 `_CriticalHalt`**（敞口真相失真）；trade_event 写失败 = CRITICAL 告警（事件可补，不 halt）。
- c：方向为 BUY 时调 `place_take_profit(symbol, ...)`（模块级，见 4.5）；方向未知时 CRITICAL 告警 + 跳过止盈（不静默）。
- a：`record_live_trade(..., kind="fill")` 失败 WARN。
- b：`fire_and_forget(notify_trade_event)` 失败 WARN。

### 4.4 方向反查链（#1）

`_order_direction(order_id)` 判定链：

1. DB 优先：`get_order_by_broker_oid(order_id)` 的 `side` → BUY/SELL。
2. DB miss：`_seq_to_real` 反查 seq，再按 `broker_oid=str(seq)` 查一次。
3. 内存兜底：`gw._orders[order_id].order_type`（4.1 merge 后主推路径也有该字段）。
4. 全 miss：返回 None，**调用方必须 CRITICAL 告警**（成交方向未知 = 审计黑洞，人工对账兜底），不得静默。

### 4.5 止盈差额补挂（#4 重设计）

把 `TradingEngine._place_take_profit` 提为**模块级函数** `async def place_take_profit(symbol, filled_qty, fill_price, order_id)`，实例方法保留为薄包装（兼容现有调用点与测试），`stop_loss_monitor` 的盘中兜底直接调模块级版本（解决原 plan E4 的 `self` 不存在问题）。

**差额补挂算法**（同时解决重复超卖与覆盖缺口）：

```
total_filled = OPEN 行 filled_qty（order 事件累计维护；miss 时用 position.qty）
tp1_target  = int(total_filled * tp1_portion / 100) * 100   # 整手向下
tp2_target  = total_filled - tp1_target
placed1     = SUM(qty) of TP1 行（未终态 state）
placed2     = SUM(qty) of TP2 行（未终态 state）
need1       = tp1_target - placed1
need2       = tp2_target - placed2
need1 > 0 → submit TP1(need1)；need2 > 0 → submit TP2(need2)
submit 前仍做 has_order(TP1/TP2) 预检（双保险，防 await 窗口重入）
_record_tp 冲突 → ERROR 告警（人工复核，绝不静默）
```

退化分支（tp1 缺失 / portion=0 / tp1>=tp2）：`need2 = total_filled - placed2`。

**为什么差额而非全量**：部分成交（如 300 股分 3 笔）时，全量重挂会重复；只按“目标量 - 已挂量”补挂，既不超卖也不留未覆盖仓。回报重推时 `total_filled` 不变 → `need=0` → 零 submit，天然幂等。

`stop_loss_monitor` TP 分支（现 `continue` 处）：先 `has_order(TP1) or has_order(TP2)`，均 False → `place_take_profit(...)` 补挂 + WARNING 告警，再 continue。

### 4.6 熔断粘滞（#6）

网关新增 `_risk_halted: bool = False`，与 `_lock_down` 分离：

- `set_risk_halt(True)`：置 `_risk_halted=True + _lock_down=True + _connected=False`（emergency_halt 与日内 -3% 熔断调用）。
- `clear_risk_halt()`：只清 `_risk_halted`；`_lock_down` 由后续 connect 自然恢复。
- `connect()` 成功：`_lock_down=False` **仅当 `not _risk_halted`**。
- `_on_account_status_change` OK(0)：清 `_lock_down` **仅当 `not _risk_halted`**。
- `submit_order`/`cancel_order`/`query_asset`/`query_orders`/`_fetch_broker_positions` 入口统一检查 `_risk_halted or _lock_down`（新增 `is_blocked` 属性收口，禁止散落只查 `_lock_down`）。
- `_health_guard`：`_risk_halted` 时只 WARNING + 跳过重连。
- 解锁：人工 `clear_risk_halt()` 后，health_guard 或手动 connect 恢复（网络断线路径不受影响，仍自愈）。
- `emergency_halt` 幂等：已 `_risk_halted` 直接返回“已处于熔断态”。

### 4.7 CSV 审计层（#3）

- `LIVE_TRADE_COLUMNS` 新增 `kind` 列；`record_live_trade(..., kind: str = "fill")`。
- 落点：submit_order 审计行 `kind="submit"`（含 REJECTED/FAILED）；成交回报行 `kind="fill"`。
- 兼容：读旧行 `r.get("kind")` 缺失 → **默认 `"submit"`**（保守：历史 CSV 主要是 submit 行，默认 fill 会在升级当天把上午旧格式行当成交，重新引入幻影持仓）。
- 新增 `aggregate_fills_by_symbol(start: str, end: str) -> dict[str, float]`：流式读 CSV，只统计 `kind=="fill"` 且 direction∈{BUY,SELL} 的行，**不走 `query_trades` 的 limit=1000 分页**（避免单日超 1000 行截断）。
- `post_close` ② 段改用 `aggregate_fills_by_symbol` 作为净持仓来源；`query_trades`/`export_trades` 透出 kind 列（`.get` 兼容）。

### 4.8 data_ready 跨日 gate（#2）

- `_pre_open_gate` ③ 段改查 `expected_latest_trade_day(clock.now())`（T+1 日 09:22 → 最近已收盘交易日 T），不再查 `date` 参数本身。
- 落库口径不变：pipeline/T2 仍落 `data_ready(T)`（语义 = “T 日数据就绪”）。
- 测试：新增真实跨两日用例（pipeline 落 T、pre_open(T+1) 命中）；既有同日冻结用例保留（修复后仍绿，因 expected_latest_trade_day(19:00)=当日）。

### 4.9 超期平仓幂等（#7）

`_close_expired_positions` 循环内：

1. 每只挂卖前 `has_order(account_id, today, symbol, "EXPIRED_CLOSE")`，True 跳过。
2. `_submit` 成功后 `insert_order(..., purpose="EXPIRED_CLOSE", state="SUBMITTED")`；失败回填 REJECTED（同 pre_open 范式）。
3. 标记文件消费时机不变（循环后）——DB 幂等兜住“提交后、消费前崩溃”的窄窗口。

### 4.10 撤单计数（#8）

`_cancel_via_broker_query`/`_cancel_via_memory`：

- 检查 `OrderResult.state`（或 dict 的 `state`）：FAILED/REJECTED → 计入新增 `failed` 计数并 WARNING；发起成功 → `cancelled`；发起成功但确认超时 → `unconfirmed`。
- 返回 dict 增加 `failed` 键（向后兼容，旧调用方只读 cancelled/unconfirmed 不受影响）。

---

## 5. 数据流与时序

### 5.1 正常路径

```
pre_open 落 OPEN(SUBMITTED, broker_oid=seq)
  → async_response(seq, real) 回填 broker_oid=real
  → on_stock_order(real, status=55/56, 累计量) 推进 PARTIAL/FILLED + filled_qty
  → on_stock_trade(real, 本笔量) 落 fill/position/FILLED 事件 + CSV(fill) + 止盈差额补挂
  → post_close 查 TP 行 state=FILLED → TP1_FILLED/TP2_FILLED + realized_pnl
```

### 5.2 竞态路径（async_response 晚到）

```
on_stock_order(real) 先到：按 real 查 DB miss → _seq_to_real 反查 seq → 命中 → 推进 state
on_stock_trade(real) 先到：方向反查 DB miss → seq 反查命中 side → 落账 + 差额补挂
async_response 后到：按 broker_oid=str(seq) 回填 real，不覆盖 state/filled_*
```

- 若 `_seq_to_real` 也无映射（极端）：order 事件内存记录（4.1 merge 后含 order_type）兜底方向；DB 状态推进记 WARN + degraded 计数，等 async_response 后由下一次 order/trade 事件补推进。
- 所有 rowcount==0 的更新点必须显式 WARN/告警，禁止静默。

---

## 6. 错误处理与降级分级

| 位置 | 失败场景 | 等级 |
|---|---|---|
| trade 分支 d 段 | insert_fill / apply_fill_to_position 异常 | L1 `_CriticalHalt`（敞口真相失真） |
| trade 分支 d 段 | insert_trade_event(FILLED) 失败 | CRITICAL 告警（事件可补，不 halt） |
| trade 分支 c 段 | 止盈挂单失败 / 方向未知 | CRITICAL 告警（人工补挂/对账） |
| async_response / order 状态推进 | rowcount==0（未命中） | WARN + degraded 计数 |
| CSV 写入 / 钉钉通知 | 任意异常 | WARN（旁路） |
| async_response 回填 DB 写异常 | 非 rowcount==0 的真实写异常 | CRITICAL 告警（撤单/对账锚点失效） |
| 网关入口 | `_risk_halted or _lock_down` | 拒单（REJECTED/空结果），不抛 |

---

## 7. 测试策略

### 7.1 防假绿规范（硬约束）

- 禁止 `eng._gw._orders = {...}` 手工注入内存态绕过回调；涉及成交回报的用例必须驱动 `on_stock_*`/`on_order_stock_async_response` → `_process_order_update` → `_handle_order_update`。
- 禁止 `eng._submit = MagicMock(...)`（实例属性遮蔽不了模块级 `_submit`）；必须 `monkeypatch.setattr("trading.engine._submit", AsyncMock(...))`。
- `place_take_profit` 相关测试必须装配：plan（take_profit/tp1/tp1_portion）+ account 行 + OPEN 行；防止“无计划早退”假绿。
- 断言以 DB 状态/rowcount 为准，不只看 mock 被调用。

### 7.2 关键用例清单

| 用例 | 验证 |
|---|---|
| async_response 回填 broker_oid（真实回调链路） | #5 第一刀 |
| order 事件推进 PARTIAL/FILLED + 累计 filled_qty | #5 第二刀（精确部分成交，非近似） |
| trade 事件落 fill/position/FILLED 事件 + CSV kind=fill | #1/#3 |
| 方向反查 DB 优先 + seq 反查回退 + 内存兜底三分支 | #1 |
| trade 先于 async_response（竞态） | 5.2 全链路仍闭环 |
| 部分成交 3 笔：TP 差额补挂不超卖、不留未覆盖仓 | #4 |
| 回报重推（同 order_id+traded_time）：fill 幂等 + TP 零重复 submit | #4 |
| risk_halted：health_guard 重连不清、account OK 不清、submit 拒单、clear 后恢复 | #6 |
| 跨两日：pipeline 落 data_ready(T) → pre_open(T+1) gate 绿 | #2 |
| EXPIRED_CLOSE 幂等：已挂不重卖 | #7 |
| breaker 撤单 FAILED 计 failed 不计 cancelled | #8 |
| 51 映射 SUBMITTED + _confirm_cancelled 不提前终态 | #9 |
| TP 漏挂盘中补挂 + WARNING | #10 |
| A5 全链路 e2e：真实回调 → FILLED → TP 挂出 → 账本写入 | 元问题 |

---

## 8. 验收标准（映射）

1. **#1**：真实回调链路下 direction 正确；fill/position/FILLED 事件落库；TP 自动挂出。
2. **#2**：T+1 pre_open gate 全绿（跨两日用例）。
3. **#3**：CSV 有 kind 列；post_close 聚合只认 fill；submit 拒单不再产生持仓。
4. **#4**：部分成交/重推下 TP1+TP2 总量 = 目标量（不超卖、无缺口）；`_record_tp` 冲突必告警。
5. **#5**：order.broker_oid=真实单号；state 推进 FILLED/PARTIAL（由 order 事件驱动，精确）；post_close TP1_FILLED/TP2_FILLED/realized_pnl 落库；get_pending_orders 不含已成交单。
6. **#6**：risk_halted 粘滞；health_guard/account-OK 均不清；解锁须显式 clear_risk_halt。
7. **#7**：EXPIRED_CLOSE DB 幂等，重复调用零重复 submit。
8. **#8**：breaker 返回含 failed，cancelled 只计成功。
9. **#9**：51→SUBMITTED，撤单确认等真终态。
10. **#10**：TP 漏挂时盘中补挂 + WARNING。
11. **元问题**：成交回报相关测试全部走真实回调链路；无手塞内存态用例残留；竞态用例存在且绿。

---

## 9. 风险与缓解

| # | 风险 | 缓解 |
|---|---|---|
| R1 | order 事件推进依赖 `_seq_to_real` 反查，映射缺失时状态不推进 | 内存 order_type 兜底方向 + WARN/degraded 计数 + 下次事件补推进；query_orders 兜底列入 follow-up |
| R2 | 差额补挂把 TP 从“一次性挂单”改为“多次补挂”，盘中调用频率上升 | 仅在 trade 事件触发（低频），且 need=0 时零 I/O；has_order 预检防并发 |
| R3 | `_risk_halted` 与既有 `_lock_down` 语义分叉，漏改某入口 | 网关所有入口统一走 `is_blocked` 属性收口；测试覆盖 submit/cancel/query 三入口 |
| R4 | 老 CSV 默认 kind="submit" 会让升级当天历史 fill 行不计入对账（保守漏计） | 方向是“宁可漏计不幻影”，部署日由人工对账复核；文档明示 |
| R5 | connect() 清锁条件改动影响所有重连路径 | 回归 health_guard/cancel_confirm/live smoke 全套 |
| R6 | 部分成交场景下 OPEN.filled_qty 与 position.qty 可能短暂不一致 | 以 order 事件累计量为主、position.qty 兜底；差额补挂天然幂等 |

---

## 10. 实施阶段（供重写 plan 用）

```
Phase A（成交链路真相源，最高杠杆，串行）：
  A1 async_response 回填 broker_oid（qmt/engine/state_store）
  A2 on_stock_order 推进 state + order_type 解析 + _process_order_update merge
  A3 方向反查链（DB→seq→内存）+ 未知方向告警
  A4 CSV kind 列 + aggregate_fills_by_symbol + post_close 换源
  A5 trade 分支顺序 d→c→a→b + 错误分级 + 全链路 e2e（含竞态用例）

Phase B（#2 data_ready 跨日，独立）：_pre_open_gate ③ 改 expected_latest_trade_day + 跨两日用例

Phase C（#6 熔断粘滞，独立）：_risk_halted + is_blocked 收口 + health_guard/account-OK 不清锁

Phase D（#4 止盈差额补挂，依赖 A）：模块级 place_take_profit + 差额算法 + 冲突告警

Phase D'（#10 盘中 TP 兜底，依赖 D）：stop_loss_monitor TP 分支补挂 + WARNING

Phase E（#7/#8/#9，独立小改）：EXPIRED_CLOSE 幂等 / breaker failed 计数 / 51 映射

执行序：A → (B ‖ C ‖ E) → D → D'；每 Phase 独立 PR。
```

---

## 11. Spec review 要点

1. **真相源单一化**：方向、状态、broker_oid、对账聚合全部以 DB 为准，内存/CSV 降级为缓存/审计——接受。
2. **竞态显式处理**：async_response 晚到经 `_seq_to_real` 反查回退 + rowcount==0 必告警——接受。
3. **部分成交精确化**：order 事件（累计量）驱动 FILLED/PARTIAL，替代旧 plan 的“保守标 FILLED”——接受。
4. **TP 差额补挂**：目标量 - 已挂量，同时解决超卖与覆盖缺口——接受。
5. **风控粘滞**：`_risk_halted` 与断线分离，网关入口统一 `is_blocked`——接受。
6. **测试防假绿**：真实回调链路 + 模块级 patch + 必需装配清单——接受。

spec 通过后重写 plan（`docs/superpowers/plans/2026-08-01-live-mainchain-fixes.md`，替换旧版）。
