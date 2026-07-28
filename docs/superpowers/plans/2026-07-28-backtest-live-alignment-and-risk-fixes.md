# 回测对齐 + live P0 风控修复 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) for tracking.

**Goal:** 合并落地两个 spec —— `2026-07-28-backtest-live-alignment-design.md`（回测对齐 9 项）+ `2026-07-28-live-p0-risk-fixes-design.md`（live P0 风控 3 项），共 12 项缺口、14 个 Task，让实盘逼近回测指标 + 满足 live 安全准入。

**Architecture:** 阶段 0 基础设施（position_book schema 升级，是一切的基础）→ 阶段 1 小改高价值 → 阶段 2 信号状态机 → 阶段 3 持仓状态机（依赖阶段 0 entry_date）→ 阶段 4 风控连线（依赖阶段 0 daily_equity）→ 阶段 5 回测侧重跑。

**Tech Stack:** Python 标准库 + pandas，pytest（`asyncio.run`，不加 mark.asyncio），TDD。测试用 `.venv310/Scripts/python.exe`。

**Spec:** `docs/superpowers/specs/2026-07-28-live-readiness-design.md`（统一设计：地基 + 回测对齐 9 项 + 风控连线 3 项；本 plan 据此实现。原 alignment / live-p0 两 spec 已合并删除）

## Global Constraints

- **全中文注释**（CLAUDE.md）：What + Why（交易物理意图/风控红线/回测对齐缺口）。
- **pytest-asyncio strict**：async 测试用 `asyncio.run(...)`，不加 `@pytest.mark.asyncio`。
- **测试用 `.venv310/Scripts/python.exe`**（系统 python 缺 pandas/xtquant）。
- **每 Task 跑相关测试 pass + 既有 e2e 回归零退化**才能标完成。
- **不破坏 scan_at 回测**：detect/stop/tp 改动后 `tests/test_neckline_*` 既有 golden 不破（或预期内更新）。
- **live 模拟盘验证**：关键 Task 后用 trigger_eod_once + pre_open 验证（模拟盘不真亏，观察对齐效果）。
- **position_book schema 迁移**（阶段 0）：列存在性检测 + 重建表（live 前无生产成交，可丢影子数据）。

---

## 阶段 0：基础设施（position_book schema + 持久化）—— 一切的基础

> 必须最先做。max_holding / trailing / 熔断 都依赖这里的 entry_date / atr / daily_equity。

### Task 1: position_book schema 升级 + apply_fill 增量幂等记账

**Spec:** live-p0-risk-fixes §3（P0-1 部分成交精度）

**Files:**
- Modify: `trading/position_book.py`（fill/position/daily_equity 表 + apply_fill 签名 + 迁移）
- Test: `tests/trading/test_position_book.py`（扩展）

**Interfaces:**
- `apply_fill(order_id, symbol, direction, qty, price, traded_time, *, db_path=None) -> bool`（新增 traded_time）
- `position` 表加 `avg_price`/`entry_date`；`fill` 表 UNIQUE 改 `(order_id, traded_time)`；新增 `daily_equity` 表
- 新增 `get_entry_dates() -> dict[str,str]` / `get_start_equity(date)` / `snapshot_start_equity(date, total)`

- [ ] **Step 1: 写失败测试**（spec live-p0 §7.1）
  - `test_apply_fill_partial_increment`：同 order_id 多笔（30+40+30）→ qty=100；重推同 (order_id,traded_time) 幂等跳过
  - `test_apply_fill_avg_price_weighted`：BUY 100@10 + 100@12 → avg=11.0；SELL → avg 不变
  - `test_apply_fill_entry_date_locked`：首次 BUY 写 entry_date；加仓不改；清仓后重新 BUY 写新日期
  - `test_fill_schema_migration`：老 UNIQUE(order_id) → bump 后重建为 UNIQUE(order_id, traded_time)
- [ ] **Step 2: 实现 schema 迁移**（init_db 内 PRAGMA table_info 检测列 + DROP/重建）
- [ ] **Step 3: 实现 apply_fill 增量逻辑**（BUY 加权 avg / entry_date 首次锁定 / SELL avg 不变 / qty=0 删）
- [ ] **Step 4: 跑测试 pass + test_position_book 既有回归零退化**

**依赖:** 无（最先）· **验收:** apply_fill 增量幂等 + avg_price 加权 + entry_date 锁定

