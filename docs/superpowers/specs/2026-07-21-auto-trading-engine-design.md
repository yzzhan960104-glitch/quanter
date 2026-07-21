# 自动交易引擎 设计（第二期）

- **日期**：2026-07-21
- **状态**：设计已与研究员对齐核心模型，待出实施计划
- **范围**：第二期「自动交易引擎」——T-1 定计划 + 开盘挂单 + 盘中止损监控 + 盘后对账
- **前置依赖**：第一期观测运营层（钉钉播报确认闸 + 后台看板）需先上线，复用其钉钉通道与看板

---

## 1. 背景与目标

### 1.1 定位
第一期已铺好「观测运营层」（钉钉每日跟踪 + 后台看板）。第二期把颈线法策略从「只能回测」推进到「**模拟盘半自动实盘交易**」——T-1 日晚机器人在钉钉推交易计划，人工确认后，T 日开盘前自动挂单、盘中被动止损、盘后自动对账。

### 1.2 自动化程度（已定）
**盘前人工确认闸 + 盘中盘后自动**：每日盘后跑信号 → 钉钉推计划 → 人工确认 → 次日开盘前自动挂单 → 盘中只做被动止损触发（不移动止损位/不追涨/不调仓）→ 盘后自动对账 + 重算次日止损位。

### 1.3 风控红线
模拟盘阶段，任何自动下单前必须先过 **N 天影子模式（dry_run）** 验证计划 vs 实际偏差；真单受 risk_shield 10 关挡板 + 熔断阈值约束。

---

## 2. A 股技术约束（已查证，非臆造）

| 约束 | 事实来源 | 对设计的影响 |
|---|---|---|
| **xtquant 无原生条件单/止损单** | `xtquant/doc/xttrader.md` 全文 grep，报价类型仅限价 FIX_PRICE / 市价 LATEST_PRICE | 止盈=柜台限价卖单可行；**止损必须盘中监控发卖单** |
| **T+1** | A 股交易规则 | 当日买入当日不能卖；T 日能挂的卖单只针对 T-1 及更早的已有持仓 |
| **卖单前置** | 柜台规则 | 止盈/止损卖出单必须在「有持仓」后挂——开盘前对昨日已持仓标的挂卖单可行 |
| **盘后不处理撤单** | 7-21 实测（1048577/1048578 收盘后 status=50 不撤） | 撤单动作必须在交易时段（9:30-15:00）内执行 |

---

## 3. 核心交易模型（四阶段时序）

```
┌─ T-1 日盘后（如 15:30 收盘数据齐全后）────────────────────────┐
│  ① 颈线法 scan_at 扫描 → 新买入信号                            │
│  ② 对已有持仓重算止损位（海龟 trailing 离散版：grace 天后每日   │
│     收紧 step×ATR，floor 兜底）+ 定止盈位                      │
│  ③ 生成 T 日交易计划 JSON（买/卖/止盈/止损 + 价位 + 数量）      │
│  ④ 交易机器人钉钉推计划 → 人工确认（ask 审批闸）→ 落盘计划     │
└───────────────────────────────────────────────────────────────┘
          ↓（计划落盘：logs/trading_plan_<T日期>.json）

┌─ T 日开盘前（9:20-9:25）──────────────────────────────────────┐
│  ⑤ scheduler 读已确认计划：                                    │
│     - 新买单 → 限价买单（挂单价=颈线+1×ATR，待确认）           │
│     - 已有持仓止盈 → 高位限价卖单                              │
│     - 撤昨日未成交单（cancel_order，交易时段内）               │
│  ⑥ 白名单动态化：计划标的临时注入 risk_shield 白名单（当日有效）│
└───────────────────────────────────────────────────────────────┘

┌─ T 日盘中（9:30-15:00，每 5min）──────────────────────────────┐
│  ⑦ 止损监控：查持仓标的现价，跌破 T-1 晚定的止损价 → 发卖出单  │
│     （被动触发，不移动止损位，符合"盘中不调整"）                │
│  ⑧ 不做其他动作（不追涨/不调仓/不移动止盈止损位）              │
└───────────────────────────────────────────────────────────────┘

┌─ T 日盘后（15:30）────────────────────────────────────────────┐
│  ⑨ 对账：reconcile() 持仓数量 本地 vs 券商，偏差超阈值钉钉告警  │
│  ⑩ 重算次日（T+1）止损位（trailing 离散推进）→ 进入下一循环    │
│  ⑪ 交易机器人推当日成交复盘（挂单/成交/止损触发/期初期末资金）  │
└───────────────────────────────────────────────────────────────┘
```

---

## 4. 组件设计

### 4.1 交易日历 `trading/calendar.py`（新增）
- 基于 Tushare `trade_cal` 缓存 A 股交易日历（`is_trading_day(date)`）。
- scheduler 所有触发点先判交易日，节假日/周末跳过。
- 每年初自动刷新当年交易日历。

