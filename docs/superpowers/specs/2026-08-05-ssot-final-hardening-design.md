# 唯一真相源最终整改设计（ssot-final-hardening · Phase A/B/C）

- **日期**：2026-08-05
- **分支**：master @ 01735955（spec 阶段）
- **状态**：spec review 已过（2026-08-05 · 3 断点决策 + 3 阻塞遗漏已补）→ 进入 plan（A/B/C 三份）
- **关联**：
  - `docs/data-source-of-truth.md`（本 spec 完成后按 3.1 目标态修订）
  - `docs/superpowers/specs/2026-08-04-gateway-ssot-hardening-design.md`（W1-W6，本 spec 是其“清除镜像”收口）
  - `docs/superpowers/specs/2026-08-04-reliability-remediation-design.md`（A 系列数据观测治理，路径修复已在工作区）
  - commit `172c4c97`（W3 完整收口：export/review/aggregate 切 state_store.fill）
  - `scripts/backfill_live_trades_to_state_store.py`（历史 CSV 可信回填已执行，本 spec 的 Phase A 据此清理源）
- **范围**：Phase A 彻底移除 CSV / Phase B 其余文件镜像收敛 / Phase C 计划内容入 DB。只落 spec；实施按 phase 拆 plan。

---

## 1. 背景与现状

### 1.1 为什么还有 CSV（过渡态盘点）

W3 做的是“收敛”而非“清除”：消费端默认读 DB，但 CSV 仍以“审计旁路 + 一键回滚”名义存在。逐项盘点：

| # | 位置 | 现状 | 性质 |
|---|---|---|---|
| 1 | `trading_service.record_live_trade` / `LIVE_TRADE_LOG` | 引擎成交回报、submit_order 审计仍追加 `logs/live_trades.csv` | 写路径 |
| 2 | `aggregate_fills_by_symbol` / `export_trades` / `query_trades` | `LIVE_TRADE_READ_SOURCE=db` 默认，`=csv` 回退；DB 异常自动回退 CSV | 读回退 |
| 3 | `research/digest.load_live_fills` | 仍直接读 `logs/live_trades.csv` | 消费端未切 |
| 4 | `review_service` / `schemas/review.py` | 数据源已走 DB 导出，但文案/schema 仍提 CSV | 残留语义 |
| 5 | `scripts/migrate_live_trades_csv.py` / `backfill_live_trades_to_state_store.py` | 一次性迁移工具（后者已执行） | 一次性脚本 |
| 6 | 测试 | `tests/trading/test_live_trades_csv.py` 等仍以 CSV 写读为契约 | 测试契约 |
| 7 | `docs/data-source-of-truth.md` #3 | 仍把 CSV 列为“成交流水审计 SSoT” | 文档过期 |

### 1.2 其余文件/内存镜像盘点

| # | 镜像 | 位置 | 风险 |
|---|---|---|---|
| 1 | `logs/expired_positions.json` | `trading/engine.py:1371`（post_close 写、pre_open 读删） | 可重算却落盘；崩溃窗口残留/重复消费 |
| 2 | `_position_attribution`（进程内存） | `trading_service.py:48` | 重启丢失；get_positions 归因变 “—” |
| 3 | `logs/param_iter_state.json` 回退 | `broadcast/__main__.py:447-477` | DB 空时展示陈旧参数 |
| 4 | `logs/.last_<bot>_brief` | `broadcast/__main__.py` / `catchup.py` | 与 job_ledger 双幂等机制 |
| 5 | `daily_equity` 表 + position_book 读写函数 | `state_store.py:211-214` / `position_book.py:262` | W4 后无生产写入，读口已迁 account_daily |
| 6 | `logs/trading_plans/plan_<date>.json` | `trading_plan.py` | 计划内容源仍在 JSON（Phase C 迁 DB） |

### 1.3 根因归纳

1. **回退开关是“留后路”的债**：`LIVE_TRADE_READ_SOURCE` 让 CSV 永远有第二读路径，镜像无法真正废弃。
2. **消费端切换不彻底**：digest 直接读 CSV，属于 08-04 修复时漏网的读方。
3. **可重算状态落盘**：expired_positions/attribution 本可从 DB 派生，落盘即引入漂移面。
4. **无静态护栏**：没有“生产代码零 CSV 引用”的检查，回归无感。

### 1.4 与既有 spec 的关系

