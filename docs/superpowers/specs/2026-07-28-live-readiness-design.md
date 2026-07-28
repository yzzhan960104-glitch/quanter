# 2026-07-28 live 准入统一设计：回测对齐 + P0 风控

> **范围**：合并原 `backtest-live-alignment`（回测对齐 9 项）+ `live-p0-risk-fixes`（P0 风控 3 项 + 基础设施）= **12 项缺口，统一 live 准入设计**。原两个 spec 已删除，本 spec 是唯一设计文档。
> **plan**：`docs/superpowers/plans/2026-07-28-backtest-live-alignment-and-risk-fixes.md`（14 Task，据此实现）
> **物理定位**：`trading/position_book.py`（schema 升级·地基）+ `trading/engine.py`（四触发点编排）+ `strategies/neckline_method.py`（scan_live）+ `trading/compute/plan.py`（build_orders）+ `strategies/neckline/backtest.py`（回测侧重跑）。
> **设计哲学**：Karpathy 极简（复用 `NecklineConfig` 18 维参数 + `simulate_exit` 状态机语义 + `check_daily_loss_limit`/`compute_stop_price` 既有 functional core，只补连线与口径对齐，不引新框架）。

---

## 1. 背景与目标

颈线法 live 准入有两类缺口叠加：
- **回测对齐**（9 项）：`NecklineConfig` 执行层 7 维参数，实盘只接了 `stop_atr_mult` + `tp_h_mult`(=tp2)，**5 维失效**（`buy_limit_atr_mult`/`max_wait`/`cancel_thresh_mult`/`tp1_h_mult`/`tp1_portion`/`max_holding`/`cooldown`）+ stop 默认值不一 + 手续费未建模 + 标的池不同。回测冠军档套实盘行为显著背离。
- **P0 风控**（3 项）：部分成交精度（position_book 整笔去重）/ 日内熔断（post_close 未串联）/ trailing（env 读未消费）。live 安全准入红线。

**两者强耦合**：共享 `position_book` schema 升级（`entry_date`/`atr`/`daily_equity`/`fill.traded_time`）这条"地基"——max_holding/trailing 依赖 entry_date，熔断依赖 daily_equity，分级止盈依赖部分成交精度。故合并一个 spec 统一设计地基 + 上层。

| 项 | 类别 | 已有基础（不重写） | 本 spec 补的缺口 |
|----|------|------------------|-----------------|
| **地基** | 地基 | position_book SQLite WAL | schema 升级（fill traded_time + position avg_price/entry_date + daily_equity）+ apply_fill 增量幂等 |
| P0-1 挂单价偏移 | 对齐 | buy_limit_atr_mult、scan_live entry_price | scan_live entry=颈线+buy_limit_atr_mult×ATR |
| P0-2 max_wait | 对齐 | max_wait、plan formed_at | 信号 N 日窗口 + pre_open 超期过滤 |
| P0-3 分级止盈 | 对齐 | tp1_h_mult/tp1_portion、_place_take_profit | plan 落 tp1 + 挂两张止盈单 |
| P0-4 max_holding | 对齐 | max_holding、entry_date（地基） | post_close 超期扫描 + pre_open 平 |
| P0-5 cooldown | 对齐 | cooldown、plan formed_at | _eod 跨日去重 |
| P1-6 stop 默认值 | 对齐 | stop_atr_mult=1.0 | _trade_cfg 从实验参数读 |
| P1-7 止损语义 | 对齐 | should_trigger_stop | 接受差异 + 巡检 30s→15s |
| P1-9 手续费 | 对齐（回测） | simulate_exit（零费率） | CostModel + 重跑 |
| P1-11 标的池 | 对齐（回测） | _load_universe（创板科创） | param_iter 改创板科创 + 重跑 |
| R-1 部分成交精度 | 风控 | apply_fill、query_trades | 增量记账（地基）+ 盘后兜底纠正 |
| R-2 日内熔断 | 风控 | check_daily_loss_limit、cancel_all、emergency_halt | daily_equity（地基）+ post_close 三步串联 |
| R-3 trailing | 风控 | compute_stop_price（grace/step/floor） | atr/entry_date（地基）+ 盘后演进 |