---

### Task 2: build_orders 落盘 atr + formed_at + max_wait

**Spec:** 对齐 §4.2.1（P0-2 前置）+ live-p0 §5.3（trailing 前置）

**Files:**
- Modify: `trading/compute/plan.py`（PlannedOrder 加 atr/formed_at/max_wait）+ `trading/engine.py`（eod_plan 序列化）

**Interfaces:**
- `PlannedOrder` 加 `atr: float` / `formed_at: str` / `max_wait: int`
- order_dict 新增 `"atr": round(o.atr,4)` / `"formed_at": str(o.formed_at)` / `"max_wait": o.max_wait`

- [ ] **Step 1: 写测试** `test_build_orders_persist_atr_formed_at_max_wait`（order_dict 含三字段）
- [ ] **Step 2: PlannedOrder 加字段 + build_orders 填充**（atr from signal、formed_at from signal、max_wait from exec_cfg）
- [ ] **Step 3: eod_plan 序列化透出三字段**
- [ ] **Step 4: 跑测试 + test_e2e 回归**

**依赖:** 无 · **验收:** plan.json 含 atr/formed_at/max_wait

---

## 阶段 1：小改高价值（对齐波1）

### Task 3: P0-1 挂单价偏移（buy_limit_atr_mult）

**Spec:** 对齐 §3 · **Files:** `strategies/neckline_method.py:286` · **Test:** `tests/test_neckline_recognition.py`（或 strategy 测试）

- [ ] **Step 1: 写测试** `test_scan_live_entry_has_atr_offset`（neckline=10/atr=0.5/mult=1.0 → entry=10.5, neckline 仍=10）；`test_scan_live_entry_atr_mult_zero`（mult=0 → entry=neckline 零回归）
- [ ] **Step 2: scan_live:286 改** `entry_price = neckline + exec_cfg["buy_limit_atr_mult"] × atr`（round 2）
- [ ] **Step 3: 跑测试 + 验证 Signal.neckline 不变（stop/tp 基准不动）**
- [ ] **Step 4: trigger_eod_once 验证 plan.json order.price=颈线+ATR**

**依赖:** 无 · **验收:** entry 含 ATR 偏移，neckline 独立保留

---

### Task 4: P1-6 stop_atr_mult 默认值对齐

**Spec:** 对齐 §8 · **Files:** `trading/engine.py:76-94`（_trade_cfg）+ `_eod`（传 resolve_active）

- [ ] **Step 1: 写测试** `test_trade_cfg_reads_experiment_stop`（mock 实验 stop_atr_mult=1.0 → _trade_cfg 返 1.0 非 env 2.0）；`test_trade_cfg_fallback_default_1_0`（无实验 → 1.0）
- [ ] **Step 2: _trade_cfg 加 active_experiments 参数**，stop_atr_mult 从主力实验 params 读，env 仅兜底，默认 1.0
- [ ] **Step 3: _eod 调 _trade_cfg(resolve_active())**
- [ ] **Step 4: 跑测试 + .env TRADE_STOP_ATR_MULT 建议改 1.0（或删让默认）**

**依赖:** 无 · **验收:** stop_atr_mult 对齐回测冠军档

---

### Task 5: P0-5 cooldown 信号去重

**Spec:** 对齐 §7 · **Files:** `trading/engine.py`（_eod 内 scan 后过滤）· **Test:** `tests/trading/test_engine_eod_injection.py`（扩展）

- [ ] **Step 1: 写测试** `test_eod_cooldown_dedup`（同标的最近5日 plan 有 formed_at → 新信号丢弃；6日前 → 保留）
- [ ] **Step 2: 实现 `_load_recent_plan_symbols(days)`**（扫 logs/trading_plans/plan_*.json 最近 N 日，返 symbol 集，容错）
- [ ] **Step 3: _eod 内 scan_live 产出 signals 后按 cooldown 过滤**（exec_cfg["cooldown"]）
- [ ] **Step 4: 跑测试 + test_engine_eod_injection 回归**

**依赖:** Task 2（formed_at 落盘）· **验收:** 同标的 cooldown 内不重复出信号

---

## 阶段 2：信号状态机（对齐波2）

### Task 6: P0-2 max_wait 等待窗口

