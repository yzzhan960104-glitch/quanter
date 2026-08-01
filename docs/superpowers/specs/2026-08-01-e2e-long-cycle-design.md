# C1-C7 长周期 E2E 时序回放测试

- **日期**：2026-08-01
- **分支**：feat/e2e-long-cycle（spec 阶段）
- **状态**：待审（spec review gate）
- **关联**：
  - C-5（进程模型 + `_gw_health_gate`）/ C-6（`trading.clock` 单一时间源）/ C-7（start_all 收编 + discovery 进 lifespan）
  - 既有 e2e 范式：`tests/trading/test_e2e_trading_flow.py`（单日 4 步 + 6 表一致性）、`tests/trading/test_clock_e2e_freeze.py`（clock freeze 单一口子）、`tests/discovery/test_plan4_e2e.py`（discovery e2e）
  - memory：[[eod-date-offbyone-fix]]（跨日 key 对齐）/ [[gap4 持仓账本]]（部分成交精度）/ [[qmt-live-smoke-findings]]（拒单/主推延迟）/ [[neckline-algorithm-gaps]]（颈线法扫描）
- **范围**：写一个长周期 E2E 时序回放套件，模拟 2026-07-01 ~ 07-31 共 23 个交易日的全流程行为（mock QMT 行为层，真实信号扫描 + 真实分钟行情 + 真推钉钉 + connect 真起），跑完生成汇总文档（每张表落了哪些数据 + 是否符合预期）。

---

## 1. 背景与现状

### 1.1 痛点
C1-C7 重构已 merged master（1180 passed/0），理论上全流程可稳定自动化跑通，但既有 e2e 都是**单日或双日**维度的局部串联（交易 4 步 / clock 对齐 / discovery plan4），没有「跨月 × 全 job × 概率异常 × 数据落表」的**长周期整体回归**。重构的真正价值（多日时序下 key 对齐 / 韧性机制触发 / 表间一致性不漂 / 钉钉推送链路）需要长周期才能暴露。

### 1.2 现状（master HEAD）
| 项 | 现状 | 证据 |
|---|---|---|
| clock 单一源 | `trading.clock.now/today/trading_day` | C-6；`test_clock_e2e_freeze.py` 验证 patch 单一口子冻结全包 |
| 单日 e2e 范式 | 4 步闭环 + 6 表一致性 + 韧性 4 场景 | `test_e2e_trading_flow.py`（isolated fixture + mock gw/钉钉 + `_handle_order_update` 注入成交） |
| `_stoploss` 行情源 | `qmt_market_data.get_quotes` → `xtdata.get_full_tick` | engine.py:1036；注释明确「无 xtdata 时 live 前需另接行情源」 |
| QMT mock 抽象 | `engine.get_gateway` + `_submit` + MagicMock gw | `test_e2e_trading_flow.py` 范式成熟 |
| 信号扫描真身 | `engine._eod` → `detect_signal(symbol, df_upto, ...)` → `eod_plan` | strategies/neckline/method_v0.py:302（无前视纯函数） |
| 钉钉配置 | `.env` 完备 | DINGTALK_WEBHOOK + APP_KEY/SECRET + 5 connect bot（CLI/DATA/REVIEW/STRATEGY/TRADING） |
| data_lake 7 月 | 23 交易日全覆盖，无缺采日 | 实测每日标的数 5218-5528（均值 5338） |
| Tushare 分钟级 | `stk_mins` 接口可用 | 实测 300001.SZ 5min 返 49 行（OHLCV），token 有权限 |
| 状态库表 | trade_event/order/fill/position/account/account_daily/data_ready | state-store-redesign 6 表 + data_ready |

### 1.3 核心挑战
- **时间驱动**：长跑 server 逐日推进 clock 不可行 → 单进程 clock-freeze 时序回放（方案 A）。
- **`_stoploss` 行情缺口**：mock QMT 后 xtdata 行情源空 → Tushare `stk_mins` 历史分钟线填补（真实价格驱动止损/止盈/cancel_on）。
- **概率模拟的"符合预期"口径**：概率成交下精确数值不可预期 → 结构性完整性 + 表间一致性 + 韧性事件覆盖率 + 时序口径 4 类校验。

