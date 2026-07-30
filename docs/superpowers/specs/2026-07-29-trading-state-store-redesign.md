# 2026-07-29 交易状态存储重构设计（统一状态真理源）

> **范围**：将当前散落在 5+ 处（gw._orders 内存 / _tp_placed 内存 / position_book SQLite / live_trades.csv / trading_plan JSON）的交易状态收口到一个统一的 SQLite 交易状态库，解决状态碎片化（C-1）+ 幂等不闭环（C-3）+ 撤单漏（P0-4）+ 止盈超卖（P0-1）+ 计划覆盖 veto（P0-3）。
> **物理定位**：`trading/state_store.py`（新模块，替代 position_book 的部分职责）+ `trading/engine.py`（四触发点改造）+ `trading/trading_plan.py`（plan 落 DB）。
> **设计哲学**：append-only 事件日志（trade_event + fill）+ DB UNIQUE 幂等 + 柜台查询替代内存（撤单/状态）+ 不可变审计层。

## 1. 背景与目标

当前交易状态散落在 5+ 处互不同步的存储，互相视为对账基准。今天实测暴露了 3 个致命问题：
- pre_open 不幂等 → 重复挂单 → 超买 3 倍（12,900 股 vs 计划 4,300）
- cancel_all_open_orders 只读 gw._orders 内存 → 新连接空 → 撤不掉柜台旧单
- _tp_placed 内存态 → engine 重启清空 → 重连重推 → 重复挂止盈超卖

**根因（设计层面）**：缺一个权威的、事务一致的、跨重启的、幂等保护的交易状态存储。

## 2. 统一状态库 schema（6 张表）

### 2.1 ER 关系图

```
account（账号配置）
  │ 1:N
  ├──< trade_event（标的事件流 · append-only）
  │        action=SIGNAL/CONFIRMED/ORDERED/FILLED/TP1_FILLED/.../CLOSED
  │        order_id 关联 ↓
  ├──< order（委托记录 · 幂等 UNIQUE）
  │        purpose=OPEN/TP1/TP2/STOP/CLOSE
  │        order_id 关联 ↓
  ├──< fill（成交流水 · append-only 不可变事件日志）
  │
  ├──< position（当前持仓 · fill 的累加汇总）
  └──< account_daily（账户日级快照 · 开盘+收盘+盈亏）
```

### 2.2 完整 DDL

