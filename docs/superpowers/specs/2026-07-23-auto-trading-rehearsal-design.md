# 模拟盘端到端演练设计（方向1：交易全流程打通）

| 项 | 值 |
|---|---|
| 日期 | 2026-07-23 |
| 状态 | 设计已确认，待 writing-plans 拆任务 |
| 前置 spec | `2026-07-21-auto-trading-engine-design.md`（引擎四触发点骨架） |
| 关联 | `2026-07-22-miniqmt-access-gap-design.md`（QMT 接入缺口） |
| 方向2（独立） | 颈线 param_iter 全维度持久化——**本 spec 不涉及**，另起周期 |

---

## 1. 背景与目标

研究员提出两个 TODO 方向。经 brainstorming 拆解，**两方向独立、无强依赖**，各自走独立 spec→plan→实施周期。本 spec 聚焦**方向1**：在 miniQMT 模拟盘（东北证券 NET 10110356 @100万）端到端跑通完整交易流程，为将来切 live 做准入验证。

方向1 三个子诉求（研究员原话）：

- **1<1> 数据机器人每日检查数据实时性**：17点前至少有 T-1 完整数据，17点后要有 T 完整数据。
- **1<2> 交易机器人19点算T+1交易计划 + 当前持仓 + 盈亏比例等核心数据**。
- **1<3> 开盘 9:30-15:00 按前日计划挂买入单，买入成交后挂止盈单，30s一次监控持仓挂止损单，每笔成交钉钉通知 + 落交易日志**。

**成功标准**：模拟盘连续多日端到端自动跑通（数据检查→计划→人审→挂单→止盈止损→通知日志→对账），零漏单零重单，盈亏可追溯。策略保真度对齐回测（无执行偏差）。

---

## 2. 现状地基核实（去幻觉，2026-07-23 实读源码）

brainstorming 前先核实，纠正了多处与记忆/旧 spec 不符的认知：

### 2.1 已落地可用（直接复用）

| 设施 | 位置 | 说明 |
|---|---|---|
| 引擎四触发点 APScheduler | `trading/engine.py` TradingEngine | eod_plan/pre_open/stop_loss/post_close 四 cron 已装配 |
| 下单/撤单/批量撤单 | `broker/qmt.py` submit_order/cancel_order + `trading/io/breaker.py` | order_stock_async + seq↔real_order_id 映射 |
| 持仓/资产/委托/成交查询 | `broker/qmt.py` _fetch_broker_positions/query_asset/query_orders/query_trades | 持仓含 avg_price（可算浮盈），asset 含 total_asset（熔断 equity 源） |
| 风控 10 关 | `trading/compute/risk.py check_order` | 资金/涨跌停/白名单/熔断 lock_down/session/confirm |
| 计划落盘+人审确认 | `trading/trading_plan.py` save_plan/load_plan/confirm_plan/push_plan_to_dingtalk | JSON `logs/trading_plans/plan_<date>.json`，confirmed 闸 |
| 钉钉出站 | `broadcast/push.py` + dws send-by-bot | 零自写加签 |
| Tushare 增量采集 | `scripts/sync_incremental.py` @schtasks 18:00 | 每日自动拉 T 日数据落湖 |
| 断线重连 | `broker/qmt.py` _reconnect | 指数退避 5 次上限（避免刷爆登录限频） |
| 回报回调注入点 | `broker/qmt.py:301` `_on_order_update` | **代码注释原文：「上层注入的异步回报回调（钉钉报警 / State 持久化）」——挂载点早已预留，当前无人注册** |
| post_close 熔断三件套 | `trading/compute/breaker.py` + `trading/io/breaker.py` + `trading_service.emergency_halt` | 纯函数+I/O壳+API均已实现且单测覆盖，**仅 post_close 未串联** |

### 2.2 关键缺口（本 spec 要补）

| # | 缺口 | 现状证据 |
|---|---|---|
| G1 | **数据实时性主动检查**（对比交易日历判 T-1/T） | data bot @17:00 只看 parquet mtime 新鲜度（`data_service._derive_status`），不看数据内容最新日期 |
| G2 | **eod_plan 时序 bug**：@15:35 跑，但 T 日数据 @18:00 才落湖 → 用的是 T-1 数据 | `engine.py:524` cron `35 15`，增量采集 @18:00 |
| G3 | **stop_loss_monitor 恒空转** | `engine.py:649` 注释自承 `stop_prices=None` → 永远返「无止损价配置」no-op |
| G4 | **止盈完全未落地**（实盘） | stop_loss_monitor 只判跌破止损；pre_open 只挂买单，止盈价只存 plan JSON 不挂卖单 |
| G5 | **成交→钉钉+日志链路断** | `_on_order_update` 无人注册；`record_live_trade` 只在 submit_order 内调一笔，on_stock_trade 成交回报不补写 |
| G6 | **持仓盈亏未算** | `trading_service.get_positions` 的 pnl/market_value = None |
| G7 | **移动止损未接入** | `compute_stop_price`(grace/step/floor) 纯函数在，`engine.py:88` 注释自承「本 task 未实际消费」 |
| G8 | **post_close 熔断未连线** | `engine.py:456` docstring 明列 follow-up TODO；equity 源缺口**其实已补**（query_asset 已实现），只差串联 |