- 本 spec 是 `2026-08-04-gateway-ssot-hardening` 的 W3/W6 收口（W3 已默认切 DB，本 spec 负责删干净）。
- `2026-08-04-reliability-remediation-design.md` 的 A 系列路径修复（`_PROJECT_ROOT` 四级）在工作区未提交，实施 Phase A 时并入。
- 进程/网关/miniQMT 治理另立 spec（2026-08-05-process-gateway-final-design），本 spec 不重复。

---

## 2. 目标与非目标

### 目标

1. **SQLite（state_store）成为唯一写入口和唯一读入口**；CSV 只是“按需生成的导出产物”，磁盘不再维护 `live_trades.csv`。
2. 删除全部 `LIVE_TRADE_READ_SOURCE` 回退分支；DB 异常 = 显式日志 + 空/降级文案，绝不静默读镜像。
3. 其余文件/内存镜像全部收敛进 DB（expired/attribution/param_iter/brief 幂等/daily_equity/plan）。
4. 更新 `data-source-of-truth.md` 为目标态表格（3.1）。
5. 新增静态护栏测试：生产代码 `live_trades.csv` / `LIVE_TRADE_READ_SOURCE` 引用数为 0。

### 非目标

- 历史 CSV 行回填 SQLite（`backfill_live_trades_to_state_store.py` 已执行完成，Phase A 只归档）。
- 前端/播报排版改造、策略逻辑改动。
- 进程/网关/miniQMT 治理（另立 spec）。
- 回填历史 submit/BLOCKED 审计行（只保证审计职能平移后的新行落 DB）。

---

## 3. 目标态架构

### 3.1 数据域最终表（`data-source-of-truth.md` 修订目标）

| # | 数据域 | 唯一真相源 | 导出产物（可重建） |
|---|--------|-----------|-------------------|
| 1 | 成交流水 | `state_store.fill`（UNIQUE(order_id, traded_time)） | 导出接口按需生成 CSV |
| 2 | 订单/委托 | `state_store.order` | — |
| 3 | 持仓 | `state_store.position`（含归因列，Phase B2） | — |
| 4 | 交易生命周期 | `state_store.trade_event`（SIGNAL/CONFIRMED/VETOED/BLOCKED/CLOSED…） | — |
| 5 | 日权益 | `state_store.account_daily` | — |
| 6 | 交易计划 | `state_store.trade_event(SIGNAL).meta`（Phase C） | `plan_<date>.json` 按需导出 |
| 7 | 数据就绪 | `state_store.data_ready` + `job_ledger`（get_ready 单口） | — |
| 8 | 播报幂等 | `job_ledger`（brief_<bot> 行） | — |
| 9 | 参数迭代/实验 | `experiment.db` ACTIVE | — |

### 3.2 读写拓扑

```
引擎/服务 ──写──▶ state_store（唯一写入口）
                    │
                    ├─▶ 导出接口（CSV/JSON 按需生成，不落盘）
                    ├─▶ 播报/复盘/digest（唯一读入口）
                    └─▶ 巡检 audit_ssot.py（一致性校验）
```

---

## 4. Phase A：彻底移除 CSV

### A1 删除写路径

- `trading/engine.py:3198-3210` `_handle_order_update`：删除 `record_live_trade` 调用 + `from ...trading_service import record_live_trade` 延迟 import（fill 已由 `insert_fill` 落真相源）；direction=None 旁路不再写 CSV（保留告警 + trade_event 审计）。
- `trading_service.submit_order`（`trading_service.py:659-688`）：submit 审计**全部平移 trade_event**（断点-1 决策 · spec review 2026-08-05）：
  - BLOCKED → `trade_event(BLOCKED, meta=f"{stage}:{reason}")`
  - 真单成功 → `trade_event(ORDERED, meta=f"{gw.__class__.__name__}:{state}:{message}")`（spec 原例漏此行，补）
  - REJECTED/FAILED → `trade_event(REJECTED, meta=message)`；dry_run 成功 → `trade_event(DRY_RUN, meta=reason)`
  - 「崩溃后真实已成交但系统不知情」敞口黑洞（原 kind=submit 兜底，`trading_service.py:676-688`）：成交真相由 fill 表 `UNIQUE(order_id,traded_time)` 幂等 + QMT 重连 `query_trades` 自补承担；下单审计由上述 trade_event 承担。CSV 兜底删除不丢审计职能。
- 删除 `record_live_trade` / `LIVE_TRADE_LOG` / `LIVE_TRADE_COLUMNS`（`trading_service.py:37-44,212-247`）。