```sql
PRAGMA foreign_keys = ON;  -- 启用 FK 引用完整性（_connect 内）

-- ① account（账号配置）
CREATE TABLE account (
    account_id     TEXT PRIMARY KEY,
    broker         TEXT NOT NULL,           -- "qmt"
    name           TEXT,                    -- "东北模拟盘"
    userdata_path  TEXT,
    session_id     INTEGER,
    strategy_name  TEXT DEFAULT 'quanter',
    mode           TEXT DEFAULT 'dry_run',  -- dry_run / live
    active         INTEGER DEFAULT 1,
    created_at     TEXT NOT NULL
);

-- ② trade_event（标的交易生命周期事件流 · append-only）
CREATE TABLE trade_event (
    event_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id   TEXT NOT NULL REFERENCES account(account_id) ON DELETE RESTRICT,
    trade_id     TEXT NOT NULL,             -- {account_id}_{symbol}_{signal_date}
    symbol       TEXT NOT NULL,
    action       TEXT NOT NULL,             -- SIGNAL/CONFIRMED/VETOED/ORDERED/CANCELLED/EXPIRED/PARTIAL_FILL/FILLED/TP1_FILLED/TP2_FILLED/STOP_TRIGGERED/STOP_FILLED/TIMEOUT_CLOSED/CLOSED
    timestamp    TEXT NOT NULL,
    -- 事件数据（不同 action 填不同字段）
    order_id     TEXT,                      -- 关联 order（ORDERED/FILLED/TP1_FILLED 等操作类）
    qty          REAL,                      -- 本次涉及量
    price        REAL,                      -- 本次成交价/挂单价
    realized_pnl REAL,                      -- 本次已实现盈亏（出场事件）
    meta         TEXT,                      -- JSON（SIGNAL 时存计划参数快照）
    UNIQUE(account_id, trade_id, action)    -- ★ 同 trade 同 action 幂等
);
CREATE INDEX idx_trade_event_trade ON trade_event(trade_id);

-- ③ order（engine 下过的委托 · 幂等 UNIQUE）
CREATE TABLE "order" (
    order_id     TEXT PRIMARY KEY,          -- {date}_{symbol}_{purpose}_{seq}
    trade_id     TEXT NOT NULL,             -- 关联 trade_event.trade_id
    account_id   TEXT NOT NULL REFERENCES account(account_id) ON DELETE RESTRICT,
    trade_date   TEXT NOT NULL,
    symbol       TEXT NOT NULL,
    side         TEXT NOT NULL,             -- buy / sell
    purpose      TEXT NOT NULL,             -- OPEN/TP1/TP2/STOP/CLOSE
    qty          REAL NOT NULL,
    price        REAL NOT NULL,
    broker_oid   TEXT,                      -- QMT 柜台真实单号（async_response 回调回填）
    state        TEXT NOT NULL DEFAULT 'PENDING', -- PENDING/SUBMITTED/PARTIAL/FILLED/CANCELLED/REJECTED
    filled_qty   REAL,
    filled_price REAL,
    submitted_at TEXT,
    filled_at    TEXT,
    UNIQUE(account_id, trade_date, symbol, purpose)  -- ★ 幂等：同日同标的同目的一笔
);
CREATE INDEX idx_order_trade ON "order"(trade_id);

-- ④ fill（成交流水 · append-only 不可变事件日志）
CREATE TABLE fill (
    fill_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id     TEXT NOT NULL REFERENCES "order"(order_id) ON DELETE RESTRICT,
    account_id   TEXT NOT NULL REFERENCES account(account_id) ON DELETE RESTRICT,
    traded_time  TEXT NOT NULL,             -- on_stock_trade 回调的本笔成交时间
    symbol       TEXT NOT NULL,
    direction    TEXT NOT NULL,             -- BUY / SELL
    qty          REAL NOT NULL,
    price        REAL NOT NULL,
    applied_at   TEXT NOT NULL,             -- engine 写入时间
    UNIQUE(order_id, traded_time)           -- ★ 增量幂等（部分成交重推跳过）
);
CREATE INDEX idx_fill_symbol ON fill(symbol);

-- ⑤ position（当前持仓 · fill 的累加汇总，可变）
CREATE TABLE position (
    account_id  TEXT NOT NULL REFERENCES account(account_id) ON DELETE RESTRICT,
    symbol      TEXT NOT NULL,
    qty         REAL NOT NULL,
    avg_price   REAL,                       -- 加权成本（BUY 加权 / SELL 不变）
    entry_date  TEXT,                       -- 首次 BUY 日（max_holding/trailing 用）
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (account_id, symbol)
);

-- ⑥ account_daily（账户日级快照 · pre_open 写 start / post_close 写 close）
CREATE TABLE account_daily (
    account_id          TEXT NOT NULL REFERENCES account(account_id) ON DELETE RESTRICT,
    date                TEXT NOT NULL,
    start_total_asset   REAL,
    start_cash          REAL,
    close_total_asset   REAL,
    close_cash          REAL,
    close_market_value  REAL,
    daily_pnl           REAL,
    daily_pnl_pct       REAL,
    start_snap_at       TEXT,
    close_snap_at       TEXT,
    PRIMARY KEY (account_id, date)
);
```

### 2.3 表间关联关系

| 关联 | 类型 | 说明 |
|---|---|---|
| account → trade_event | 1:N | 一个账户多个标的事件流 |
| account → order | 1:N | 一个账户多笔委托 |
| account → fill | 1:N | 一个账户多笔成交 |
| account → position | 1:N | 一个账户多个持仓 |
| account → account_daily | 1:N | 一个账户多日快照 |
| trade_event → order | N:1（via order_id） | trade 事件关联委托 |
| order → fill | 1:N | 一笔委托可能多笔成交 |

### 2.4 幂等键汇总

| 表 | 幂等键 | 保护的场景 |
|---|---|---|
| trade_event | (account_id, trade_id, action) | 事件重推跳过（engine 重启 / 回调重推） |
| order | (account_id, trade_date, symbol, purpose) | 重复挂单跳过（pre_open 重跑 / 止盈重挂 / 止损重发） |
| fill | (order_id, traded_time) | 成交回报重推跳过（部分成交增量幂等） |
| position | (account_id, symbol) | 同标的一行汇总 |
| account_daily | (account_id, date) | 同日一行快照 |

