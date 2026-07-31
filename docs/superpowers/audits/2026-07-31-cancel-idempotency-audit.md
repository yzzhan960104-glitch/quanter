# C-3 cancel 幂等审计结论

- **日期**：2026-07-31
- **分支**：feat/c4-error-grading-scheduler-hardening
- **Task**：C-4 Task 7（U5）
- **结论**：`pre_open` 撤昨日单调 `_cancel_all_open_orders(gw, account_id=_resolve_account_id())`，
  激活柜台路径 `cancel_order_by_broker_oid_db` 把 `order.state` 回写 `CANCELLED`。
  **不新增 `purpose='CANCEL'` 行**（判据：撤单落 DB 即免 CANCEL 行）。

## 问题根因

`cancel_all_open_orders`（`trading/io/breaker.py:48`）柜台路径 `_cancel_via_broker_query`
（`:95`）的 DB 回写是「条件激活」—— 仅当 `account_id` 提供时（`:120 if account_id:`）才调
`state_store.cancel_order_by_broker_oid_db(broker_oid)`（`:122`）回写 `order.state=CANCELLED`。

`pre_open` 原调用 `_cancel_all_open_orders(gw)`（`engine.py:705`）**未传 `account_id`** →

- 撤单指令已发往柜台（昨日未成交单确实被撤）；
- 但 DB `order` 行 `state` 仍记 `SUBMITTED`（回写路径未激活）；
- T+1 对账时 `has_order(OPEN)` / `get_latest_action` 以为单子还活着 → 幽灵单 / 重复挂 / FK 漂移。

## 修法（最小改动 · 不重建 C-1）

`engine.py:705` 一行改：

```python
# 修复前
_cancel_res = await _cancel_all_open_orders(gw)
# 修复后
_cancel_res = await _cancel_all_open_orders(gw, account_id=_resolve_account_id())
```

激活既有回写路径即可，无需：

- 新增 `purpose='CANCEL'` 行（spec §6.1 判据决策树）；
- 改 `cancel_all_open_orders` 本身（breaker.py 不动）；
- 新建 CANCEL 专用 trade_event（撤单不是独立 trade，是既有 order 的状态迁移）。

## 判据决策树（spec §6.1）

撤单确认是否落 DB？

- **是**（`cancel_order_by_broker_oid_db` 回写 `CANCELLED`，`account_id` 提供时）
  → 免 `purpose='CANCEL'` 行。**本结论。**
- **否**（`account_id=None` / 内存回退路径 / 柜台无 `broker_oid` 可查）
  → 幽灵单风险，须补 `purpose='CANCEL'` 行 或 `trade_event(action=CANCEL)` 兜底审计。

边界：`cancel_all_open_orders` 在 `gw=None` 或柜台查询失败时回退内存路径（不回写 DB），
此场景 pre_open 已有 warning + 不阻塞挂单的口径（`engine.py:697`），属已知可观测的「软降级」，
不属幽灵单（幽灵单 = 「以为撤了 DB 没记」，内存回退 = 「明告没撤」）。

## UNIQUE 覆盖度 grep（执行命令 + 真实输出）

执行命令：

```bash
grep -nE "UNIQUE\(|def (insert_order|insert_trade_event|insert_fill|update_order_state)" trading/state_store.py
```

真实输出（2026-07-31 执行）：

```
19:  trade_event  UNIQUE(account_id, trade_id, action)    —— 事件重推跳过
20:  order        UNIQUE(account_id, trade_date, symbol, purpose) —— 重复挂单跳过
21:  fill         UNIQUE(order_id, traded_time)           —— 成交回报重推跳过（部分成交增量幂等）
130:                UNIQUE(account_id, trade_id, action)
152:                UNIQUE(account_id, trade_date, symbol, purpose)
171:                UNIQUE(order_id, traded_time)
297:def insert_trade_event(account_id: str, trade_id: str, symbol: str, action: str, *,
326:def insert_order(order_id: str, trade_id: str, account_id: str, trade_date: str,
358:def update_order_state(order_id: str, state: str, *, broker_oid: str | None = None,
405:def insert_fill(order_id: str, account_id: str, traded_time: str, symbol: str,
```

### 覆盖度结论

| 写入路径 | 守护 UNIQUE / PK | 命中行 |
|----------|------------------|--------|
| `insert_trade_event` | `UNIQUE(account_id, trade_id, action)` | :130 |
| `insert_order` | `UNIQUE(account_id, trade_date, symbol, purpose)` | :152 |
| `insert_fill` | `UNIQUE(order_id, traded_time)` | :171 |
| `update_order_state` | （状态迁移，非新增行；幂等由 order PK 守护 + 撤单回写复用此路径） | :358 |
| `account_daily` | `PRIMARY KEY (account_id, date)` | :198 |

**所有交易写入路径（`insert_order` / `insert_trade_event` / `insert_fill`）均过 UNIQUE，
无遗漏。** `update_order_state` 是状态迁移（UPDATE 已有行，非 INSERT），其幂等由
`order` 表的 `order_id PRIMARY KEY` + 撤单回写复用同一行的 `purpose`（OPEN/STOP/TP1/TP2，
不新增 CANCEL purpose）守护。

## 验证

- 新增测试：`tests/trading/test_cancel_all_account_id.py`（spy 捕获 `account_id` 关键字参数）。
- 回归：`tests/trading/test_engine_pre_open_gate.py` 13 passed 零退化。
- 物理断言：修复前 spy 捕获 `account_id=None`（RED），修复后 `account_id=='ACC_QMT_001'`（GREEN）。

## Follow-up（非本期）

- `purpose` 当前隐含 `side+op`（OPEN/STOP/TP1/TP2），`side` 显式进 UNIQUE 为冗余增强
  （防同标的双向单 purpose 碰撞）—— review 提及但本期不做，记 follow-up。
- 内存回退路径（`account_id=None` / 柜台无 `broker_oid`）若未来成为生产路径，
  需补 `trade_event(action=CANCEL)` 兜底审计行—— 当前属明告「没撤」的可观测软降级，不补。
