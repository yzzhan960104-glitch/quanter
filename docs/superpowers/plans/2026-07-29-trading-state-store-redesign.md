# 交易状态存储重构 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) for tracking.

**Goal:** 将散落在 5+ 处的交易状态收口到统一 SQLite 交易状态库（6 张表），解决幂等不闭环 + 撤单漏 + 止盈超卖 + 计划覆盖 veto。

**Architecture:** 新建 `trading/state_store.py`（6 张表 CRUD + 幂等写入）→ 改造 engine 四触发点（DB 幂等替代内存）→ 改造 breaker 撤单（查柜台替代内存）→ 废弃 _tp_placed / live_trades.csv。

**Tech Stack:** Python 标准库 + sqlite3，pytest（asyncio.run），TDD。测试用 `.venv310/Scripts/python.exe`。

**Spec:** `docs/superpowers/specs/2026-07-29-trading-state-store-redesign.md`（6 张表 DDL + ER 关系 + 操作流程 + 迁移波次）

## Global Constraints

- **全中文注释**（CLAUDE.md）：What + Why（交易物理意图/幂等红线/状态真理源）。
- **pytest-asyncio strict**：async 测试用 `asyncio.run(...)`，不加 `@pytest.mark.asyncio`。
- **测试用 `.venv310/Scripts/python.exe`**（系统 python 缺 pandas/xtquant）。
- **每 Task 跑测试 pass + 既有回归零退化才标完成**。
- **SQLite WAL + foreign_keys=ON**：_connect 内 `PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON;`。
- **append-only 表只 INSERT**：trade_event + fill 永不 UPDATE（不可变事件日志）。
- **幂等写 INSERT 冲突返 False**：不抛 IntegrityError，catch 后 log + return False。
- **position_book.py 保留**：state_store.py 是它的扩展（新建模块，不破坏既有）。
- **plan JSON 双写保留**：DB 是真相源，JSON 给人看/veto CLI/钉钉推送。

---

## 阶段 1：state_store.py 地基（6 张表 + CRUD + account 迁移）

### Task 1: state_store.py — 6 张表建表 + schema 迁移

**Spec:** §2.2 DDL · **Files:** `trading/state_store.py`（新）· **Test:** `tests/trading/test_state_store.py`（新）

- [ ] **Step 1: 写失败测试**
  - `test_init_store_creates_6_tables`：init_store 后 PRAGMA table_info 确认 account/trade_event/order/fill/position/account_daily 六张表存在
  - `test_fill_table_has_account_id`：fill 表有 account_id 列（ALTER ADD COLUMN 迁移）
  - `test_position_pk_is_composite`：position PK = (account_id, symbol)
  - `test_foreign_keys_enforced`：插入 trade_event 引用不存在的 account_id → IntegrityError
- [ ] **Step 2: 实现 state_store.py `_connect` + `init_store`**
  - `_connect(db_path)`：WAL + foreign_keys=ON + row_factory + 自动 commit/rollback（复用 position_book 范式）
  - `init_store(db_path)`：6 张表 CREATE + fill 加 account_id 列（ALTER）+ position 重建（加 account_id PK）+ daily_equity 迁移到 account_daily
- [ ] **Step 3: 跑测试 pass + test_position_book 既有回归**

**依赖:** 无 · **验收:** 6 张表 + FK 引用完整性 + fill/position 兼容既有数据

---

### Task 2: state_store.py — account 表 CRUD + .env 迁移

**Spec:** §7.2 · **Files:** `trading/state_store.py` · **Test:** `tests/trading/test_state_store.py`

- [ ] **Step 1: 写失败测试**
  - `test_upsert_account_idempotent`：INSERT OR REPLACE 同 account_id 覆盖不报错
  - `test_get_account`：读 account 配置（broker/name/session_id/mode）
  - `test_migrate_env_to_account`：mock env QMT_ACCOUNT_ID/QMT_USERDATA_PATH → upsert_account → 读取一致
