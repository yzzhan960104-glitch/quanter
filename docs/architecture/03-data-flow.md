> 最近复核：2026-08-14 · 维护者：glm-5.3-session ·
> 权威归宿：**核心数据路径**（单一归宿）。外部边界见 [#1](01-system-context.md)；真相源规则见 [data-source-of-truth](../data-source-of-truth.md) 与 [#7](07-ssot-data-layer.md)；策略算法见 [neckline-method](../neckline-method.md)。

# #3 核心数据流

一条成交从「数据入库」到「落账持仓」的完整路径：**采集 → lake → 信号 → 计划 → 订单 → 成交 → 对账 → 持仓**。两视角：
- **时序图**——单交易日内各阶段的触发与写库顺序；
- **结构图**——数据形态与持久化归宿（每节点链 #7 真相源，不重抄）。

## 时序：单交易日生命周期（T 日）

```mermaid
sequenceDiagram
  autonumber
  participant SCH as schtasks/cron
  participant EOD as eod (T-1 盘后)
  participant PO as pre_open (T 09:22-10:00)
  participant ENG as engine
  participant STRAT as strategies/neckline
  participant BRK as broker/qmt
  participant QMT as miniQMT
  participant SS as state_store
  participant PC as post_close (T 盘后)
  participant BR as broadcast

  SCH->>EOD: 触发 eod（计算 next_trading_day）
  EOD->>SS: data_ready 校验（gate 阻断则跳过次日 pre_open）
  Note over EOD,SS: 2026-08-14 订正：现行代码 eod/pipeline **不写** account_daily<br/>（全库唯一写入方 snapshot_start_equity 在 pre_open，见下）
  SCH->>PO: 次日 09:22 触发 pre_open
  PO->>SS: account_daily.start_total_asset（query_asset 精确抓取；<br/>失败时 T-1 close 兜底回填——DG-G3）
  PO->>ENG: _health_guard gate 通过 → scan
  ENG->>STRAT: detect_signal(lake.loc[:T-1])  %% 严格无前视
  STRAT-->>ENG: list[Signal]（frozen dataclass）
  ENG->>SS: trade_event(SIGNAL).meta（计划入库）+ save_plan plan_<date>.json（过渡期镜像）
  ENG->>BRK: submit_order(signal)
  BRK->>QMT: 委托（seq-str order_id）
  BRK->>SS: trade_event(ORDERED / SUBMITTED / REJECTED / DRY_RUN)
  QMT-->>BRK: 成交主推 on_stock_trade（subscribe 成功时）
  BRK->>SS: fill（UNIQUE(order_id, traded_time) 幂等）
  SCH->>PC: 盘后触发 post_close
  PC->>BRK: query_position（QMT 真实持仓，外部参照）
  PC->>PC: reconcile（fill 累加 ± vs state_store.position，容差 1e-6）
  PC->>SS: position（含 strategy + entry_rationale 归因）+ account_daily.close_total_asset
  PC->>BR: brief / discovery 推送（job_ledger 幂等）
```

> 注：subscribe 失败时无主推，订单状态退化为例：`_sync_orders_if_stale` 在触发点前主动 `query_orders` 兜底（[T4](../../plans/wayfinder/T4.md)：不引入后台轮询，惰性补全）。

## 结构：数据路径与真相源归宿

```mermaid
flowchart LR
  Tushare[("☁ Tushare")] --> Sync["data/tushare_sync<br/>(采集·全量/增量)"]
  Sync --> Lake[("data_lake/*.parquet<br/>a_shares_daily 等")]
  Lake --> Reader["data/lake_reader<br/>DataLakeReader"]
  Reader --> Scan["strategies/neckline<br/>detect_signal / scan_symbol"]
  Scan --> Signal["list[Signal]<br/>(内存态·不入库)"]
  Signal --> Plan["trading_plan<br/>plan_<date>.json<br/>Phase C → trade_event(SIGNAL).meta"]
  Plan --> Submit["engine.submit_order"]
  Submit --> Broker["broker/qmt<br/>QmtExecutionGateway"]
  Broker --> QMT["miniQMT"]
  Broker -.成交主推.-> Fill["state_store.fill"]
  Fill --> Recon["post_close<br/>reconcile"]
  QMT -.query_position<br/>真实持仓(外部参照).-> Recon
  Recon --> Position["state_store.position<br/>(strategy + entry_rationale)"]
  Signal -.生命周期写.-> TE["state_store.trade_event<br/>SIGNAL→CONFIRMED/VETOED→ORDERED→FILLED→CLOSED"]
  Submit -.订单状态.-> Order["state_store.order"]
```

## 阶段—产物—真相源（链 [data-source-of-truth](../data-source-of-truth.md) / [#7](07-ssot-data-layer.md)，不重抄）

| 阶段 | 产物 | 真相源（域） | 关键不变量 |
|---|---|---|---|
| 采集 | `data_lake/*.parquet` | 原始落盘（非 SSoT 域） | 行数骤降即异常（[T12](../../plans/wayfinder/T12.md) 教训） |
| 信号 | `list[Signal]` | 内存态 | 严格无前视 `df.loc[:T]`；持久化经 trade_event |
| 计划 | `plan_<date>.json` / `trade_event(SIGNAL).meta` | **第 6 域**（Phase C 升格中，JSON 当前仍生产写入口） | `UNIQUE(account_id,trade_id,action)` 幂等 |
| 订单 | `state_store.order` | **第 2 域** | order_id PK；状态机 SUBMITTED/FILLED/CANCELED |
| 委托生命周期 | `state_store.trade_event` | **第 4 域** | 动作枚举 SIGNAL/CONFIRMED/VETOED/BLOCKED/ORDERED/SUBMITTED/REJECTED/DRY_RUN/FILLED/CLOSED |
| 成交 | `state_store.fill` | **第 1 域** | `UNIQUE(order_id, traded_time)` 幂等——双写防重 |
| 持仓 | `state_store.position` | **第 3 域** | 含归因列 `strategy`+`entry_rationale`；QMT 持仓是外部对账参照**非内部真相** |
| 日权益/熔断 | `state_store.account_daily` | **第 5 域** | PK(account_id,date)；start=熔断基线 / close=日终闭合 |
| 数据就绪 | `state_store.data_ready` + `job_ledger` | **第 7 域** | `get_ready` 单口读；PK(date,dataset) |
| 播报幂等 | `job_ledger` | **第 8 域** | brief_<bot> 行 begin/finish 成对，独立库 |

## 关键风险点（详见 [#6](06-tech-debt.md) · 2026-08-14 复核后口径）

- **~~start_total_asset 漏采 → 熔断基线裸奔~~ ✅ 已治（DG-G3 · `8d4ef714`）**：判定层 fail-closed（基线 None → live `_CriticalHalt` / dry 停手）+ pre_open T-1 close 兜底回填（`phases/pre_open.py:363-404`）。**残留（CR-4）**：curr_equity 缺失方向仍 fail-open 静默跳过；收盘快照失败会静默掏空次日 T-1 兜底。另注意 **熔断判定点 = 15:30 post_close（盘后）**，盘中组合级回撤无实时闸（CR-3）。
- **湖历史缺口（CR-6）**：scan 发现 15701 段漏采（390 标的），补采回路熔断停摆（连续 7 败）+ 配额 50/轮——T13-B 闭环运行态停摆，回测语料可信度打折。
- **撤单主推延迟**：QMT CANCELLED 主推延迟 1-2s，须轮询确认（非数据流 bug，业务机制）。
- **第 6 域过渡态**：✅ Phase C 已全治理（2026-08-12）——`save_plan`/`confirm_plan` 已删，JSON 读侧 fallback 已关，本行保留作历史注记。