**Spec:** 对齐 §4 · **Files:** `trading/engine.py`（pre_open 超期过滤）+ `trading/compute/stop.py`（抽 `_trading_days_between`）· **Test:** `tests/trading/test_engine.py`

- [ ] **Step 1: 写测试** `test_pre_open_skip_expired_signal`（formed_at=5日前/max_wait=5 → 跳过；3日前 → 挂）；`test_pre_open_formed_at_missing_fallback`（缺 formed_at → days=0 挂）
- [ ] **Step 2: 抽 `_trading_days_between(start, end)`**（复用 calendar.fetch_trade_cal，与 live-p0 compute_holding_days 同款，可合并公共函数）
- [ ] **Step 3: pre_open 遍历 orders 时按 formed_at+max_wait 过滤超期**（continue 跳过 + log）
- [ ] **Step 4: 跑测试 + test_e2e 回归**

**依赖:** Task 2（formed_at/max_wait 落盘）· **验收:** 超期信号不挂，窗口内每日可挂

---

### Task 7: P0-3 分级止盈 tp1/tp2 + tp1_portion

**Spec:** 对齐 §5 · **Files:** `trading/compute/plan.py`（算 tp1）+ `trading/engine.py:1045`（_place_take_profit）· **Test:** `tests/trading/test_engine_order_update_handler.py`（扩展止盈测试）

- [ ] **Step 1: 写测试**
  - `test_build_orders_persist_tp1_portion`（order_dict 含 tp1/tp1_portion）
  - `test_place_take_profit_two_legs`（filled=1000/portion=0.5 → 挂 tp1@500 + tp2@500）
  - `test_place_take_profit_portion_zero/full`（portion=0 只 tp2；=1 只 tp1）
  - `test_tp1_qty_round_to_lot`（filled=430/portion=0.5 → tp1=200, tp2=230）
- [ ] **Step 2: build_orders 算 tp1**（颈线+tp1_h_mult×H）+ PlannedOrder 加 tp1/tp1_portion + eod_plan 序列化
- [ ] **Step 3: _place_take_profit 改挂两张限价卖单**（整手分割：tp1_qty=int(filled×portion/100)*100, tp2_qty=filled-tp1_qty）
- [ ] **Step 4: sanity 守卫**（tp1≥tp2 时只挂 tp2）+ 跑测试 + test_engine_order_update_handler 回归**

**依赖:** 无（独立）· **验收:** 分级止盈挂两张单 + 整手分割

---

## 阶段 3：持仓状态机（依赖阶段 0 entry_date）

### Task 8: P0-4 max_holding 超时平仓

**Spec:** 对齐 §6 · **Files:** `trading/engine.py`（post_close 扫超期 + pre_open 平）· **Test:** `tests/trading/test_engine.py`

- [ ] **Step 1: 写测试** `test_post_close_mark_expired`（entry_date=20日前/max_holding=15 → 标记；10日前 → 不标）；`test_pre_open_close_expired`（读标记 → 挂平仓单@跌停价）
- [ ] **Step 2: post_close 加超期扫描**（position_book.get_entry_dates + compute_holding_days > max_holding → 写 logs/expired_positions.json + notify WARN）
- [ ] **Step 3: pre_open 撤昨日单后、挂新单前，平超期持仓**（读标记 + gw 持仓 qty + get_quote.low_limit → _submit 卖）
- [ ] **Step 4: 跑测试 + 熔断优先（若 post_close 触发熔断则跳过 max_holding 标记）**

**依赖:** Task 1（entry_date）+ live-p0 熔断（Task 10，熔断优先约束）· **验收:** 超期持仓次日平

---

### Task 9: live-p0 P0-3 trailing 盘后演进

**Spec:** live-p0 §5 · **Files:** `trading/engine.py`（_evolve_trailing_stops + post_close 串联）+ `trading/compute/stop.py`（compute_holding_days 公共函数）· **Test:** `tests/trading/test_stop_loss.py` + 新 test

- [ ] **Step 1: 写测试**（spec live-p0 §7.3）`test_compute_holding_days_trading_calendar`（entry→today 交易日计数；缺 entry → 0）；`test_evolve_trailing_stops_writeback`（mock plan+entry_date → 写回 stop_price round2；holding_days=0 时 stop=base_stop 零回归）；`test_post_close_trailing_skipped_after_breaker`（熔断后跳过）
- [ ] **Step 2: compute_stop_price 接入**（已实现，补 holding_days 维度测试）
- [ ] **Step 3: _evolve_trailing_stops**（遍历 plan orders + entry_date → compute_stop_price → 写回 plan.stop_price）
- [ ] **Step 4: post_close 串联**（对账+兜底+熔断之后，未熔断时跑 trailing）+ _trade_cfg 加 trailing_grace/step/floor env**