---

## 2. 目标与非目标

### 目标
1. **时序回放器**：单进程 in-process，clock-freeze 逐日推进 7/1-7/31 共 23 交易日，每日 4 阶段（pipeline_then_eod / pre_open / _stoploss / post_close）。
2. **真实信号扫描**：直调 `engine._eod()` 真身，扫创板科创 ~500 标的 × 真实 7 月日线（data_lake parquet），产真实颈线法信号。
3. **真实分钟行情**：Tushare `stk_mins` 拉当日 5min bar → 按盘中时点切片累积 high/low/last → 注入 `qmt_market_data.get_quotes`，驱动 `_stoploss` 的 `decide_exit` 真实触发止损/止盈/cancel_on。
4. **概率成交模拟**：mock QMT gw 行为层（成交/拒单/部分成交/主推延迟/熔断/超期），固定种子可重复；价格全部真实（stk_mins）。
5. **真推钉钉 + connect 真起**：`notify_*/push_*` 真调钉钉 API 推测试群；`connect_manager.start` 拉 5 个常驻 Claude Code 子进程（C-7 V1 范式），teardown `stop` 树杀。
6. **discovery 触发真 daemon mock**：engine.sched cron 02:00 注册真 + `_run_discovery_subprocess` mock + `_discovery_missed_last_run` 补跑判定验证（C-7 V2/V3）。
7. **汇总文档**：md 报告，每张表逐日落点 + 4 类预期校验 + 推送记录 + 异常清单。

### 非目标（显式 out of scope）
- **不真起 uvicorn**：C-7 lifespan 装配（connect/discovery 进 lifespan）由 `test_lifespan_consolidation.py` 11 用例覆盖，本 E2E 不重跑（方案 A 决策）。
- **不真跑 discovery daemon**：discovery 重型 4h 全市场扫描，22 次 × 4h 不可行；只验证触发机制（cron 注册 + 补跑），daemon 执行体 mock（discovery e2e 已有 plan3/plan4）。
- **不追求精确数值预期**：概率模拟下不断言"7/15 应有 3 笔成交"，只断言事件链完整 + 表间零漂 + 韧性覆盖 + 时序对齐。
- **不改生产代码**：E2E 是测试套件，不动 engine/clock/state_store 等生产模块（如需测试钩子，显式列在测试侧，不污染生产）。
- **不做 C-8 全 job 启动补跑**：本 E2E 时序由回放器显式编排驱动，不验证"offline 跨 02:00 全 job 补跑一致性"（那是 C-8 独立 project；C-7 已做 discovery 补跑）。

---

## 3. 架构（方案 A：单进程 clock-freeze 时序回放）

### 3.1 整体
独立 pytest 套件 `tests/e2e_long_cycle/`，单进程 in-process。回放器主循环遍历 7 月交易日历，每日 4 阶段 freeze `trading.clock.now` 推进时间，直调 `engine` 模块级/方法级函数（与 `test_e2e_trading_flow.py` 同范式）。

mock 边界严格：
- **真身**：信号扫描（`_eod`/`detect_signal`）、计划（`eod_plan`/`save_plan`）、挂单编排（`pre_open`）、止损判定（`decide_exit`）、对账（`post_close`）、表落库（state_store）、钉钉推送（notify_*）、connect 机器人（connect_manager）。
- **mock**：QMT gw 行为层（`get_gateway` 返 MagicMock + `_submit`/`_handle_order_update` 概率注入）、采集 subprocess（pipeline_then_eod 内）、`qmt_market_data.get_quotes`（注入 stk_mins 真实分钟价）、discovery daemon 执行体。

### 3.2 时序驱动（clock-freeze 逐日推进）
C-6 的 `trading.clock.now/today/trading_day` 是单一时间口子，patch 一处即冻结全包：