---

## 2. 设计决策（已拍板）

| 决策点 | 选定方案 | 否决项 |
|--------|---------|--------|
| 地基 schema 迁移 | 列存在性检测 + 重建表（live 前无生产数据可丢） | ALTER 增量 / schema_version（YAGNI） |
| 地基 apply_fill 增量 | (order_id, traded_time) 幂等 + 加权 avg_price + entry_date 首次锁定 | 纯盘后重算（盘中 drift 盲区） |
| P0-1 entry 与颈线 | entry=挂单价(颈线+ATR)、neckline=颈线（stop/tp 基准不变） | 改 stop/tp 基准（破坏形态语义） |
| P0-2 max_wait | plan 持久化 formed_at + pre_open 超期过滤 | scan_live 放宽 cooldown（破坏去重） |
| P0-3 分级止盈 | 挂两张限价卖单（柜台各自撮合） | 状态机逐笔（回报延迟） |
| P0-4 max_holding | post_close 扫超期 + pre_open 平（跌停价确保成交） | 独立超时 cron |
| P0-5 cooldown | _eod 查 plan formed_at 跨日去重 | 独立 last_signal.json |
| P1-6 stop 参数源 | _trade_cfg 从 experiment.params 读 | env 硬编码 2.0 |
| P1-7 止损粒度 | 接受 last_price 差异 + 巡检 15s | 抓日内 low（本质限制） |
| P1-9 手续费 | simulate_exit 加 CostModel + 重跑 | 只改实盘 |
| P1-11 标的池 | param_iter universe 改创板科创 + 重跑 | 实盘扩全市场 |
| R-1 部分成交 | 双保险：盘中增量幂等 + 盘后 query_trades 兜底 | 纯盘后重算 |
| R-2 熔断 | daily -3%（pre_open 快照 + post_close 三步） | 账号级故障熔断（搁置） |
| R-3 trailing | 盘后演进（post_close 重算次日 stop，盘中不调） | 盘中 ATR high 跟踪（突破 spec） |

---

## 3. 基础设施：position_book schema 升级（一切基础 · 阶段0 · plan Task 1）

**必须最先实现**。max_holding/trailing/熔断/分级止盈全依赖此。

### 3.1 schema 变更

```sql
-- fill 表：UNIQUE(order_id) → UNIQUE(order_id, traded_time)
CREATE TABLE fill (
    fill_id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT NOT NULL, traded_time TEXT NOT NULL,   -- on_stock_trade 本笔成交时间（幂等键一半）
    symbol TEXT NOT NULL, direction TEXT NOT NULL,
    qty REAL NOT NULL, price REAL NOT NULL, applied_at TEXT NOT NULL,
    UNIQUE(order_id, traded_time)
);
-- position 表：加 avg_price + entry_date
CREATE TABLE position (
    symbol TEXT PRIMARY KEY, qty REAL NOT NULL,
    avg_price REAL,              -- 加权成本（浮盈/成本对账）
    entry_date TEXT,             -- 首次 BUY 日（max_holding/trailing 的 holding_days 用）
    updated_at TEXT NOT NULL
);
-- daily_equity 表（新）：熔断 start_equity 基线
CREATE TABLE daily_equity (
    date TEXT PRIMARY KEY, start_total_asset REAL NOT NULL, snap_at TEXT NOT NULL
);
```

**迁移**：init_db 内 `PRAGMA table_info` 检测列存在性，不存在则 DROP+重建（live 前无生产成交，可丢影子数据）。不写 ALTER（SQLite 改 UNIQUE 必须重建表）/ 不引 schema_version（YAGNI）。

### 3.2 apply_fill 增量幂等记账

签名加 `traded_time`：
```python
def apply_fill(order_id, symbol, direction, qty, price, traded_time, *, db_path=None) -> bool:
```
核心（单事务原子）：
1. `INSERT fill(order_id, traded_time, ...)` —— 同 (order_id, traded_time) 重推 IntegrityError → 返 False（幂等）
2. **BUY**: new_qty=old+qty; new_avg=(old_qty·old_avg+qty·price)/new_qty（加权）; entry_date 首次 BUY 锁定（加仓不改）
3. **SELL**: new_qty=old-qty; avg_price 不变（A 股口径）; entry_date 不变
4. qty==0 → DELETE