**依赖:** Task 1（entry_date）+ Task 2（atr）+ Task 10（熔断优先）· **验收:** 盘后演进次日 stop + holding_days=0 零回归

---

## 阶段 4：风控连线（live-p0-risk-fixes）

### Task 10: live-p0 P0-2 日内熔断（daily -3%）

**Spec:** live-p0 §4 · **Files:** `trading/engine.py`（_pre_open 快照 + post_close 三步）· **Test:** `tests/trading/test_engine.py` + `tests/trading/test_circuit_breaker.py`

- [ ] **Step 1: 写测试**（spec live-p0 §7.2）`test_snapshot_start_equity`（pre_open 快照；query_asset 返空不写；重入幂等）；`test_post_close_circuit_breaker_triggers`（start=100w/curr=96w → cancel_all+halt+告警）；`test_circuit_breaker_skip`（-2% 不触发；缺基线跳过+WARN）；`test_circuit_breaker_isolated_from_reconcile`
- [ ] **Step 2: pre_open 快照**（确认闸后 query_asset → snapshot_start_equity 写 daily_equity）
- [ ] **Step 3: post_close 串联三步**（reconcile+兜底之后，check_daily_loss_limit → cancel_all + emergency_halt + 告警；缺基线跳过+WARN）
- [ ] **Step 4: 跑测试 + test_circuit_breaker 回归**

**依赖:** Task 1（daily_equity 表）· **验收:** 日内 -3% 熔断三步 + 缺基线安全跳过

---

### Task 11: live-p0 P0-1 盘后 query_trades 兜底纠正

**Spec:** live-p0 §3.3 · **Files:** `trading/engine.py`（post_close 兜底）· **Test:** `tests/trading/test_engine.py`

- [ ] **Step 1: 写测试** `test_post_close_query_trades_reconcile`（mock query_trades 返[100@10]，position 记 30 → 纠正为 100 + 告警）
- [ ] **Step 2: post_close reconcile 之后加成交流水交叉校验**（query_trades 聚合 vs position_book，不一致以 query_trades 为准重写 + notify）
- [ ] **Step 3: 跑测试 + 验证与 reconcile 分工（reconcile 查持仓 drift，本步查成交流水漏笔）**

**依赖:** Task 1（apply_fill 增量）· **验收:** 盘后账本纠正 + 告警

---

## 阶段 5：回测侧重跑（对齐波4）

### Task 12: P1-9 手续费/印花税（simulate_exit CostModel）

**Spec:** 对齐 §10 · **Files:** `strategies/neckline/backtest.py`（CostModel + simulate_exit 扣费）· **Test:** `tests/test_neckline_core.py`（或回测测试）

- [ ] **Step 1: 写测试** `test_simulate_exit_with_cost`（entry/exit 扣佣+印+过 → pnl 比零费率低；分级两笔双倍佣金）
- [ ] **Step 2: 实现 `_cost(side, qty, price)`**（万三佣 min5 + 卖0.05%印 + 0.001%过户）
- [ ] **Step 3: simulate_exit entry/exit pnl 扣费**（lot1/lot2 各扣 exit_cost）
- [ ] **Step 4: 跑测试 + 既有回测 golden 更新（费率使 pnl 降，预期内）**

**依赖:** 无（改回测）· **验收:** simulate_exit 扣费

---

### Task 13: P1-11 标的池对齐（param_iter 创板科创）

**Spec:** 对齐 §11 · **Files:** `strategies/neckline/backtest.py`（universe 过滤）· **Test:** 回测 universe 测试

- [ ] **Step 1: 写测试** `test_backtest_universe_chuangke_kechuang`（universe 只含 300/301/688/689）
- [ ] **Step 2: scan_symbol/run_backtest universe 加创板科创前缀过滤**（对齐 _load_universe）
- [ ] **Step 3: 跑测试**

**依赖:** 无 · **验收:** 回测 universe 对齐实盘

---

### Task 14: P1-7 止损巡检缩短 + param_iter 重跑