### 4.2 APScheduler 统一调度 `trading/engine.py`（新增，独立常驻进程）
- **独立进程** `python -m trading.engine`（不寄生 server uvicorn，避免 server 重启中断交易）。
- APScheduler 三个触发点（均先过 `is_trading_day`）：
  - `pre_open`：09:22，读已确认计划 → 挂单 + 撤昨日未成交单
  - `stop_loss_monitor`：工作日每 5min 触发，**函数内再判 intraday 交易时段（9:30-11:30 / 13:00-15:00）**，盘前/午休跳过（cron 表达不了分段，靠运行时判断）；跌破止损价发卖
  - `post_close`：15:30，对账 + 重算次日止损 + 推复盘
- `T-1 晚 15:30` 的信号扫描 + 计划生成作为第四个触发点 `eod_plan`（或并入 post_close）。
- 配置化：所有时点走 `.env`，APScheduler 改 cron 即生效（无需 schtasks 重建）。

### 4.3 Live 信号生成器 `trading/signal_runner.py`（新增）
- 把 `strategies/neckline_method.py::NecklineMethodStrategy.scan_at` 接到收盘数据。
- 输出标准化 `OrderRequest(symbol, qty, side, price)` 列表 + 止盈/止损价位。
- **与回测同源**：复用 `scan_at`，不另写信号逻辑（保证实盘=回测一致性）。

### 4.4 海龟止损迁出 `trading/stop_loss.py`（新增）
- 从 `scripts/neckline_backtest.py::simulate_exit` 抽出 grace/step/floor 逻辑。
- **离散化**：`compute_stop_price(position, holding_days, atr, close)` 纯函数——给定持仓、持有天数、ATR、当日收盘，返回次日止损价。
- T-1 晚盘后对每只持仓调此函数重算 T 日止损位，盘中监控用此固定价。
- 参数（param_iter 已搜过，待确认最优值）：grace 天数 / step×ATR 系数 / floor 比例。

### 4.5 盘后对账 `trading/reconcile_job.py`（新增）
- 调 `BaseExecutionGateway.reconcile()`（已就绪）做持仓数量对账。
- 偏差超阈值（待确认，如 ±1 股或 0.5%）→ 钉钉告警 + 写 `logs/reconcile_<date>.json`。
- 实盘成交 vs 理论挂单价滑点统计（读 `live_trades.csv` vs 计划 JSON）→ 策略微机器人播报。

### 4.6 安全熔断 `trading/circuit_breaker.py`（新增）
- **日亏上限**（待确认，如 -3%）：当日累计盈亏触及 → `emergency_halt` + 撤所有未终态订单 + 钉钉 ERROR 告警。
- **总仓位上限**（待确认，如 ≤80%）：超限拒新买单。
- **断线处理**：补全 `emergency_halt` 的「撤所有未终态订单」路径（一期探索发现现状只 lock_down 不撤单）。
- **单笔上限**：复用 risk_shield 的 `QMT_ORDER_MAX_AMOUNT` / `MAX_SHARES`。

### 4.7 白名单动态化 `trading/dynamic_whitelist.py`（新增）
- 计划确认后，把当日计划标的临时注入 risk_shield 白名单（内存，当日有效，盘后清）。
- `.env` 的静态 `QMT_SYMBOL_WHITELIST` 降级为兜底（人工标的），动态白名单优先。

---

## 5. 配置化（`.env` 新增）

```ini
# === 第二期 自动交易引擎 ===
# 总闸（影子模式 dry_run / 真单 live）
AUTO_TRADE_MODE=dry_run          # 上线前必须 dry_run 跑通 N 天

# scheduler 时点（APScheduler cron，改即生效）
ENGINE_PRE_OPEN_CRON=22 9 * * 1-5        # 09:22 开盘前挂单
ENGINE_STOPLOSS_CRON=*/5 9-14 * * 1-5     # 每5min止损监控(9:30-15:00近似)
ENGINE_POST_CLOSE_CRON=30 15 * * 1-5      # 15:30 盘后对账
ENGINE_EOD_PLAN_CRON=30 15 * * 1-5        # 15:30 盘后信号扫描(与对账合并或紧随)

# 交易参数（param_iter 基线推荐，待确认）
TRADE_POS_CAP=0.05              # 单标的仓位上限（param_iter 最优）
TRADE_MAX_TOTAL_EXPOSURE=0.80   # 总仓位上限
TRADE_BUY_PRICE_FORMULA=neckline_plus_1atr   # 买入挂单价算法
TRADE_STOPLOSS_GRACE_DAYS=3     # 海龟 grace（待确认 param_iter 最优）
TRADE_STOPLOSS_STEP_ATR=0.5     # 海龟 step×ATR（待确认）
TRADE_STOPLOSS_FLOOR=0.92       # 止损 floor（待确认）

# 熔断
CIRCUIT_DAILY_LOSS_LIMIT=-0.03  # 日亏 -3% 触发熔断（待确认）

# 计划落盘
TRADE_PLAN_DIR=logs/trading_plans
```