- [ ] **Step 2: 实现 `upsert_account` + `get_account` + `_migrate_env_to_account`**
- [ ] **Step 3: 跑测试 pass**

**依赖:** Task 1 · **验收:** account 表从 .env 迁移 QMT_* 配置

---

### Task 3: state_store.py — trade_event / order / fill 幂等写入

**Spec:** §2.4 幂等键 · **Files:** `trading/state_store.py` · **Test:** `tests/trading/test_state_store.py`

- [ ] **Step 1: 写失败测试**
  - `test_insert_trade_event_idempotent`：同 (account_id, trade_id, action) 重插 → 返 False（UNIQUE 冲突）
  - `test_insert_order_idempotent`：同 (account_id, trade_date, symbol, purpose) 重插 → 返 False
  - `test_insert_fill_idempotent`：同 (order_id, traded_time) 重插 → 返 False
  - `test_insert_trade_event_signal_with_meta`：SIGNAL action 带 meta JSON
- [ ] **Step 2: 实现 `insert_trade_event` / `insert_order` / `insert_fill`**
  - 均 try-except IntegrityError → log + return False（幂等跳过不抛）
- [ ] **Step 3: 跑测试 pass**

**依赖:** Task 1 · **验收:** 三张 append-only 表幂等写入

---

### Task 4: state_store.py — position / account_daily 读写

**Spec:** §2.2 ⑤⑥ · **Files:** `trading/state_store.py` · **Test:** `tests/trading/test_state_store.py`

- [ ] **Step 1: 写失败测试**
  - `test_apply_fill_to_position_buy_weighted`：BUY 100@10 + 100@12 → avg=11.0；SELL avg 不变
  - `test_apply_fill_to_position_zero_clears`：归零 DELETE
  - `test_snapshot_start_equity_idempotent`：INSERT OR REPLACE 同日覆盖
  - `test_snapshot_close_equity_pnl`：close_total - start_total = daily_pnl
- [ ] **Step 2: 实现 `apply_fill_to_position` + `snapshot_start_equity` + `snapshot_close_equity`**
- [ ] **Step 3: 跑测试 pass**

**依赖:** Task 1 · **验收:** position 加权 + account_daily 快照

---

### Task 5: state_store.py — 查询接口

**Spec:** §3.1 查询接口 · **Files:** `trading/state_store.py` · **Test:** `tests/trading/test_state_store.py`

- [ ] **Step 1: 写失败测试**
  - `test_has_order_true_false`：已挂 OPEN → True；未挂 STOP → False
  - `test_get_active_trades`：最新 action 非 CLOSED/EXPIRED/VETOED 的 trade 列表
  - `test_get_pending_orders`：state IN (PENDING/SUBMITTED/PARTIAL) 的 order（撤单用）
  - `test_get_trade_plan_from_signal`：读 trade_event SIGNAL 行的 meta JSON
  - `test_get_entry_dates`：position entry_date 字典（max_holding/trailing 用）
  - `test_get_latest_action`：某 trade_id 的最新 action（当前状态）
- [ ] **Step 2: 实现 `has_order` / `get_active_trades` / `get_pending_orders` / `get_trade_plan` / `get_entry_dates` / `get_latest_action`**
- [ ] **Step 3: 跑测试 pass + 全 state_store 回归**

**依赖:** Task 1-4 · **验收:** 查询接口完整（pre_open/stop_loss/post_close 调用）

---

## 阶段 2：eod_plan 改造（trade_event SIGNAL + CONFIRMED）

### Task 6: eod_plan 落 trade_event + plan JSON 双写

**Spec:** §3.2 + §3.3 · **Files:** `trading/engine.py`（eod_plan）· **Test:** `tests/trading/test_engine.py`