## 3. 各模块改造方案

### 3.1 新增 `trading/state_store.py`（统一状态库管理）

替代 position_book 的部分职责，提供 6 张表的 CRUD + 幂等写入 + 查询。

```python
# 核心接口设计
def init_store(db_path)
def upsert_account(account_id, broker, ...)
def insert_trade_event(account_id, trade_id, symbol, action, ...) -> bool  # 幂等
def insert_order(order_id, trade_id, account_id, ...) -> bool  # 幂等 UNIQUE
def update_order_state(order_id, state, filled_qty, ...)
def insert_fill(order_id, account_id, traded_time, ...) -> bool  # 幂等 UNIQUE
def apply_fill_to_position(account_id, symbol, direction, qty, price, traded_time)  # 事务
def snapshot_start_equity(account_id, date, total_asset, cash)
def snapshot_close_equity(account_id, date, ...)
# 查询
def get_active_trades(account_id) -> list  # 最新 action 非终态
def get_pending_orders(account_id) -> list  # state 未终态（撤单用）
def has_order(account_id, trade_date, symbol, purpose) -> bool  # 幂等检查
def get_trade_plan(trade_id) -> dict  # 从 SIGNAL 事件 meta 读计划参数
def get_position(account_id, symbol) -> dict
def get_entry_dates(account_id) -> dict
```

### 3.2 改造 `trading/engine.py`（四触发点）

| 触发点 | 当前 | 改造后 |
|---|---|---|
| **eod_plan** | save_plan JSON + push | insert trade_event(SIGNAL) + CONFIRMED → push（JSON 保留给人看/钉钉，DB 是真相源） |
| **pre_open** | cancel gw._orders + 挂单 | ① query_orders 查柜台撤旧单（写 order.state=CANCELLED）② 查 DB has_order 幂等 ③ insert order(OPEN) + trade_event(ORDERED) → submit |
| **_handle_order_update** | _tp_placed 内存 + apply_fill | insert fill + apply_fill_to_position + insert trade_event(FILLED) + insert order(TP1/TP2) 幂等 |
| **stop_loss** | 无状态重发 | 查 DB has_order(STOP) 幂等 → insert order(STOP) + trade_event(STOP_TRIGGERED) |
| **post_close** | reconcile + CSV 兜底 | ① reconcile（position vs broker）② insert trade_event(TP1_FILLED/CLOSED) ③ snapshot_close_equity ④ trailing 演进（读 trade_event SIGNAL meta） |

### 3.3 改造 `trading/trading_plan.py`（plan 双写）

plan JSON **保留**（人看 / 钉钉推送 / veto CLI），但不再是真相源：
- eod_plan：insert trade_event(SIGNAL, meta=计划参数 JSON) → save_plan JSON（双写）
- confirmed 闸：trade_event(CONFIRMED) / trade_event(VETOED)（DB 乐观锁）
- veto：insert trade_event(VETOED)（DB 先写，JSON 同步）
- pre_open 读 confirmed：查 trade_event 最新 action（CONFIRMED→挂，VETOED→跳）

### 3.4 改造 `trading/io/breaker.py`（撤单改查柜台）

```python
async def cancel_all_open_orders(gw, account_id):
    # 旧：for oid in gw._orders → cancel（内存，新连接空）
    # 新：
    orders = await gw.query_orders(cancelable_only=True)  # 查柜台全量
    for o in orders:
        broker_oid = o["order_id"]
        rc = await gw.cancel_order_by_broker_oid(broker_oid)  # 用柜台真实单号撤
        state_store.update_order_state(order_id, "CANCELLED")  # 回写 DB
```

### 3.5 改造 `broker/qmt.py`（新增 cancel_order_by_broker_oid）

当前 cancel_order(order_id) 走 seq→real 映射（内存）。新增直接用柜台单号撤：
```python
async def cancel_order_by_broker_oid(self, broker_oid: int):
    # 直接调 cancel_order_stock(account, broker_oid)，不走 seq→real 映射
```

### 3.6 废弃 `_tp_placed` 内存 + `_orders` 内存（撤单用）

| 当前内存态 | 替代方案 |
|---|---|
| `_tp_placed: set[str]` | order 表 UNIQUE(purpose=TP1) 查 DB |
| `gw._orders` 用于撤单 | query_orders 查柜台 + order 表回写 |
| `gw._orders` 用于 seq→real | 保留（回调链路必需，但不再用于撤单判重） |
| `live_trades.csv` | fill 表替代（不再双轨记账） |