- **实施细化**（spec review 2026-08-05 核验发现 · 断点-1 落地依据）：
  - engine 自动下单（pre_open）**已写** `trade_event(ORDERED)` + `order(SUBMITTED)`（`engine.py:944-950`）+ REJECTED（`engine.py:934`）。submit_order 的真单成功审计（`:684`）对 engine 路径冗余，但 `trade_event` `UNIQUE(account_id,trade_id,action)` 幂等——双写第二次 `IntegrityError` 返 False 跳过（安全）。
  - **选「双写幂等」方案**（审计完整，贴合断点-1 决策）：submit_order 写 `trade_event(ORDERED/BLOCKED/REJECTED/DRY_RUN)`，engine 路径双写幂等跳过，server 手动下单路径首次写入（唯一审计）。
  - submit_order 签名无 account_id/trade_id → 在 trading_service.py 加 `_resolve_account_id()`（env QMT_ACCOUNT_ID，与 `engine._resolve_account_id:455` 同口径，本地实现避免循环 import）+ trade_id 构造 `f"{aid}_{order.symbol}_{clock.today()}"`。

验收：`rg "record_live_trade|LIVE_TRADE_LOG" trading presentation --glob '*.py'` 仅剩测试改造后 0 命中；BLOCKED/ORDERED/REJECTED/DRY_RUN 均可在 trade_event 查到；engine 路径双写 ORDERED 幂等（trade_event 仅 1 行）。

### A2 删除读回退

- `aggregate_fills_by_symbol` / `export_trades` / `query_trades`：删除 `LIVE_TRADE_READ_SOURCE` 分支与 CSV 读口，只读 `state_store.query_fills`。
- DB 异常策略：`logger.exception` + 返空/降级文案（不 raise、不回退）。

验收：`rg "LIVE_TRADE_READ_SOURCE" . --glob '*.py'` = 0；三个函数单测覆盖“DB 空→空结果”“DB 异常→降级不抛”。

### A3 消费端切 DB

- `research/digest.load_live_fills`：改读 `state_store.query_fills`（保留函数签名与去重逻辑，去掉 CSV 读取）。
- `review_service` / `schemas/review.py`：文案与 schema 注释改为“DB 生成 CSV 导出”。
- `position_book.reconcile_qty` docstring 同步。

验收：digest 单测用 tmp state_store 构造 fill，断言 `load_live_fills` 返回去重结果。

### A4 归档与清理

- `logs/live_trades.csv` → `logs/archive/live_trades.csv.final-20260805`（可恢复）。
- `scripts/migrate_live_trades_csv.py`、`scripts/backfill_live_trades_to_state_store.py` 移入 `scripts/archive/`（或标注“已完成一次性工具”）；`backfill_..._to_state_store.py:105` 默认读 `logs/live_trades.csv`，归档后需参数化 `--csv logs/archive/...` 或先归档脚本（避免归档后脚本失效）。
- **测试改造全清单**（阻塞级遗漏 · spec review 2026-08-05）：删 `record_live_trade` 函数后，所有 `patch("...trading_service.record_live_trade")` 会因 `create=False` 抛 `AttributeError`，必须同步改造：
  - 删除/改写：`tests/trading/test_live_trades_csv.py` → `test_fill_db_contract.py`；`tests/server/test_trading_trades.py`（整文件 `LIVE_TRADE_READ_SOURCE=csv` 分页/过滤契约 → DB 契约）；`tests/server/test_review_service_db.py`（patch `LIVE_TRADE_LOG` 常量改 DB 构造）；`tests/research/test_digest.py`（tmp CSV → tmp state_store.fill）。
  - 去 mock：`tests/test_trading_service.py:152,171,193,225,250`、`tests/test_trading_api.py:37,58`、`tests/trading/test_engine.py:1420,1435,1479`、`tests/trading/test_e2e_trading_flow.py:225`、`tests/e2e_long_cycle/conftest.py:29-60`（含大段注释）、`tests/e2e_long_cycle/test_probabilistic_broker.py:97,178`。

### A5 静态护栏

- 新增 `tests/test_ssot_static_guard.py`：
  - 生产代码（trading/presentation/broadcast/research）无 `live_trades.csv`、`LIVE_TRADE_READ_SOURCE`、`record_live_trade` 引用；
  - `logs/live_trades.csv` 不存在（或仅在 archive 目录）。

---

## 5. Phase B：其余文件镜像收敛

### B1 expired_positions.json