- [ ] **Step 1: 写失败测试**
  - `test_eod_plan_inserts_signal_event`：eod_plan 后 trade_event 有 SIGNAL 行（meta 含计划参数）
  - `test_eod_plan_auto_confirm_event`：AUTO_CONFIRM_PLAN=true → trade_event 有 CONFIRMED 行
  - `test_eod_plan_signal_idempotent`：重跑 eod_plan → SIGNAL 已存在 → 跳过（UNIQUE 幂等）
  - `test_eod_plan_veto_protection`：trade_event VETOED 后重跑 eod_plan → 不覆盖（CONFIRMED 不重写）
- [ ] **Step 2: eod_plan 内 save_plan 后加 `state_store.insert_trade_event(SIGNAL, meta=json.dumps(计划参数))`**
  - AUTO_CONFIRM_PLAN=true 时 insert_trade_event(CONFIRMED)
  - insert 冲突（已有 SIGNAL/CONFIRMED）→ 跳过（幂等）
- [ ] **Step 3: 跑测试 + test_engine_eod_injection 回归**

**依赖:** Task 3 · **验收:** eod_plan 落 DB 事件 + JSON 双写 + veto 保护

---

## 阶段 3：pre_open 改造（查柜台撤单 + DB 幂等挂单）

### Task 7: cancel_all_open_orders 改查柜台

**Spec:** §3.4 · **Files:** `trading/io/breaker.py` + `broker/qmt.py` · **Test:** `tests/trading/test_engine.py`

- [ ] **Step 1: 写失败测试**
  - `test_cancel_uses_query_orders_not_memory`：mock gw.query_orders 返 2 笔可撤单 + gw._orders 空 → 撤 2 笔（不依赖内存）
  - `test_cancel_updates_order_state_db`：撤单后 state_store order.state=CANCELLED
- [ ] **Step 2: 改 cancel_all_open_orders**
  - 旧：`for oid in gw._orders` → cancel
  - 新：`orders = await gw.query_orders(cancelable_only=True)` → 逐个 cancel_order_by_broker_oid → state_store.update_order_state(CANCELLED)
- [ ] **Step 3: broker/qmt.py 加 `cancel_order_by_broker_oid(broker_oid)`**
  - 直接调 `self._trader.cancel_order_stock(account, broker_oid)`，不走 seq→real 映射
- [ ] **Step 4: 跑测试 + test_engine stop_loss pre_open 回归**

**依赖:** Task 1 · **验收:** 撤单查柜台（不依赖 gw._orders 内存）

---

### Task 8: pre_open DB 幂等挂单

**Spec:** §4.1 · **Files:** `trading/engine.py`（pre_open）· **Test:** `tests/trading/test_engine.py`

- [ ] **Step 1: 写失败测试**
  - `test_pre_open_idempotent`：同日 pre_open 调两次 → 只挂一次（has_order OPEN 第二次跳过）
  - `test_pre_open_skips_vetoed`：trade_event 最新 action=VETOED → 跳过该标的
  - `test_pre_open_inserts_order_and_event`：挂单后 order 表有 OPEN 行 + trade_event 有 ORDERED 行
- [ ] **Step 2: pre_open 改造**
  - ③ 遍历 trade_event 最新 action=CONFIRMED 的标的
  - ④ `has_order(account_id, today, symbol, "OPEN")?` → True 跳过 / False insert order(OPEN) + trade_event(ORDERED) → submit
  - submit 后回填 `update_order_state(order_id, "SUBMITTED", broker_oid=seq→real)`
- [ ] **Step 3: 跑测试 + test_engine 回归**

**依赖:** Task 5 + Task 7 · **验收:** pre_open 不再重复挂单（DB UNIQUE 幂等）

---

## 阶段 4：成交回调改造（fill + order 幂等 + 替代 _tp_placed）

### Task 9: _handle_order_update — fill + order(TP1/TP2) 幂等

**Spec:** §4.2 · **Files:** `trading/engine.py`（_handle_order_update）· **Test:** `tests/trading/test_engine_order_update_handler.py`