新增 reader：`get_entry_dates()` / `get_start_equity(date)` / `snapshot_start_equity(date, total)`。

**关键事实**：`_handle_order_update` 只消费 `kind=="trade"`（on_stock_trade），其 `traded_volume` 是**本笔增量**（非累计）—— 已由 record_live_trade 当本笔量用确认。故 apply_fill 每次 `+= delta` 语义正确。

---

## 4. 回测对齐（9 项）

### 4.1 P0-1 挂单价偏移（buy_limit_atr_mult）— plan Task 3
- **缺口**：`scan_live:286` `entry_price=res["entry"]`=颈线价，无 ATR 偏移。回测=颈线+1×ATR（`backtest:75`）。
- **方案**：`scan_live` `entry = 颈线 + exec_cfg["buy_limit_atr_mult"] × atr`（round 2）。`Signal.neckline` 仍=颈线（stop/tp 基准不变）。
- **边界**：mult=0 退化颈线（零回归）；atr 缺失回退颈线。

### 4.2 P0-2 max_wait 等待窗口 — plan Task 6（依赖 Task 2 落盘）
- **缺口**：回测信号后 5 日窗口等回踩，实盘只挂 1 天（次日 pre_open 撤昨日）。
- **方案**：plan 落 formed_at+max_wait（Task 2）；pre_open 按 `_trading_days_between(formed_at, today) > max_wait` 过滤超期。
- **边界**：formed_at 缺失→days=0 挂（不误杀）；max_wait=0 退化只挂 1 天。

### 4.3 P0-3 分级止盈 tp1/tp2 — plan Task 7
- **缺口**：`_place_take_profit:1047` 单笔全平 tp2，无 tp1 锁利。
- **方案**：build_orders 算 tp1（颈线+tp1_h_mult×H）；_place_take_profit 挂两张单：`tp1_qty=int(filled×tp1_portion/100)*100` + `tp2_qty=余量`。
- **边界**：tp1_qty=0（portion×filled<100）→ 全量 tp2；tp1≥tp2 sanity 只挂 tp2。

### 4.4 P0-4 max_holding 超时平仓 — plan Task 8（依赖地基 entry_date）
- **缺口**：实盘无超时平仓，持仓无限期挂账。
- **方案**：post_close 扫 `compute_holding_days(entry_date, today) > max_holding` → 标记 `logs/expired_positions.json`；pre_open 平（跌停价确保成交）。
- **边界**：熔断优先（post_close 触发熔断则跳过 max_holding 标记）。

### 4.5 P0-5 cooldown 信号去重 — plan Task 5（依赖 Task 2 formed_at）
- **缺口**：scan_live 无去重，同标的连续多产信号超额成交。
- **方案**：_eod scan 后查最近 cooldown 日 plan 的 formed_at，同标的丢弃。
- **边界**：plan 损坏容错（跳过，保守不误杀）；cooldown=0 不去重。

### 4.6 P1-6 stop_atr_mult 默认值 — plan Task 4
- **缺口**：回测 DEFAULTS=1.0（`method_v0:49`），实盘 env=2.0（`engine:85`）。
- **方案**：`_trade_cfg` 从 `experiment.params.stop_atr_mult` 读（主力实验），env 仅兜底，默认 1.0。
- **边界**：env 仍可强制覆盖；无实验→1.0。

### 4.7 P1-7 止损语义 — plan Task 14
- **缺口**：回测 K线 low，实盘 30s 巡检 last_price。
- **方案**：接受 last_price 巡检差异（本质限制），巡检 30s→15s（`ENGINE_STOPLOSS_INTERVAL_SECONDS=15`）。
- **边界**：15s 仍漏瞬时穿越（接受）；probe_qmt_ratelimit 验 15s 不撞限频。

### 4.8 P1-9 手续费（改回测）— plan Task 12
- **缺口**：simulate_exit 零费率，胜率/年化虚高。
- **方案**：加 `_cost(side, qty, price)`（万三佣 min5 + 卖0.05%印 + 0.001%过）；entry/exit pnl 扣费；分级两笔双倍佣。
- **边界**：重跑 param_iter（冠军档可能变）。