## 4. 关键操作流程（改造后）

### 4.1 pre_open 挂单（幂等）

```
① query_orders(cancelable_only=True) → 查柜台可撤单
② 逐个 cancel_order_by_broker_oid → 回写 order.state=CANCELLED
③ 读 trade_event 最新 action=CONFIRMED 的 trade_id 列表
④ 对每个 trade_id：
    has_order(account_id, today, symbol, "OPEN")?
    → True: 跳过（已挂过，幂等）
    → False: insert order(OPEN) + insert trade_event(ORDERED) → submit_order
```

### 4.2 成交回报 → 挂止盈（幂等）

```
on_stock_trade → _handle_order_update(kind=trade):
① insert_fill(order_id, traded_time, ...) → UNIQUE 幂等（重推跳过）
② apply_fill_to_position（加权 avg + entry_date）
③ insert trade_event(FILLED, filled_qty, filled_price)
④ has_order(account_id, today, symbol, "TP1")?
    → False: insert order(TP1) + insert order(TP2) → submit 两张止盈卖单
    → True: 跳过（已挂过，幂等——替代 _tp_placed）
```

### 4.3 stop_loss 止损（幂等）

```
stop_loss_monitor 每轮：
① 对每个持仓标的，查 trade_event SIGNAL meta 读 stop_price
② 现价 <= stop_price?
③ has_order(account_id, today, symbol, "STOP")?
    → True + state 未终态: 跳过（已有止损单在跑）
    → False: insert order(STOP) + insert trade_event(STOP_TRIGGERED) → submit
```

### 4.4 撤单（查柜台，不查内存）

```
cancel_all_open_orders:
① gw.query_orders(cancelable_only=True) → 柜台全量可撤单
② 逐个 cancel_order_by_broker_oid(broker_oid)
③ state_store.update_order_state(order_id, "CANCELLED")
```

## 5. 数据流（改造后全链路）

```
[19:00 eod_plan]
  scan_live → insert trade_event(SIGNAL, meta=计划参数)
  AUTO_CONFIRM_PLAN=true → insert trade_event(CONFIRMED)
  save_plan JSON（双写，人看/推送用）
  push 钉钉

[09:22 pre_open]
  ① cancel_all_open_orders: query_orders 查柜台 → cancel_by_broker_oid → DB UPDATE
  ② snapshot_start_equity: query_asset → account_daily INSERT
  ③ 遍历 trade_event CONFIRMED:
     has_order(OPEN)? → 跳过（幂等）
     insert order(OPEN) + trade_event(ORDERED) → submit_order → 回填 broker_oid

[成交回调 on_stock_trade]
  insert fill（UNIQUE 幂等）→ apply_fill_to_position → insert trade_event(FILLED)
  has_order(TP1)? → 跳过（幂等，替代 _tp_placed）
  insert order(TP1/TP2) → submit 两张止盈卖单

[盘中 30s stop_loss]
  查 trade_event SIGNAL meta 读 stop_price → 现价跌破?
  has_order(STOP)? → 跳过（幂等）
  insert order(STOP) + trade_event(STOP_TRIGGERED) → submit

[15:30 post_close]
  ① reconcile: position(DB) vs broker(query_positions) → drift 告警
  ② 查 order FILLED → insert trade_event(TP1_FILLED/STOP_FILLED, realized_pnl)
  ③ 全平 → insert trade_event(CLOSED, 总 realized_pnl)
  ④ snapshot_close_equity: query_asset → account_daily UPDATE
  ⑤ trailing 演进: 读 SIGNAL meta 的 atr → compute_stop_price
  ⑥ max_holding 检查: position.entry_date → 超期标记
```

## 6. 显式搁置项

1. **plan JSON 保留双写** — DB 是真相源，JSON 给人看 / veto CLI / 钉钉推送用。不强制只保留 DB（向后兼容 + 人可读）。
2. **`gw._orders` 内存保留** — 回调链路（seq→real 映射 / _handle_order_update 查方向）仍需要。只是撤单不再依赖它（改查柜台）。
3. **job_run 调度幂等表** — order 表 UNIQUE 已防住单笔重挂。job_run 是额外调度层幂等，可后续加。
4. **前端 Cockpit 改造** — get_positions / get_status 读 DB 而非 broker 实时查。后续按需改。
5. **多账户 engine 进程模型** — account 表支持多账户 schema，但 engine 单进程管理单账户的运行模型不变。多账户 = 多 engine 进程。