### 2.3 纠正的记忆失实项（写代码前必知）

- ❌「19点算交易计划」→ ✅ 19:00 是行情播报；交易计划入口实际 @15:35（本 spec 挪到 19:00）
- ❌「check_exit 回测实盘同源」→ ✅ check_exit 是**已删 caisen 形态**的遗留纯函数，颈线法 `simulate_exit` 是独立状态机，**不调 check_exit**
- ❌「CandidatePlan/plan_id 体系」→ ✅ 未落地，现行是 `logs/trading_plans/plan_<date>.json` 以 date 为主键
- ❌「监控 30s」→ ✅ 现状 `*/5`（5min）且空转

---

## 3. 四支柱定位与每日时序编排

### 3.1 四支柱定位

```
数据支柱 ── 新增「主动实时性检查」(替换 mtime 被动统计)  ── 修 G1
   │  └─ 17:00 查T-1完整 + 18:30 查T完整 + 重采熔断
   ▼
交易支柱 ── eod_plan 挪19:00 + 开盘执行状态机          ── 修 G2/G3/G4/G7
   │  └─ 挂买单 → 成交挂止盈 → 30s止损/移动止损监控
   ▼
观测支柱 ── 成交+告警钉钉 + 持仓盈亏播报 + 交易日志      ── 修 G5/G6
```

### 3.2 每日时序编排（双检查点，brainstorm 决策 A）

```
【盘后 T日】
16:00  data bot 体检（mtime 健康度，现状保留）
17:00  检查点① 查T-1完整（历史数据应齐全；缺→告警）
18:00  Tushare 增量采集（拉T日数据，现状保留）
18:30  检查点② 查T完整 + 重采窗口开启（失败每15min重采至20:00，仍失败熔断）
19:00  eod_plan 算T+1计划 + 持仓盈亏播报 + 推钉钉人审   ← 从15:35挪来（修G2）
19:30  行情播报（避冲突）

【盘中 T+1日】
09:22  pre_open 挂买单（confirmed 后）
09:30+ 每30s 止损/止盈监控 + 成交钉钉 + 日志          ← 5min→30s（修G3）
15:30  post_close 对账 + 熔断（Phase4 连线，修G8）
```

**时序现实约束（不幻觉）**：Tushare `daily` 接口当日数据要等交易所清算完成，**一般 17:30-18:00 后才稳定**。故增量采集保留 18:00，检查点②放在 18:30（采集后）。研究员 TODO 的「17点」理解为「盘后」语义，实际查 T 数据推迟到 18:00 后。

### 3.3 调度落地

- engine 内四 cron（APSched ular）：eod_plan cron 改 `0 19 * * 1-5`；stop_loss 改 `IntervalTrigger(seconds=30)`（cron 最小粒度分钟，30s 必须 interval）。
- engine 外 schtasks：数据检查点①②走 `scripts/manage_ops_schtasks.py` 幂等注册（沿用 QuanterDataBrief 模式，新增两个 bat）。

---

## 4. 模块清单（新增 / 改造）