**Spec:** 对齐 §9 · **Files:** `.env`（ENGINE_STOPLOSS_INTERVAL_SECONDS=15）· **Test:** probe_qmt_ratelimit 确认 15s 不撞限频

- [ ] **Step 1: probe_qmt_ratelimit 验证 15s 巡检 + get_quote 不撞限频**
- [ ] **Step 2: .env ENGINE_STOPLOSS_INTERVAL_SECONDS=15**
- [ ] **Step 3: 重跑 param_iter**（Task 12 手续费 + Task 13 创板科创 后，冠军档可能变）
- [ ] **Step 4: publish 新冠军档到 experiment（若变化）**

**依赖:** Task 12 + Task 13（重跑要用新回测）· **验收:** 15s 巡检 + 新冠军档

---

## 依赖图

```
Task 1 (position_book schema) ─┬─→ Task 8 (max_holding, entry_date)
                               ├─→ Task 9 (trailing, entry_date+atr)
                               ├─→ Task 10 (熔断, daily_equity)
                               └─→ Task 11 (兜底, apply_fill 增量)

Task 2 (atr/formed_at/max_wait 落盘) ─┬─→ Task 5 (cooldown, formed_at)
                                       └─→ Task 6 (max_wait, formed_at)

Task 3 (挂单价)     独立
Task 4 (stop默认值) 独立
Task 7 (分级止盈)   独立
Task 10 (熔断) ──────→ Task 8/9（熔断优先约束）

Task 12 (手续费) ─┐
Task 13 (标的池) ─┴→ Task 14 (重跑 param_iter)
```

## 实现顺序 + commit 节奏

| 阶段 | Task | 累计 | commit |
|------|------|------|--------|
| 0 基础设施 | T1, T2 | 2 | feat(position_book): schema升级+增量记账+atr/formed_at持久化 |
| 1 小改 | T3, T4, T5 | 5 | feat(trading): 挂单价偏移+stop默认值对齐+cooldown去重 |
| 2 信号状态机 | T6, T7 | 7 | feat(trading): max_wait窗口+分级止盈tp1/tp2 |
| 3 持仓状态机 | T8, T9 | 9 | feat(trading): max_holding超时平仓+trailing盘后演进 |
| 4 风控连线 | T10, T11 | 11 | feat(trading): 日内熔断-3%+盘后query_trades兜底 |
| 5 回测侧 | T12, T13, T14 | 14 | feat(backtest): 手续费+创板科创池+重跑param_iter |

**每 Task 跑测试 pass + e2e 回归 + 模拟盘验证（关键 Task）+ commit。**

## 验收（Definition of Done）

- [ ] 阶段 0-5 全部 Task 完成，§13/§7 测试全 pass
- [ ] 既有 test_e2e_trading_flow / test_engine / test_qmt_gateway / test_neckline_* 回归零退化
- [ ] plan.json 含 neckline/entry(颈线+ATR)/formed_at/max_wait/tp1/tp1_portion/atr/stop/take_profit 全字段
- [ ] pre_open 过滤超期信号；_place_take_profit 挂 tp1+tp2 两张单；post_close 扫 max_holding+熔断+trailing
- [ ] 模拟盘 trigger_eod_once + pre_open 验证对齐（plan 字段 + 挂单口径）
- [ ] 回测 simulate_exit 扣费 + 创板科创池 + 重跑 param_iter 新冠军档
- [ ] 所有新增/修改代码配备像素级中文注释（CLAUDE.md 红线）
- [ ] live 前必修 4 项（部分成交/熔断/trailing/EMT）中 3 项闭环（EMT 另立项）

## 风险与取舍（同对齐 spec §17 + live-p0 spec §10）

| 风险 | 取舍 |
|------|------|
| 阶段 0 schema 重建丢影子期 dry_run 数据 | live 前无生产成交，可接受 |
| Task 14 重跑 param_iter 冠军档变化 | 预期变化（费率/池改），用新冠军档 |
| Task 8 max_holding 平仓价（跌停价） | 超时释放资金，不等好价位（接受滑点） |
| Task 9 trailing 依赖 Task 1+2+10 | 严格按依赖序，阶段 3 在阶段 0/4 后 |
| 模拟盘验证不等于实盘 | 模拟盘验对齐口径，切实盘前再跑 TRADE_SHADOW_MIN_DAYS 影子期 |