---

## 6. 错误处理（边界审查）

| 场景 | 处理 |
|---|---|
| 开盘前挂单时网关断线 | 重连（指数退避，已有）+ 超时未连 → 跳过该标的 + 钉钉告警，不裸发废单 |
| 柜台拒单（资金不足/涨跌停） | `on_order_error` 已捕获 → 记 REJECTED + 钉钉告警该标的失败 |
| 部分成交 | 状态机推进 PARTIAL_FILLED，未成交部分由止损/止盈监控自然覆盖或盘后清理 |
| 止损监控发单失败 | 重试 1 次 + 失败钉钉 ERROR（止损发不出 = 敞口风险，必须告警） |
| T-1 计划未人工确认 | T 日开盘前 scheduler 检测计划未确认 → 不挂单 + 钉钉提醒「计划待确认」 |
| scheduler 进程崩溃 | Windows 服务/schtasks 守护拉起 + 崩溃钉钉告警 |
| 对账偏差超阈值 | 钉钉告警 + 暂停次日新买单（保守）直至人工核查 |

---

## 7. 测试策略

- **影子模式（dry_run）必跑**：上线真单前，`AUTO_TRADE_MODE=dry_run` 跑满 N 个交易日（建议 ≥5 日），scheduler 跑全套流程但只记录计划 + 模拟成交（不调网关真单），产出「计划 vs 模拟成交 vs 实际行情」对比报告。
- **纯函数单测**：`compute_stop_price`（海龟 trailing 离散）、`scan_at → OrderRequest` 映射、`reconcile` 偏差判定、白名单动态注入/清除。
- **熔断单测**：日亏触及阈值触发 halt、断线撤单路径。
- **模拟盘小额头单**：影子模式通过后，单标的 100 股最小额真单验证全链路（复用一期验证过的通道）。
- **端到端**：一个完整交易日循环（eod_plan → pre_open → stop_loss_monitor → post_close）在模拟盘 dry_run 跑通。

---

## 8. 时间线（本周）

| 时间 | 交付 |
|---|---|
| **周二** | calendar + engine 骨架 + signal_runner + stop_loss 迁出 + 单测 |
| **周三** | reconcile_job + circuit_breaker + dynamic_whitelist + 计划确认钉钉交互；**影子模式 dry_run 开跑** |
| **周四** | dry_run 跑 ≥2 日，对比计划 vs 模拟成交，修偏差；后台 `/cockpit` 加「引擎状态 + 当日计划」小部件 |
| **周五** | dry_run 稳定后，单标的 100 股真单验证；放量至完整仓位（视 dry_run 结果） |

---

## 9. 风控红线（不可逾越）

1. **影子模式先行**：`AUTO_TRADE_MODE=dry_run` 未跑满 N 日 + 偏差可接受前，**绝不切 live**。
2. **T-1 确认闸**：计划未经人工确认，T 日不挂任何单。
3. **熔断兜底**：日亏上限 / 断线全撤 / 总仓位上限 三道闸，任一触发即 halt + 告警。
4. **止损必须有监控**：A 股无原生止损单，止损监控进程崩溃 = 敞口失控，必须守护 + 告警。
5. **实盘 vs 回测一致性**：signal_runner 复用 `scan_at`，止损参数与 param_iter 基线一致，偏差超阈值暂停。
6. **凭证/参数安全**：`AUTO_TRADE_MODE` / 熔断阈值改动需留痕（`logs/config_changes.jsonl`）。

---

## 10. param_iter 基线复用 + 待确认参数清单

### 已有基线（param_iter 已搜，可直接复用）
- 单标的仓位：`pos_cap=0.05`
- 颈线法股票池：创板/科创（freq_cap=150 口径）
- 海龟 trailing grace/step/floor 已纳入 param_iter 搜索（需取最优组合值）

### 待研究员确认（spec 标注，审阅时拍板）
- [ ] 买入挂单价算法：颈线+1×ATR？（或 param_iter 最优 entry）
- [ ] 海龟 grace 天数 / step×ATR 系数 / floor 比例 的最优组合
- [ ] 总仓位上限（0.80？）
- [ ] 日亏熔断阈值（-3%？）
- [ ] 止损监控频率（5min？）
- [ ] 影子模式天数 N（≥5？）
- [ ] T-1 信号扫描时点（15:30 收盘后？还是等数据落湖 16:00？）

---

## 11. 与第一期的衔接

- **钉钉确认闸**：复用第一期交易机器人（②），T-1 晚推计划 + 收人工确认。
- **后台看板**：`/cockpit` 加「引擎状态（dry_run/live）+ 当日计划 JSON + 影子模式对比」小部件。
- **每日复盘播报**：复用第一期交易 brief，第二期注入真实成交/止损触发数据。
- **通用机器人**：@查询引擎状态/计划/熔断，转发 Claude Code 大脑。