| 模块 | 类型 | 落点 | 动作 |
|---|---|---|---|
| 数据实时性检查 | 新增 | `data/freshness.py`（或 `data_service` 内新函数） | 交易日历 + 数据湖最新日期比对，判 T-1/T 是否齐全 |
| 数据检查点①②调度 | 新增 | `scripts/run_data_check.bat` ×2 + schtasks | 17:00/18:30 触发，失败重采熔断 |
| data bot 增强 | 改造 | `broadcast/brief_data.py` | 检查点结果并入播报（实时性 + mtime 双口径） |
| eod_plan 调度挪移 | 改造 | `trading/engine.py` cron | 15:35 → 19:00 |
| stop_prices 注入 | 改造 | `trading/engine.py _stoploss` | 从活跃计划读 `{symbol: stop_price}` 注入 monitor（修 G3） |
| 监控周期 | 改造 | `trading/engine.py` TradingEngine.__init__ | `*/5` cron → `IntervalTrigger(seconds=30)` |
| 成交回调链路 | 改造 | `trading/engine.py` 启动注册 `_on_order_update` | 成交→日志+钉钉+挂止盈（修 G5） |
| 成交通知 | 新增 | `infra/notifier.py notify_trade_event` | 区别于 `notify_risk_event`（风控告警） |
| 交易日志补写 | 改造 | `trading_service.record_live_trade` | 支持 on_stock_trade 成交回报补写（价/量/时间/方向） |
| 持仓盈亏计算 | 改造 | `trading_service.get_positions` | avg_price × 现价算浮盈 + 盈亏比（修 G6） |
| 止盈挂单（Phase1简化） | 新增 | `trading/engine.py` 回调内联 | 成交后挂单一固定止盈限价单（读 plan.take_profit） |
| 止盈分级状态机（Phase2） | 新增 | `trading/compute/tp_state.py` | 复刻 `simulate_exit`（tp1部分量+tp2剩余+撤单） |
| 移动止损（Phase3） | 改造 | `trading/engine.py` + `compute/stop.py` | `compute_stop_price`(grace/step/floor) 盘中动态更新注入 |
| post_close 熔断（Phase4） | 改造 | `trading/engine.py post_close` | query_asset.total_asset → check_daily_loss_limit → cancel_all → emergency_halt |

---

## 5. 关键决策记录（brainstorm 四连）

| 决策点 | 选定 | 理由 |
|---|---|---|
| 止盈止损语义 | **A 忠实复刻回测状态机** | 策略保真度底线，避免执行偏差（tp1落袋alpha丢失）；分阶段实施 |
| 人审节点 | **A 保留人审** | 演练安全网，避免代码 bug 污染验证；链路稳定后转全自动 |
| 数据失败处置 | **A 重采+超时熔断** | 不交易不自欺；重试自愈 Tushare 偶发抽风 |
| 时序编排 | **A 双检查点** | 贴「17点前查T-1/17点后查T」意图 + 尊重 Tushare 18:00 后稳定现实 |
| Phase1 止盈形态 | 简化版（单一固定止盈单）→ Phase2 升级分级 | 先跑通骨架，再消除执行偏差 |
| 30s 监控周期 | 按目标 30s 设计，**spec/plan 阶段实测限频后定终值** | 不拍脑袋；柜台有限频（代码已体现惰性同步避撞限频设计） |

---

## 6. Phase 1 详细设计（核心）

### 6.1 开盘执行链路数据流

```
① pre_open @09:22 ─ 挂买单（confirmed 后）              [现状已落地]
     读 plan_<date>.json → OrderRequest(buy) → _submit → order_stock_async
                          │
② on_stock_trade 回调 ─ 买单成交（FILLED/PARTIAL_FILLED） [★核心改造 C1]
     注册 _on_order_update（挂载点已存在，当前空转）→ 触发三连：
       ├─ a. record_live_trade 补写成交回报（价/量/时间/方向）   [日志]
       ├─ b. notify_trade_event 推钉钉成交通知                   [观测]
       └─ c. 挂止盈限价单（读 plan.take_profit，全额）           [Phase1简化]
                          │
③ stop_loss_monitor @30s ─ 盘中监控持仓                    [★核心改造 C2/C3]
     注入 stop_prices（从活跃计划读，修现状 None 空转）
     批量 get_quotes 取现价 → should_trigger_stop 跌破 → 发卖单
                          │
④ 止盈单/止损单成交 ─ 同样走 on_stock_trade 回调 → 日志 + 钉钉
                          │
⑤ post_close @15:30 ─ 对账 + （Phase4）熔断
```

### 6.2 四个核心改造点

**C1 成交回调链路（修 G5）**
- 现状：`broker/qmt.py:301` `_on_order_update` 注入点存在，注释明确预期用途（钉钉报警/State持久化），但 engine 启动时未注册。
- 改造：TradingEngine 启动时 `gw._on_order_update = self._handle_order_update`，回调内：
  - 仅 `OrderState in {FILLED, PARTIAL_FILLED}` 触发；
  - `record_live_trade(symbol, direction, traded_volume, traded_price, strategy, "成交回报")`；
  - `notify_trade_event(...)` fire-and-forget 异步（不阻塞回调线程）；
  - 买单成交 → 查 plan 拿 take_profit → 挂限价卖单（Phase1 全量；Phase2 分级）。