### 4.9 P1-11 标的池（改回测）— plan Task 13
- **缺口**：param_iter 全市场 top，实盘创板科创。
- **方案**：param_iter universe 加创板科创前缀过滤（300/301/688/689，对齐 `_load_universe`）。
- **边界**：重跑 param_iter。

---

## 5. 风控连线（3 项）

### 5.1 R-1 部分成交精度（双保险）— plan Task 1（盘中）+ Task 11（盘后）
- **缺口**：position_book 整笔去重只记首笔量，部分成交 drift。
- **盘中**（§3.2 已含）：apply_fill 增量幂等 + 加权 avg_price + entry_date 锁定。
- **盘后兜底**（Task 11）：post_close reconcile 后，`query_trades` 聚合 vs position_book，不一致以 query_trades 为准重写 + 告警。
- **分工**：reconcile 查持仓 drift；query_trades 兜底查成交流水漏笔。

### 5.2 R-2 日内熔断（daily -3%）— plan Task 10（依赖地基 daily_equity）
- **缺口**：post_close 不调 check_daily_loss_limit（无 equity 源 + 三步未串）。
- **方案**：
  - **pre_open 快照**：确认闸后 `query_asset` → `snapshot_start_equity` 写 daily_equity（地基 §3.1）。
  - **post_close 串联**：reconcile+兜底之后，`check_daily_loss_limit(start, curr)` → `cancel_all` + `emergency_halt` + 告警。缺基线跳过+WARN（不拿 0 误触发）。
- **边界**：start/curr 缺失跳过+告警；熔断后跳过 trailing/max_holding（已 lock_down，次日人工）。

### 5.3 R-3 trailing 盘后演进 — plan Task 9（依赖地基 entry_date + Task 2 atr + Task 10 熔断优先）
- **缺口**：`compute_stop_price` 已实现但实盘零调用（env 读 grace/step/floor 未消费）。
- **方案**：
  - build_orders 落盘 atr（Task 2）。
  - post_close（熔断之后）：`_evolve_trailing_stops` 遍历 plan orders + entry_date → `compute_stop_price(neckline, atr, holding_days, ...)` → 写回 plan.stop_price（round 2）。
  - 次日 stop_loss 读演进后的 stop（盘中不调，符合 spec「盘中不调整」）。
- **holding_days**：`compute_holding_days(entry_date, today)` 交易日口径（复用 `calendar.fetch_trade_cal`，与 P0-4 max_holding 共用）。
- **边界**：holding_days=0 → base_stop（零回归）；atr 缺失跳过；熔断后跳过。

---

## 6. 数据流总览（对齐 + 风控合并后）

```
┌─ eod_plan(19:00,T-1) ──────────────────────────────────────┐
│  scan_live: entry=颈线+ATR(P0-1) → cooldown 去重(P0-5)      │
│  build_orders: tp1+tp1_portion(P0-3) + atr/formed_at/max_wait│
│  落盘: order.price/neckline/tp1/tp1_portion/stop/tp2/atr/   │
│        formed_at/max_wait                                   │
└──────────────────────────────────────────────────────────┘
┌─ pre_open(09:22) ──────────────────────────────────────────┐
│  ① 确认闸  ② snapshot_start_equity(R-2)  ③ 撤昨日单         │
│  ④ 平 max_holding 超期(P0-4)  ⑤ 过滤 max_wait 超期信号(P0-2)│
│  ⑥ 逐单挂                                                   │
└──────────────────────────────────────────────────────────┘
┌─ stop_loss(15s,盘中) ─ P1-7 ───────────────────────────────┐
│  load_plan.stop_price → last_price 跌破 → 卖（15s）         │
└──────────────────────────────────────────────────────────┘
┌─ _handle_order_update(trade) ─ P0-3 + R-1 ─────────────────┐
│  买单成交 → _place_take_profit 挂 tp1+tp2 两单(P0-3)        │
│  apply_fill 增量+avg+entry_date(R-1 地基)                   │
└──────────────────────────────────────────────────────────┘
┌─ post_close(15:30) ─ R-1/R-2/R-3 + P0-4 ───────────────────┐
│  ① reconcile  ② query_trades 兜底纠正(R-1)                  │
│  ③ 熔断 -3%(R-2)  ④ trailing 演进(R-3，未熔断时)            │
│  ⑤ max_holding 超期扫描→标记(P0-4，未熔断时)                │
└──────────────────────────────────────────────────────────┘
```