## 7. 迁移策略

### 7.1 DB 迁移（schema 重建）

```python
def init_store(db_path):
    # 列存在性检测 → DROP+重建（SQLite 改 UNIQUE 必须重建）
    # live 前无生产成交，丢影子数据可接受
    if not _has_table(con, "account"):
        con.execute("CREATE TABLE account (...)")  # 新建
    if _has_table(con, "position") and not _has_column(con, "position", "account_id"):
        con.execute("DROP TABLE position")  # 旧无 account_id → 重建
        con.execute("CREATE TABLE position (...)")
    # fill 表已有 traded_time + UNIQUE(order_id, traded_time)（Task 1 升级过）
    # → 加 account_id 列（ALTER ADD COLUMN，不影响既有数据）
    # trade_event / order / account_daily 全新表 → CREATE IF NOT EXISTS
```

### 7.2 .env QMT_* 配置迁移到 account 表

```python
# engine 启动时从 .env 读 QMT_* → INSERT OR REPLACE INTO account
def _migrate_env_to_account():
    account_store.upsert_account(
        account_id=os.getenv("QMT_ACCOUNT_ID"),
        broker="qmt",
        userdata_path=os.getenv("QMT_USERDATA_PATH"),
        session_id=int(os.getenv("QMT_SESSION_ID", "123458")),
        mode=os.getenv("AUTO_TRADE_MODE", "dry_run"),
    )
```

### 7.3 代码迁移波次

| 波次 | 内容 | 依赖 |
|---|---|---|
| **波1** | state_store.py + 6 张表建表 + account 迁移 | 无 |
| **波2** | eod_plan 改 insert trade_event(SIGNAL/CONFIRMED) + plan 双写 | 波1 |
| **波3** | pre_open 改 DB 幂等 + cancel 查柜台 | 波1+2 |
| **波4** | _handle_order_update 改 fill + order(TP1/TP2) 幂等 | 波1+3 |
| **波5** | stop_loss 改 DB 幂等 | 波1+4 |
| **波6** | post_close 改 trade_event(CLOSED) + account_daily | 波1+4 |

## 8. 验收标准

- [ ] state_store.py 6 张表建表 + CRUD + 幂等写入（INSERT 冲突返 False）
- [ ] account 表从 .env 迁移 QMT_* 配置
- [ ] eod_plan 落 trade_event(SIGNAL + CONFIRMED) + plan JSON 双写
- [ ] pre_open 查柜台撤单（query_orders → cancel_by_broker_oid）+ DB 幂等挂单（has_order）
- [ ] _handle_order_update fill 幂等 + order(TP1/TP2) 幂等（替代 _tp_placed）
- [ ] stop_loss DB 幂等（has_order STOP 未终态跳过）
- [ ] post_close trade_event(CLOSED + realized_pnl) + account_daily 收盘快照
- [ ] _tp_placed 内存废弃（用 order 表替代）
- [ ] cancel_all_open_orders 查柜台（不依赖 gw._orders）
- [ ] live_trades.csv 废弃（fill 表替代）
- [ ] 全测试 pass + 既有 e2e/engine/qmt_gateway 回归零退化
- [ ] 全中文注释（CLAUDE.md）

## 9. 风险与取舍

| 风险 | 取舍 |
|---|---|
| DB 迁移丢影子期数据 | live 前无生产成交，可接受 |
| plan JSON + DB 双写一致性 | DB 是真相源，JSON 只读不写时以 DB 为准 |
| 当前活跃 trade（300654/688036）迁移 | 这些是"计划层"数据，重启后 eod_plan 重扫落 trade_event 即可（不迁移历史） |
| trade_event 查"当前状态"需子查询 | 加 idx_trade_event_trade 索引；或后续加 position-for-trade 物化视图 |
| fill 表加 account_id 列（ALTER） | SQLite ALTER ADD COLUMN 支持，默认 NULL，不破坏既有数据 |
| APScheduler misfire/重叠 | 本 spec 不覆盖（方向 B 调度编排层），但 DB 幂等天然防住重复操作的后果 |