- 删除 `_EXPIRED_POSITIONS_PATH` / `_write_expired_positions` / `_load_expired_positions` / `_consume_expired_positions`（`engine.py:1371,1413,1425,1435`）；pre_open 直接调 `_scan_expired_positions`（从 `position.entry_date` + `max_holding` 现算）。
- **holding_days 基准日 = 上一交易日**（断点-2 决策 · spec review 2026-08-05）：pre_open 现算时基准日取 `clock` 的「上一交易日」（= 原 post_close 扫描的 `today_eq` 的口径），与原 post_close 扫描基准完全一致，**零策略语义漂移**。不取 pre_open 当日 today（避免多算 1 天提前平仓）。
- post_close 删除 `_scan_expired_positions` + `_write_expired_positions` 调用（`engine.py:1809-1815`）；超期检测的唯一触发点挪到 pre_open。
- 幂等保护保留：`EXPIRED_CLOSE` order 行 + `has_order` 防重挂（已有）。

验收：e2e 测试断言“超期持仓 → 次日 pre_open 平仓”不依赖文件；删除文件后流程不变；holding_days 基准日与改造前 post_close 口径逐日对齐（边界测试：刚满 max_holding 当日不平、第 max_holding+1 日平）。

### B2 持仓归因落 DB

- **事实纠正**（spec review 2026-08-05）：`trading_service.py:341,355,450` 注释证实 **fill 表不含 strategy/rationale 列**，原 spec「从 fill/order 重建」不可行；重建源唯一可行是 `trade_event(SIGNAL).meta`（已存在，`engine.py:639`）。
- **决策（断点-3）**：Phase B 只做「落 DB 列 + submit 时 DB upsert」，**不做重启重建**——接受存量持仓重启后归因丢失窗口（get_positions 归因显示「—」）；重建推迟到 Phase C（与计划消费端切换一并，从 `trade_event(SIGNAL).meta` 补重建）。
- `state_store.position` 增列 `strategy` / `entry_rationale`（`state_store.py` position 表 DDL + 迁移）；`record_position_attribution` / `clear_position_attribution`（`trading_service.py:197-209`）改为 DB upsert/delete；`get_positions`（`trading_service.py:179-192`）富化读 DB 列；删除 `_position_attribution` 内存字典（`trading_service.py:48`）。

验收：单测覆盖 upsert/clear/读富化；重启后**新开仓**归因不丢（submit 已落 DB）；存量持仓重启归因丢失窗口可接受（Phase C 补重建）。

### B3 param_iter_state.json 回退删除（含 4 个遗漏读口）

- `broadcast/__main__._fetch_strategy_snapshot:441-478`：删除 legacy JSON 回退分支，只认 `experiment.db ACTIVE`；无 ACTIVE → None（brief 显式“无在线实验”）。
- **遗漏读口全清**（阻塞级遗漏 · spec review 2026-08-05）：以下 4 个生产读口仍读 legacy JSON，必须同步切 `experiment.db ACTIVE`：
  - `backtest/weekly_replay.py:13,19,41,51`（周度回测冠军来源）
  - `discovery/cli.py:4,24,50`（oos 命令冠军）
  - `discovery/tools/param_iter.py:78,229`（双轨冠军治理）
  - `backtest/tools/kbkg_trailing_verify.py:43`（直接 `json.load`）
- 归档 `logs/param_iter_state.json` → `logs/archive/`。

验收：`rg "param_iter_state\.json" --glob '*.py'` 生产代码 0 命中（仅 archive/路径与测试）；4 个读口单测覆盖「无 ACTIVE → 降级 None/默认参数」。

### B4 播报幂等迁 job_ledger

- `last_brief_file` / `.last_<bot>_brief` 替换为 `job_ledger` 行（job_name=`brief_trading/data/strategy`，business_date=日期）；`--force` 语义保留。
- `catchup._brief_missed` 同步改读台账。

### B5 daily_equity 清理

- 删除 `position_book.snapshot_start_equity/get_start_equity` 与 `daily_equity` 表（迁移 SQL 归档）；熔断读口已迁 `account_daily`。

### B6 治理文档修订

- 按 3.1 重写 `docs/data-source-of-truth.md`；新增 `scripts/audit_ssot.py`（fill↔position↔account_daily↔trade_event 一致性 + 引擎进程数 + CSV 引用检查）。

---

## 6. Phase C：计划内容入 DB

### C1 内容源

- `trade_event(SIGNAL).meta` 已是完整订单参数快照（symbol/qty/side/price/stop/tp/neckline/atr/rr/experiment），升格为计划内容唯一源。

### C2 消费端改读 DB（含 5 个遗漏消费方 + 归因重建）