- 线程安全：回调经 `call_soon_threadsafe` 投递主线程 `_process_order_update`，再 `create_task` 调度 `_on_order_update`——钉钉走异步 fire-and-forget 不阻塞。

**C2 stop_prices 注入（修 G3）**
- 现状：`engine.py:649` `stop_loss_monitor(stop_prices=None)` 恒空转。
- 改造：`_stoploss` 从当日活跃计划读 `{symbol: stop_price}`（`trading_plan.load_plan(date)["orders"]` 的 stop_price 字段）注入。

**C3 监控周期（修 G3 配套）**
- 现状：`*/5 9-14` cron。
- 改造：`IntervalTrigger(seconds=30)`，仍受 `calendar.is_intraday_session` 约束（9:30-11:30/13:00-15:00）。
- **限频现实约束（待实测）**：每轮 `get_quotes`(批量1次) + `query_stock_positions`(1次)。柜台有限频（代码惰性同步设计已体现）。plan 阶段在模拟盘实测 30s 是否触发限流；若限流，上调到不触发上限（候选 60s），并在日志标注。

**C4 持仓盈亏播报（修 G6）**
- 改造：19:00 eod_plan 末尾，`query_asset()` 拿 total_asset/cash，`_fetch_broker_positions()` 拿各仓 avg_price，`get_quotes()` 批量取现价 → 逐仓浮盈 = (现价-avg_price)×volume，盈亏比 = 浮动盈亏/成本。推钉钉（持仓表 + 总资产 + 浮盈合计）。

### 6.3 数据实时性检查（修 G1，子诉求 1<1>）

**检查逻辑**（新模块 `data/freshness.py`）：
1. 用 `calendar` 算期望最新交易日：今天是交易日且盘后 → 期望 T；否则期望 T-1。
2. 查数据湖核心数据集（颈线法依赖：`a_shares_daily` 为主，按需扩展成交额/ATR源）的最新日期 = 读 parquet date index max。
3. 比对：最新日期 ≥ 期望 → PASS；否则 FAIL。

**双检查点**：
- ①@17:00 查 T-1：历史数据应齐全，FAIL 说明历史采集有洞 → 告警（不熔断计划，T-1 历史缺不影响 T+1 计划的 T 日数据）。
- ②@18:30 查 T：T 日数据是 T+1 计划的输入，FAIL → 触发重采窗口（每 15min 重跑 `sync_incremental` 至 20:00），仍 FAIL → 熔断 eod_plan（不生成计划 + 钉钉 ERROR 告警）。

**失败处置（brainstorm 决策 A）**：重采 + 超时熔断。绝不用 T-1 兜底算 T+1（前视偏差，与「检查实时性」初衷矛盾）。

### 6.4 交易计划与持仓盈亏（修 G2/G6，子诉求 1<2>）

- eod_plan @19:00（数据已落湖）：复用现状 `scan_live` 产信号 → `build_orders_from_signals` → save_plan(confirmed=False) → 推钉钉等人审。
- 持仓盈亏播报：见 C4。
- 盈亏比：见 C4（逐仓 + 合计）。

### 6.5 OrderState 状态机（成交回调触发边界）

```
PENDING → SUBMITTED → PARTIAL_FILLED → FILLED★(终态)
                    ↘ CANCELLED(终态) / PARTIAL_CANCELLED(终态)
        ↘ REJECTED(终态) / FAILED(终态)
```
- 成交回调仅在 FILLED / PARTIAL_FILLED 触发。
- 止盈单/止损单复用同一状态机。

---

## 7. Phase 2/3/4 演进

### Phase 2：止盈分级状态机（消除执行偏差）
- 复刻 `strategies/neckline/backtest.py simulate_exit` 到实盘（新模块 `trading/compute/tp_state.py`）。
- 参数对齐 `EXEC_DEFAULTS`：tp1_h_mult / tp1_portion / cancel_thresh_mult / max_wait / cooldown。
- 成交后挂 tp1 限价卖单（tp1_portion 比例量）+ tp2 限价卖单（剩余量）；超时（max_wait）撤单；回踩/撤单逻辑忠实复刻。
- **消除 Phase1 简化版的执行偏差**（tp1 落袋 alpha 回归）。

### Phase 3：移动止损动态更新
- `compute_stop_price(neckline, atr, holding_days, stop_atr_mult, grace, step, floor)` 盘中每日重算。
- 引擎状态层维护 `{symbol: stop_price}`，盘中注入 stop_loss_monitor 动态更新（修 G7）。
- 参数从 env 读（`TRADE_STOPLOSS_GRACE_DAYS/STEP_ATR/FLOOR`，现状已配默认值）。