- [ ] **Step 1: 写失败测试**
  - `test_trade_update_inserts_fill_and_event`：成交回报 → fill 表 + trade_event(FILLED) 写入
  - `test_tp_idempotent_via_db`：同 symbol 成交回报重推 → has_order(TP1)=True → 跳过（替代 _tp_placed 内存）
  - `test_tp_inserts_two_orders`：成交后挂 tp1 + tp2 两笔 order（UNIQUE 幂等）
- [ ] **Step 2: _handle_order_update 改造**
  - d. `state_store.insert_fill` 替代 `position_book.apply_fill`（兼容，state_store 内部调 position_book）
  - `state_store.insert_trade_event(FILLED, qty, price)`
  - `has_order(TP1)?` False → `insert_order(TP1) + insert_order(TP2)` → submit 两张止盈
  - True → 跳过（不再用 _tp_placed）
- [ ] **Step 3: 跑测试 + test_engine_order_update_handler 回归**

**依赖:** Task 3 + Task 5 · **验收:** 成交回调幂等（替代 _tp_placed 内存）

---

## 阶段 5：stop_loss + post_close 改造

### Task 10: stop_loss DB 幂等

**Spec:** §4.3 · **Files:** `trading/engine.py`（stop_loss_monitor）· **Test:** `tests/trading/test_engine.py`

- [ ] **Step 1: 写失败测试**
  - `test_stop_loss_idempotent`：跌破止损 + has_order(STOP) 未终态 → 跳过（不重复发卖）
  - `test_stop_loss_reads_plan_from_db`：从 trade_event SIGNAL meta 读 stop_price（不依赖 plan JSON）
- [ ] **Step 2: stop_loss_monitor 改造**
  - 读 stop_price 改从 `state_store.get_trade_plan(trade_id)` 读 meta
  - 跌破 → `has_order(STOP)?` False → insert order(STOP) + trade_event(STOP_TRIGGERED) → submit
  - True + 未终态 → 跳过
- [ ] **Step 3: 跑测试 + test_engine stop_loss 回归**

**依赖:** Task 5 · **验收:** stop_loss 不重复发卖（DB 幂等）

---

### Task 11: post_close — trade_event(CLOSED) + account_daily

**Spec:** §3.2 post_close + §4.4 · **Files:** `trading/engine.py`（post_close）· **Test:** `tests/trading/test_engine.py`

- [ ] **Step 1: 写失败测试**
  - `test_post_close_inserts_closed_event`：持仓归零 → trade_event(CLOSED, realized_pnl)
  - `test_post_close_snapshot_close_equity`：account_daily 写 close_total_asset + daily_pnl
  - `test_post_close_tp1_filled_event`：止盈成交 → trade_event(TP1_FILLED, realized_pnl)
- [ ] **Step 2: post_close 改造**
  - reconcile 后：查 order FILLED → insert trade_event(TP1_FILLED/STOP_FILLED, realized_pnl)
  - position 归零 → insert trade_event(CLOSED, 总 realized_pnl)
  - snapshot_close_equity: query_asset → account_daily UPDATE
- [ ] **Step 3: 跑测试 + test_engine post_close 回归**

**依赖:** Task 4 + Task 5 · **验收:** post_close 落盈亏 + 账户快照

---

## 阶段 6：废弃清理 + 全链路验证

### Task 12: 废弃 _tp_placed + live_trades.csv

**Files:** `trading/engine.py` · **Test:** `tests/trading/test_engine.py`

- [ ] **Step 1: 写测试** `test_no_tp_placed_memory`：_handle_order_update 不再写 _tp_placed（grep engine.py 无 _tp_placed 赋值）
- [ ] **Step 2: 删 _tp_placed 声明 + 所有引用**（已被 has_order 替代）
- [ ] **Step 3: record_live_trade 改 state_store.insert_fill**（不再写 CSV，或 CSV 保留作 log 但不作为对账源）
- [ ] **Step 4: 跑全测试回归**

**依赖:** Task 9 · **验收:** 无内存态/CSV 当真相源

---

### Task 13: __main__.py 启动期 init_store + account 迁移