---

## 7. 测试策略

测试环境 `.venv310/Scripts/python.exe`，pytest asyncio strict。**逐项 TDD 步骤见 plan Task 1-14 的 Step 1**（每 Task 先写失败测试）。关键测试：

- **地基**：test_apply_fill_partial_increment / avg_price_weighted / entry_date_locked / schema_migration
- **对齐**：test_scan_live_entry_atr_offset / pre_open_skip_expired / place_take_profit_two_legs / post_close_mark_expired / eod_cooldown_dedup / trade_cfg_reads_experiment
- **风控**：test_post_close_circuit_breaker_triggers / evolve_trailing_stops_writeback / post_close_query_trades_reconcile
- **回归**：test_e2e_trading_flow / test_engine / test_qmt_gateway / test_neckline_* 零退化

---

## 8. 显式搁置（不在本 spec）

1. **EMT 行情源**（stop_loss 现价依赖 xtdata，EMT 网关无）— 另立项。
2. **cancel_thresh_mult execute 层**（挂单后涨幅兑现撤单）— scan_live 识别期预判已替代大部分；execute 层剩余需 max_wait 窗口内挂单监控，follow-up。
3. **账号级故障熔断**（连续 query 失败/订单 FAILED 探测）— R-2 仅做 daily -3% 结果熔断，账号级原因熔断 follow-up。
4. **`_tp_placed` 持久化**（进程重启丢失）— 与 R-1 fill 表 traded_time 幂等耦合，随 R-1 加固。
5. **盘中 ATR high 跟踪 trailing** — R-3 选盘后演进（spec 红线：盘中不调整 stop）。

---

## 9. 验收（Definition of Done）

- [ ] 地基：position_book fill 表 UNIQUE(order_id, traded_time) + position 含 avg_price/entry_date + daily_equity；apply_fill 增量+加权+entry_date 锁定
- [ ] P0-1 scan_live entry=颈线+ATR；P0-2 pre_open 过滤超期；P0-3 挂 tp1+tp2 两单；P0-4 post_close 扫超期+pre_open 平；P0-5 _eod cooldown 去重
- [ ] P1-6 _trade_cfg 从实验读；P1-7 巡检 15s；P1-9 simulate_exit 扣费；P1-11 param_iter 创板科创
- [ ] R-1 盘后 query_trades 兜底；R-2 熔断三步；R-3 trailing 盘后演进
- [ ] plan.json 全字段（neckline/entry/formed_at/max_wait/tp1/tp1_portion/atr/stop/take_profit）
- [ ] 全测试 pass + 既有回归零退化
- [ ] 模拟盘 trigger_eod_once + pre_open 验证对齐
- [ ] 回测重跑 param_iter 新冠军档
- [ ] 全中文注释（CLAUDE.md）；live 必修 4 项中 3 项闭环（EMT 另立项）

---

## 10. 风险与取舍

| 风险 | 取舍 |
|------|------|
| 地基 schema 重建丢影子期数据 | live 前无生产成交，可接受 |
| P0-2 max_wait 窗口内每日重挂累积 | pre_open 撤昨日单（既有），每日只挂当日有效单 |
| P0-3 分级整手分割（tp1_qty 非精确） | 向下取整 100，tp2_qty=余量（含零股），柜台接受 |
| P0-4 平仓价（跌停价） | 超时释放资金，不等好价位 |
| R-2 熔断基线缺失 | 跳过+WARN，不拿 0 误触发 |
| R-3 trailing 依赖地基+熔断 | 严格阶段序（Task 9 在 Task 1+2+10 后） |
| P1-9/P1-11 重跑冠军档变化 | 预期变化，用新冠军档 |
| 模拟盘 ≠ 实盘 | 模拟盘验对齐口径，切实盘前再跑 TRADE_SHADOW_MIN_DAYS 影子期 |