### Phase 4：post_close 熔断连线 + 生产级
- `post_close` 串联：`gw.query_asset().total_asset` 作 equity → `check_daily_loss_limit(start_equity, curr_equity)` → 命中即 `cancel_all_open_orders(gw)` + `emergency_halt()`（修 G8）。
- start_equity 持久化（盘前快照 total_asset）。
- 断线重连端到端验证（_reconnect 已实现，验证恢复后主推/状态一致性）。

---

## 8. 错误处理与边界（Grill Me 拷问，spec 固化）

| 边界 | 防御 |
|---|---|
| **重复挂止盈** | 部分/多次成交回报 → 「symbol→已挂止盈」幂等标记，不重复挂 |
| **部分成交量** | 买单 PARTIAL_FILLED → 止盈单按**已成交量**挂，非计划全量 |
| **盲价** | 现价 None/NaN 绝不发卖单（现状 stop_loss_monitor 已有，保留） |
| **撤单失败** | seq→real_order_id 缺失撤单返 FAILED → 告警 + 人工（不静默吞） |
| **查持仓失败** | 拒发任何卖出单（敞口未明即操作 = 盲卖，现状已有，保留） |
| **断线窗口** | lock_down=True 期间 submit_order 被网关拒（现状已有） |
| **30s 限频** | 实测后定终值，限流则上调周期（见 6.2 C3） |
| **Tushare 抽风** | 重采窗口自愈；超时熔断不交易（见 6.3） |
| **数据湖查空** | parquet date index max 缺失 → 视同 FAIL，不猜 |

---

## 9. 测试策略

- **单元测试**（纯函数，TDD）：
  - `data/freshness.py` 检查逻辑（期望交易日计算 + 比对）。
  - 止盈挂单决策（成交→挂止盈的幂等/部分量）。
  - 盈亏比计算。
- **集成测试**（monkeypatch 网关）：
  - 成交回调链路（注入 mock _on_order_update，验证日志+钉钉+挂止盈三连）。
  - stop_prices 注入（验证 monitor 不再空转）。
- **契约测试**：守 Layer2 spec §7 六铁律（不破坏五模块单向依赖）。
- **模拟盘 smoke**：`scripts/smoke_trading_engine.py`（现状已有）扩展，覆盖完整一日链路。
- **影子对照（可选）**：dry_run 影子与 live 模拟盘并行，对比执行差异。

---

## 10. 关键待核实点（不幻觉，plan/实施阶段实证）

| 待核实 | 方法 | 影响 |
|---|---|---|
| miniQMT 30s 监控限频 | 模拟盘实测 get_quotes+query_positions 连续调用 | 定监控终值（30s/60s） |
| Tushare 当日 daily 真实可用时间 | 实测 17:00/17:30/18:00 查当日 daily | 定检查点②最早触发 |
| on_stock_trade 回调字段 | 查 xtquant XtTrade 字段（traded_volume/traded_price/traded_time/order_id） | 成交日志/钉钉字段 |
| `_on_order_update` 注册时序 | 查 engine 启动流程，确认 gw 连接后注册 | 避免回调漏注册 |

---

## 11. 分阶段实施路线与验收

| 阶段 | 范围 | 验收标准 |
|---|---|---|
| **Phase 1 骨架** | C1-C4 + 数据检查①② + eod_plan@19:00 + 盈亏播报；止盈简化版 | 模拟盘单日端到端跑通：数据检查→计划→人审→挂单→成交钉钉日志→止损监控→盈亏播报，零漏单零重单 |
| **Phase 2 止盈分级** | tp1/tp2 状态机复刻 | 止盈语义与回测 simulate_exit 一致（执行偏差归零，影子对照验证） |
| **Phase 3 移动止损** | compute_stop_price 动态更新 | grace/step/floor 生效，止损价随持仓天数收紧 |
| **Phase 4 生产级** | post_close 熔断 + 断线验证 | 日内回撤熔断可触发；断线重连后状态一致 |

---

## 12. 不在本 spec 范围（follow-up / 方向2）

- **方向2 颈线 param_iter 全维度持久化**：独立 spec，本 spec 不涉及。
- **schema 漂移修复**（param_iter 21维 vs NecklineConfig 18维）：属方向2。
- **三套调优设施收敛**（param_iter JSON / Parameter Lab / Training Loop）：属方向2 Spec4。
- **全自动去人审**：Phase1 保留人审，待链路稳定 N 天后另起小改动。
- **切 live**：本 spec 是模拟盘演练，live 准入需 Phase4 完成 + 影子观测 ≥5 天。