**Files:** `trading/__main__.py` · **Test:** `tests/trading/test_main.py`

- [ ] **Step 1: 写测试** `test_main_calls_init_store`：_run_forever 内调 state_store.init_store + _migrate_env_to_account
- [ ] **Step 2: __main__.py _run_forever 内 init_store 替代/补充 position_book.init_db**
- [ ] **Step 3: 跑测试 + test_main 回归**

**依赖:** Task 2 · **验收:** engine 启动期建表 + 迁移 account

---

### Task 14: 全链路 e2e 测试（模拟盘验证）

**Files:** `tests/trading/test_e2e_trading_flow.py`（扩展）

- [ ] **Step 1: 扩展 e2e**
  - 验证全链路：eod_plan(SIGNAL+CONFIRMED) → pre_open(order OPEN 幂等) → 成交回调(fill+TP1/TP2 幂等) → stop_loss(STOP 幂等) → post_close(CLOSED + account_daily)
  - 断言 DB 6 张表数据一致（trade_event 完整事件流 + order 幂等 + fill 增量 + position 汇总）
- [ ] **Step 2: 跑全测试** `pytest tests/trading tests/services tests/experiment -q`
- [ ] **Step 3: commit 全部重构**

**依赖:** Task 1-13 · **验收:** 全链路 e2e + 既有回归零退化

---

## 依赖图

```
Task 1 (建表) ─┬─→ Task 2 (account)
               ├─→ Task 3 (trade_event/order/fill 写入)
               ├─→ Task 4 (position/account_daily)
               └─→ Task 7 (cancel 查柜台)

Task 3 + 4 + 5(查询) ─→ Task 6 (eod_plan)
Task 5 + 7 ──────────→ Task 8 (pre_open 幂等)
Task 3 + 5 ──────────→ Task 9 (成交回调 幂等)
Task 5 ──────────────→ Task 10 (stop_loss 幂等)
Task 4 + 5 ──────────→ Task 11 (post_close)
Task 9 ──────────────→ Task 12 (废弃 _tp_placed)
Task 2 ──────────────→ Task 13 (__main__ init_store)
Task 1-13 ───────────→ Task 14 (e2e 全链路)
```

## 实现顺序 + commit 节奏

| 阶段 | Task | commit |
|---|---|---|
| 1 地基 | T1-T5 | feat(state_store): 统一交易状态库 6 张表 + CRUD + 幂等写入 + 查询 |
| 2 信号 | T6 | feat(trading): eod_plan 落 trade_event(SIGNAL+CONFIRMED) + plan 双写 + veto 保护 |
| 3 挂单 | T7-T8 | feat(trading): cancel 查柜台 + pre_open DB 幂等挂单 |
| 4 成交 | T9 | feat(trading): 成交回调 fill + order(TP1/TP2) 幂等（替代 _tp_placed） |
| 5 出场 | T10-T11 | feat(trading): stop_loss DB 幂等 + post_close trade_event(CLOSED) + account_daily |
| 6 清理 | T12-T14 | chore(trading): 废弃 _tp_placed 内存 + live_trades.csv + e2e 全链路验证 |

## 验收（Definition of Done）

- [ ] state_store.py 6 张表 + 全部 CRUD + 幂等写入 + 查询接口
- [ ] eod_plan 落 trade_event(SIGNAL/CONFIRMED) + plan JSON 双写
- [ ] pre_open 查柜台撤单 + DB 幂等挂单（has_order）
- [ ] 成交回调 fill 幂等 + order(TP1/TP2) 替代 _tp_placed
- [ ] stop_loss DB 幂等（has_order STOP 跳过）
- [ ] post_close trade_event(CLOSED + realized_pnl) + account_daily
- [ ] _tp_placed 废弃 + cancel 查柜台（不依赖 gw._orders）
- [ ] e2e 全链路（trade_event 完整事件流 + order/fill/position 一致）
- [ ] 全测试 pass + 既有回归零退化
- [ ] 全中文注释（CLAUDE.md）