```
for T in 7月交易日历([2026-07-01 .. 2026-07-31]):   # 23 交易日
    freeze clock → T 日 19:00  → pipeline_then_eod（采集mock + freshness + _eod 扫信号落 T+1 plan）
    freeze clock → T+1 09:25   → pre_open（确认闸 + 撤昨日 + 挂 T+1 单，gw 概率拒单/部分成交）
    freeze clock → T+1 盘中 N 时点 → _stoploss（MinBarFeeder 注入分钟行情 + decide_exit 真实触发）
    freeze clock → T+1 15:30   → post_close（对账 + 熔断 + trailing + 超期 + trade_event/account_daily）
    快照每张表（T+1 落点）→ ReportBuilder
```

- **日历源**：从 `data_lake/a_shares_daily.parquet` 取 7 月真实交易日（23 日全覆盖），与生产 `trading.calendar` 同源。
- **clock patch**：`monkeypatch.setattr("trading.clock.now", lambda: fixed_dt)`；`today()/trading_day()` 自动一致派生（C-6 V4 验证范式）。
- **盘中回放粒度**：每日 N≈8 时点（9:30/10:00/10:30/11:00/11:30/13:30/14:30/15:00），每时点跑一次 `_stoploss`，验证盘中即时触发（如某标的 13:45 触止损而非 10:30）。8 时点 × 23 日 ≈ 184 次 _stoploss。

### 3.3 不变量
- 跨日 key 对齐：eod 落 `trading_day(T)=T+1` = 次日 pre_open 读 `today()=T+1`（[[eod-date-offbyone-fix]] 长周期回归）。
- 生产同源软降级：E2E 不额外兜底，沿用 engine 各 job try/except；单日阶段异常 → 记录 + 跳下一日。
- 价格全真：信号 T 日日线 + 成交/止损 T+1 当日分钟（stk_mins）；只有"是否成交"是概率模拟。

---

## 4. 组件清单（9 个，各自单一职责）