- `pre_open` / `veto_plan` / `_stoploss` / `review_report` 改读 `trade_event.meta`（按 trade_id = account_symbol_date 索引）；`VETOED/CONFIRMED` 判定已双写（W2）。
- **遗漏消费方全清**（阻塞级遗漏 · spec review 2026-08-05）：以下 5 个计划消费方仍读 `plan_<date>.json`，必须同步切 `trade_event(SIGNAL).meta`：
  - `trading/engine.py:319-356` `_scan_recent_signals`（扫 `plan_*.json` 是信号识别旁路）
  - `broadcast/__main__.py:383-414`（scan_count 读计划）+ `broadcast/brief_strategy.py:7`
  - `experiment/cli.py:32-45`（report 按 experiment_id 聚合扫计划）
  - `trading/tools/trigger_eod_once.py:46-51` + `trading/tools/smoke_trading_engine.py:94`
- **归因重建**（断点-3 后续，本 Phase 补）：启动补扫一次，从 `trade_event(SIGNAL).meta` 按 symbol 索引最近 SIGNAL，回填 position.strategy/entry_rationale（弥补 Phase B 的重启丢失窗口）。
- `plan_<date>.json` 降级为“导出产物”：钉钉推送/人工查看按需从 DB 生成；删除 `save_plan/load_plan/confirm_plan` 写路径（原子写问题随之消失）。

### C3 兼容窗口

- 过渡期保留 `load_plan` 读口：DB 无 meta 时回读旧 JSON（只读，不写）；一个发布周期后删除。

---

## 7. 实施顺序与验收

| Phase | 内容 | 依赖 | 验收 |
|---|---|---|---|
| A | 删 CSV 写/读回退/消费端切 DB/归档/护栏 | 无（工作区 data_service 路径修复并入） | `pytest tests/ -q` 全绿；rg 护栏 0；导出接口输出与旧 CSV 同构 |
| B | 文件镜像收敛 | A（digest 已切 DB 后 B3 更干净，不阻塞） | 重启后归因/幂等/超期平仓不丢；audit_ssot 全绿 |
| C | 计划入 DB | A+B | pre_open/veto/复盘全读 DB；旧 JSON 只读兼容；删除写路径后全量测试绿 |

每 Phase 独立提交；提交前跑相关 pytest，Phase 完成跑 `pytest tests/ -q` + `scripts/audit_ssot.py`。

---

## 8. 风险与回滚

| 风险 | 缓解 |
|---|---|
| 删除 CSV 后 submit/BLOCKED 审计职能丢失 | A1 平移为 trade_event(BLOCKED)/order REJECTED，先落 DB 后删 CSV |
| Layer6 复盘依赖 CSV 格式 | 导出接口从 DB 生成同构 CSV，digest 改 DB 后格式兼容 |
| plan 迁 DB 影响 pre_open 挂单 | Phase C 保留 load_plan 只读兼容窗口一个发布周期 |
| DB 异常导致播报/导出空 | 显式日志 + 空结果，不回退镜像（观测层纪律）；巡检脚本告警 |

回滚策略：每 Phase 打 tag；A 完成后保留 `export_trades` 的 CSV 输出兼容层（数据源 DB），不恢复磁盘 CSV。

---

## 9. 附录：问题总账

| # | 项 | 证据 | 方案 | 测试 |
|---|---|---|---|---|
| 1 | CSV 写路径 | engine.py:3202 / trading_service.py:212,662-684 | A1 | trade_event(BLOCKED) 单测 |
| 2 | CSV 读回退 | trading_service.py:265,325,421 | A2 | 三函数 DB-only 单测 |
| 3 | digest 读 CSV | research/digest.py:189 | A3 | query_fills 去重单测 |
| 4 | 一次性脚本/文档 | scripts/*.py / data-source-of-truth.md | A4/B6 | 静态护栏 |
| 5 | expired JSON | engine.py:1371-1415 | B1 | 无文件 e2e |
| 6 | 归因内存 | trading_service.py:48 | B2 | 重启重建单测 |
| 7 | param_iter 回退 | broadcast/__main__.py:447 | B3 | 无回退单测 |
| 8 | .last_brief | broadcast/__main__.py:49-99 | B4 | job_ledger 幂等单测 |
| 9 | daily_equity | state_store.py:211-214 | B5 | 熔断读口 account_daily 单测 |
| 10 | plan JSON | trading_plan.py:41-95 | C1-C3 | pre_open 读 meta 单测 |