| # | 组件 | 职责 | 真/mock |
|---|---|---|---|
| 1 | **时序回放器** `ReplayDriver` | 日历驱动 + clock freeze + 串每日 4 阶段 + 阶段异常容错（跳下一日） | 编排层 |
| 2 | **真实信号扫描** | 直调 `engine._eod()`（创板科创 ~500 × 真实 7 月日线扫颈线法） | **真身** |
| 3 | **QMT 概率成交模拟器** `ProbabilisticBroker` | mock gw（成交/拒单/部分成交/主推延迟）+ 概率注入；成交价用 stk_mins 真实分钟价；构造熔断/超期场景 | **mock QMT 行为** |
| 4 | **钉钉真推层** | `notify_*/push_*` 真调 API + 推送日志落表（汇总用） | **真推** |
| 5 | **connect 真起** | `connect_manager.start` 5 bot（C-7 V1 范式）+ teardown `stop` 树杀 | **真起** |
| 6 | **discovery 触发层** | engine.sched cron 02:00 注册真 + `_run_discovery_subprocess` mock + `_discovery_missed_last_run` 补跑判定 | 触发真/**daemon mock** |
| 7 | **分钟行情源** `MinBarFeeder` | Tushare `stk_mins` 拉当日 5min bar → 按时点切片累积 high/low/last → 注入 `qmt_market_data.get_quotes` | **真行情** |
| 8 | **数据落表校验器** `TableSnapshotCollector` | 每日每表快照（trade_event/order/fill/position/account/account_daily/data_ready + plan JSON + review md） | 真身读 |
| 9 | **汇总文档生成器** `ReportBuilder` | md 报告：运行配置 + 22 日总览 + 每张表逐日落点 + 4 类校验 + 推送记录 + 异常清单 | 编排层 |

**组件隔离**：每组件单一职责、可独立理解/测试。`ProbabilisticBroker` 是唯一行为模拟器（固定种子 + 概率分布）；`MinBarFeeder` 是真实数据源适配器；其余是真身调用或编排/校验。

---

## 5. 每日时序编排（T+1 日 4 阶段）

```
[T 日 19:00] pipeline_then_eod
   ├─ mock subprocess（采集）+ check_freshness（真读 data_lake T 日日线）
   └─ _eod 真身扫创板科创 ~500（detect_signal × df_upto[T日]）→ eod_plan 落 T+1 plan（confirmed=AUTO_CONFIRM_PLAN）
[T+1 09:25] pre_open
   ├─ confirm 闸 + 撤昨日（gw.query_orders mock）
   ├─ _submit 挂 T+1 plan 单 → ProbabilisticBroker 按概率返 FILLED/PARTIAL/REJECTED
   └─ 成交价 = stk_mins T+1 09:25 时点价
[T+1 盘中 9:30→15:00，8 时点 freeze 推进] _stoploss 巡检
   ├─ MinBarFeeder 取 9:30→当前时点 stk_mins 累积 high/low/last → 注入 get_quotes
   ├─ decide_exit 真身判定（真实分钟价驱动止损/止盈/cancel_on 触发）
   ├─ 概率主推延迟（成交回报延后 1 时点注入）
   └─ TP1/TP2 预挂限价单：stk_mins 当日 high ≥ tp 价 → 注入 FILLED
[T+1 15:30] post_close
   ├─ 对账（gw._fetch_broker_positions mock vs position_book）+ 熔断判定（start vs curr，构造日 curr=start×0.96）
   ├─ trailing 演进 + max_holding 超期标记
   └─ trade_event(CLOSED/TP_FILLED) + account_daily 收盘快照落表
```

---

## 6. 分钟行情源（`MinBarFeeder` · stk_mins 填 `_stoploss` xtdata 缺口）

### 6.1 物理意图
`_stoploss` 依赖 `qmt_market_data.get_quotes`（xtdata 当日累积 high/low + last_price）判 decide_exit。E2E mock QMT 后 xtdata 通道空 → 用 Tushare `stk_mins` 历史分钟线回放当日盘中价格演进，注入 `get_quotes`，使止损/止盈/cancel_on 触发有真实价格依据（非概率瞎猜）。

### 6.2 实现
- **数据源**：`pro.stk_mins(ts_code=..., start_date=T+1 09:00:00, end_date=T+1 15:00:00, freq='5min')`（已验证权限 + 字段 ts_code/trade_time/open/high/low/close/vol/amount）。
- **采集**：每日 _stoploss 前拉当日 `relevant_syms`（持仓 ∪ 挂单标的，~10-30 只）的 stk_mins，tmp cache（同标的同日不重复）。23 日 × ~30 只 ≈ 690 次 API，Tushare 限频内。
- **时点切片**：freeze 在某盘中时点（如 10:30），取 9:30-10:30 的 stk_mins bar，累积 high=max(bar.high)、low=min(bar.low)、last=末根 close → 返 `{sym: {last_price, high, low}}`。
- **注入**：`monkeypatch qmt_market_data.get_quotes` 返 MinBarFeeder 的累积快照。
- **降级**：stk_mins 拉失败（限频/停牌）→ data_lake T+1 日线 high/low 近似 + 告警入汇总 §5。

---

## 7. 概率成交模拟规则（`ProbabilisticBroker` · 固定种子可重复）

| 事件 | 概率/触发 | 物理意图 |
|---|---|---|
| 全部成交 FILLED | 70% | 正常态 |
| 部分成交 PARTIAL_FILLED | 15% | traded_volume < qty（[[gap4 持仓账本]] 部分成交精度） |
| 柜台拒单 REJECTED | 5% | 涨停价买单被柜拒（[[qmt-live-smoke-findings]]） |
| 主推延迟 | 10% | 成交回报延后 1 时点注入（QMT 主推 1-2s） |
| **日内熔断**（构造场景） | 指定日（如 T+10）curr_equity = start×0.96 | -3% 熔断（cancel_all + emergency_halt + lock_down） |
| **超期持仓**（构造场景） | 指定标的 holding_days > max_holding | 次日 pre_open 跌停价平仓 |
| **TP1/TP2 止盈成交** | stk_mins 当日 high ≥ tp 价即触发 | 真实价格驱动（非概率） |
| **cancel_on 撤单** | stk_mins 当日 high ≥ cancel_on 即触发 | 真实价格驱动（pending 期撤买单） |

固定随机种子 → 事件序列可重复；概率参数（70/15/5/10%）与构造日（熔断/超期）在回放器配置层可调。价格全部取 stk_mins 真实分钟价。

---

## 8. 汇总文档结构（`ReportBuilder` → md）

```
# C1-C7 长周期 E2E 测试报告（2026-07-01 ~ 07-31）

## 0. 运行配置
   日期范围 / 创板科创 ~500 / 概率种子 / 盘中 8 时点 / stk_mins 行情源 / connect 5 bot 真起

## 1. 23 日时序执行总览
   表格：日期 × 4 阶段（pipeline_then_eod / pre_open / _stoploss / post_close）= PASS/FAIL/SKIP
   + 每日计划单数 / 成交数 / 持仓数 / 当日 pnl

## 2. 每张表逐日落点（"落了哪些数据"）
   ### 2.1 trade_event — 事件链覆盖（每日每类计数：SIGNAL/CONFIRMED/ORDERED/FILLED/CANCELED/CLOSED/STOP_TRIGGERED/TP1_FILLED/TP2_FILLED）
   ### 2.2 order — 每日委托（OPEN/STOP/TP1/TP2 × 终态 FILLED/PARTIAL/REJECTED/CANCELED）
   ### 2.3 fill — 每日成交笔数 + 净持仓变化
   ### 2.4 position — 每日持仓快照（symbol→qty，含 entry_date/holding_days）
   ### 2.5 account / account_daily — 每日 start/close equity + 日内 pnl + 熔断标记
   ### 2.6 data_ready — 每日采集就绪状态（daily/minute）
   ### 2.7 trading_plan（JSON）— 每日计划单数 + confirmed 状态
   ### 2.8 review_report（md）— 每日复盘生成情况 + drift 标记
   ### 2.9 discovery — cron 注册 + 补跑触发记录（daemon mock 不跑）

## 3. 预期校验结果（4 类口径，每类 ✓/✗ + 违规清单）
   ### 3.1 结构性完整性
   ### 3.2 表间一致性
   ### 3.3 韧性事件覆盖率（阈值 vs 实际）
   ### 3.4 时序与口径

## 4. 钉钉推送记录（每条：时点 + 命中机器人 + 内容摘要 + 成功/失败）

## 5. 异常 / 降级清单（行情降级 / 软降级告警 / connect 崩溃 / 推送失败）

## 6. 结论（全绿 / 有违规需排查）
```

---

## 9. 预期校验口径（概率模拟下 4 类）

| 类 | 口径 | 例子 |
|---|---|---|
| **a 结构性完整性** | 事件链无孤儿、无断链 | 每个 trade_id 链 `SIGNAL→CONFIRMED→ORDERED→FILLED/CANCELED→[CLOSED/TP_FILLED]`；FILLED 必有前置 ORDERED；CLOSED 必有持仓归零 |
| **b 表间一致性** | 跨表对账零漂 | `order.FILLED 量 = fill 笔数`；`fill 净持仓 = position 端持仓`；`account_daily.close = 次日 start`；trade_id 在 trade_event/order/fill/position 四表贯穿 |
| **c 韧性事件覆盖率** | 固定种子下 ≥ 阈值 | 熔断 ≥1（构造日）/ 超期平仓 ≥1（构造标的）/ 部分成交 ≥15%×单数 / 拒单 ≥5%×单数；TP 止盈 + cancel_on 撤单为 stk_mins 真实价格驱动（记录实际次数，不强制阈值，plan 定可观测下限用于告警） |
| **d 时序与口径** | 跨日 key 对齐 + 23 日全跑 | `eod 落 trading_day(T)=T+1` = `次日 pre_open 读 today()=T+1`；每日 4 阶段都执行；clock freeze 跨日无漂移 |

概率模拟下不断言精确数值（如"7/15 应 3 笔成交"），而是事件链完整 + 表间零漂 + 韧性机制确实被触发并正确记录。

---

## 10. 错误处理 + 软降级

- **生产同源软降级**：E2E 不额外兜底，沿用 engine 各 job try/except。单日某阶段异常 → 记汇总 §5 + 跳下一日（保证 23 日尽量跑完，§1 总览体现哪日哪阶段 FAIL）。
- **行情降级**：stk_mins 拉失败（限频/停牌）→ data_lake 日线 high/low 近似 + 告警入 §5。
- **钉钉真推失败**：网络/API 异常不中断回放，记 §4 推送失败。
- **connect 崩溃**：单 bot start 失败软降级（C-7 V1 范式），其余 bot 继续，入 §5；teardown `stop` 树杀所有已起 bot（try/except 兜底）。
- **discovery daemon**：纯 mock，无异常风险。

---

## 11. 测试策略

### 11.1 pytest 自动化校验（gate，互补于人类可读 md）
- 汇总 md 生成成功
- §3.b 表间一致性：order↔fill↔position 对账零漂
- §3.c 韧性覆盖率：熔断/超期/部分成交/拒单/TP 各 ≥ 阈值
- §3.a 事件链：无孤儿事件
- §3.d 时序：23 日都跑 + 跨日 key 对齐

### 11.2 组件单测（每个组件独立可测）
- `ReplayDriver`：mock 依赖，验日历驱动 + clock freeze 推进 + 阶段异常跳日
- `ProbabilisticBroker`：固定种子，验概率分布 + 构造场景触发
- `MinBarFeeder`：mock stk_mins，验时点切片累积 + 降级
- `TableSnapshotCollector`：tmp db，验每表快照
- `ReportBuilder`：快照数据，验 md 结构 + 4 类校验逻辑

### 11.3 全量回归基线
master 当前 1180 passed/0；本 E2E 套件新增不破坏既有（独立 `tests/e2e_long_cycle/`，不影响既有套件）。

---

## 12. 风险

| # | 风险 | 缓解 |
|---|---|---|
| R1 | stk_mins 限频（23 日 × ~30 只 ≈ 690 次） | tmp cache（同标的同日不重复）+ 失败重试 + 日线降级 + 告警入 §5 |
| R2 | connect 5 bot 真起成本（5 常驻 Claude Code 子进程） | teardown `connect_manager.stop` 树杀；空转不消耗 LLM quota（仅 @ 响应计费）；fixture scope=session 起/停一次 |
| R3 | 真推钉钉污染测试群（~100-200 条） | 专用测试群（DINGTALK_WEBHOOK 指向测试群）；汇总 §4 全量推送记录可审计 |
| R4 | 概率模拟下韧性事件未达覆盖率阈值（如熔断 0 次） | 构造场景兜底（指定日熔断/指定标的超期）+ 种子可调；阈值是"≥1"非精确 |
| R5 | 运行时长（23 日 × 8 时点 × 创板科创 500 扫描） | 预估 30-90min；pytest mark `@pytest.mark.e2e_long` 可单独跑；CI 不默认跑（手动/nightly） |
| R6 | data_lake 7 月个股级漏采（[[data-lake-integrity-gap]] 300214.SZ 等） | 颈线法流动性过滤（近 30 日均额 ≥1 亿）排除低流动；漏采标的降级跳过 + 告警 |
| R7 | `_stoploss` 盘中时段判定（`is_intraday_session`）依赖 clock.now | clock freeze 到盘中时点（9:30-11:30/13:00-15:00）确保进入盘中分支 |
| R8 | connect_manager 起的 5 bot 在 E2E 期间响应测试群 @ 消息（LLM 副作用） | 测试群专用，E2E 期间无人 @；或 E2E 期间 disconnect 机器人 webhook（仅验 start/stop 生命周期） |

---

## 13. 验收标准

1. **时序回放**：23 交易日 × 4 阶段全跑，clock-freeze 跨日推进，汇总 §1 总览全 PASS（或 FAIL 明确归因）。
2. **真实信号**：`_eod` 真身扫创板科创 ~500，汇总 §2.7 plan JSON 落点 ≥ 0 单/日（无信号日允许 0）。
3. **真实分钟行情**：stk_mins 注入 `_stoploss`，§2.1 trade_event 有 STOP_TRIGGERED/TP_FILLED/CANCEL 事件由真实分钟价触发（非概率）。
4. **概率成交**：固定种子可重复，§3.c 韧性覆盖率达标（熔断 ≥1 / 超期 ≥1 / 部分成交 ≥15% / 拒单 ≥5%）。
5. **真推钉钉 + connect**：§4 推送记录全量可审计，connect 5 bot start/stop 生命周期完整。
6. **discovery 触发**：engine.sched cron 02:00 注册 + 补跑判定两态（错过/未错过）覆盖，daemon mock 不跑。
7. **汇总文档**：md 生成，§2 每张表逐日落点 + §3 四类校验 + §4 推送 + §5 异常齐全。
8. **表间一致性**：§3.b order↔fill↔position 对账零漂，account_daily 连续。
9. **时序对齐**：§3.d 跨日 key 对齐（eod 落 T+1 = pre_open 读 T+1），23 日无漂移。
10. **不破坏既有**：全量回归 1180 passed/0 不退化（E2E 独立目录隔离）。

---

## 14. 实现步骤（高层 · 详细 diff 见 plan）

| 阶段 | 内容 | gate |
|---|---|---|
| **V1 时序回放器骨架** | ReplayDriver + clock-freeze 日历驱动 + 4 阶段空壳（mock job）+ 阶段异常容错 | 单测：日历推进 + clock freeze + 跳日 |
| **V2 真实信号扫描接入** | 直调 engine._eod（创板科创 500 × 真实日线）+ tmp DB/plan 隔离 | 单测：单日 _eod 落 plan + signals 真实 |
| **V3 分钟行情源** | MinBarFeeder（stk_mins 采集 + 时点切片 + 注入 get_quotes + 降级） | 单测：切片累积 + 降级 |
| **V4 概率成交模拟器** | ProbabilisticBroker（gw mock + 概率注入 + 构造场景）+ 成交价用 stk_mins | 单测：概率分布 + 熔断/超期构造 |
| **V5 钉钉真推 + connect 真起 + discovery 触发** | notify_* 真推 + connect_manager start/stop + discovery cron/补跑 mock | 单测：推送日志 + connect 生命周期 + discovery 两态 |
| **V6 数据落表校验 + 汇总文档** | TableSnapshotCollector + ReportBuilder（md + 4 类校验） | 单测：快照 + md 结构 + 校验逻辑 |
| **V7 全链路组装 + 全量回归** | ReplayDriver 串全组件跑 23 日 + pytest 自动化校验断言 + 全量回归零退化 | 23 日跑通 + §3 四类校验全绿 + 1180 不退化 |

---

## 15. spec review 要点

1. **方案 A 单进程 clock-freeze 时序回放**（不真起 uvicorn，C-7 lifespan 由既有测试覆盖）—— 接受？
2. **真实信号扫描（创板科创 ~500）+ 真实分钟行情（stk_mins）+ 概率成交（mock QMT 行为）** 的真实/mock 边界 —— 接受？
3. **stk_mins 填 `_stoploss` xtdata 行情缺口**（注入 get_quotes 驱动 decide_exit 真实触发）—— 接受？
4. **真推钉钉 + connect 5 bot 真起**（专用测试群 + teardown 树杀）—— 接受？
5. **概率模拟下"符合预期" = 结构性 + 一致性 + 覆盖率 + 时序 4 类**（不追求精确数值）—— 接受？
6. **discovery 只验触发不跑 daemon**（daemon mock，discovery e2e 已有 plan3/plan4）—— 接受？

spec 通过后落 plan（`docs/superpowers/plans/2026-08-01-e2e-long-cycle.md`）。
