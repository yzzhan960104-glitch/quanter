# Quanter —— 颈线法量化研究平台

## 1. 项目定位

Quanter 是一套面向 **A 股** 的量化研究平台：以**颈线法形态学（纯多头）**为主策略（当前唯一活跃策略），配套参数发现引擎、实验版本中心、数据中心、实盘自动交易引擎与远程协同：

- **主策略 · 颈线法**：颈线聚集带定位 + 压制时长验证 + 挂单回踩进场 + 分级止盈 + 可选海龟 trailing（`strategies/neckline/`）。策略本体与回测/执行解耦，经 `Strategy` Protocol 注入。
- **参数发现引擎 · discovery**：Plan 1-4 闭环（L0-L5 可信度），快照冻结 → 2025/2026 holdout 嵌套 OOS → 分层裁判 → Sobol/TPE 搜索 → 帕累托前沿 → daemon 生产入口 → 冠军 publish 至 experiment。已收编进引擎 lifespan cron 02:00 + 启动补跑。CLI `python -m discovery {oos,verify,daemon,publish}`。
- **实验版本中心 · experiment**：实盘下单的策略版本配置中心，`resolve_active()` 给 scan 发放当前生效的 `(strategy_name, params, weight)` 列表，支持版本切换 + 审计日志 + 权重校验。
- **回测引擎 · backtest**：`replay` 策略中立回测器 + 异步任务队列（worker/scheduler/tasks_db）+ 参数优化（`optimize/training_*`）+ 回测撮合模拟器（MockBroker）。**单向依赖铁律**：只依赖 `trading.compute`（离场纯函数）+ `strategies` + `data`，严禁触碰 `trading.engine`/`broker`（回测求变、交易求稳，分离防污染）。
- **实盘自动交易引擎 · trading（二期主线）**：单进程 `python -m trading` → uvicorn lifespan 装配 TradingEngine（APScheduler 六 job），事件链驱动盘后 pipeline、三段 gate 的 pre_open、30s 盘中巡检、盘后对账/熔断，C-8 启动补跑保证生产机不 7x24 时的最终一致性。
- **数据中心**：Tushare 通用同步器（20+ 数据集，配置驱动），AKShare / JQData 辅助。
- **后端引擎**：FastAPI（异步）+ 纯 Python 量化内核（Pandas/NumPy 显式向量化，拒绝黑盒）。
- **前端交互**：Vue 3 + Vite + ECharts，6 视图。
- **远程协同**：钉钉机器人（push 播报 3 + connect 对话/人审 5，dws 接入）。
- **测试体系**：常规回归 + `slow`（发现引擎端到端）+ `e2e_long`（C1-C7 长周期时序回放，23 日 full_run）。

设计哲学遵循「**显式实现、拒绝黑盒**」：核心指标（形态识别、盈亏比、ATR、筹码分布等）均以平铺直叙的数学运算实现；策略、撮合、状态机均配像素级中文注释。

> **架构演进注**：本平台早期以 `caisen/` 门面包组织（见 `2026-07-15-backend-layering-refactor-design.md`），后经 step4 执行链重组、Layer2 三层定型、T7 减法、U5 策略统一，到 2026-08 C5-C8 收编为**单进程模型**：`python -m trading` = uvicorn server + TradingEngine + broadcast connect + discovery cron + 启动补跑，全部在 lifespan 内装配。README 反映 `2026-08-02 C-8` 之后的真实状态。

---

## 2. 后端分层架构（四层 + 发现/回测两条副线）

接口层经应用服务编排各域；模型/执行/数据层单向依赖；发现与回测为两条策略副线。

```
quanter/
├─ 接口层 presentation/         web/+server/ 收编（README §2 语义落地）
│  ├─ web/                      前端 6 视图(CaisenScreen/ParamLab/Dashboard/LiveCockpit/DataLake/Review)
│  └─ server/                   FastAPI 应用
│     ├─ api/v1/                HTTP 路由(caisen·data·macro·review·trading·training·logs + sse)
│     ├─ services/              应用服务(编排用例,聚合各域)
│     ├─ schemas/               请求/响应 DTO
│     ├─ http/                  HTTP 运行时基建(auth/config/_responses)
│     └─ main.py                app 装配 + lifespan（引擎/discovery/connect/补跑收编点）
│
├─ 执行编排层 trading/          实盘执行引擎
│  ├─ engine.py                 TradingEngine：APScheduler 六 job + 网关自愈 + 熔断
│  ├─ compute/                  纯函数（plan/risk/breaker/stop/reconcile/types）
│  ├─ orchestrate/pipeline.py   事件链：采集→校验→data_ready→eod→brief（C-2/C-8）
│  ├─ catchup.py                启动补跑编排（C-8：pipeline/brief/pre_open 三腿）
│  ├─ job_ledger.py             job 运行台账（running/done/skipped/failed，跨重启）
│  ├─ state_store.py            统一交易状态库（6 表：account/trade_event/order/fill/position/account_daily）
│  ├─ position_book.py          本地持仓账本
│  ├─ calendar.py / clock.py    交易日历 + 单一时间源（C-6）
│  └─ qmt_gateway.py / qmt_market_data.py / trading_plan.py / reconcile_job.py ...
├─ 网关层 broker/               券商网关抽象(base/mock/qmt/qmt_quote)
├─ 策略层 strategies/           纯叶子(base/registry/signal + neckline/{method_v0,backtest,execution,schema,strategy})
├─ 回测副线 backtest/           策略中立回测器(replay/worker/scheduler/optimize/mock_broker)
├─ 发现副线 discovery/          参数发现引擎(Plan 1-4 · L0-L5 闭环)
├─ 实验中心 experiment/         实盘版本配置中心(models/resolver/store)
├─ 数据层
│  ├─ data/                     取数(clients/fetcher/cleaner/lake_reader/integrity/freshness)
│  ├─ data_lake/                parquet 存储(只放数据,禁放 .py,不入库)
│  └─ config/                   按层拆配置(8 子文件包)
└─ 横切
   ├─ infra/                    通知(notifier)+ LLM(llm/glm)
   ├─ broadcast/                机器人总管(push 3 + connect 5, brief/push/connect_manager/name_resolver)
   └─ ops/                      运维(data_pipeline/brief_all/manage_ops_schtasks/run_checks)
```

**依赖铁律（实证）**：

- `strategies / infra / experiment / config` 为**纯叶子**（零下游扇出），是健康的底层/横切。
- `backtest → strategies / trading.compute / data`（单向，不碰 broker/engine）——已实证合规。
- `discovery → strategies / experiment / infra`（发现链自洽内聚）。
- `trading → broker`（执行层调网关，正向）；`broker → trading.compute.types`（疑似反向，Layer2 follow-up #4c 类型迁移遗留，待收尾）。
- `trading → presentation.server.services`（5 处，执行层倒挂接口层）；`trading/protocols.py` 定义了 `ExecutionExecutor` Protocol 拟反转此依赖，但当前为孤儿契约、未接通——已记入架构债。
- `presentation.server` 经 services 扇出 7 个域。

> 详细跨包依赖矩阵与层次违规清单见 `docs/superpowers/specs/2026-07-22-layer2-decoupling-design.md`。

---

## 3. 当前策略详解：颈线法（唯一活跃策略）

### 3.1 策略全貌

颈线法是多空转折的纯多头形态策略，全流程分四段：

1. **识别（T 日盘后 `_eod`）**：在 `window`（当前 80）日窗口内找 ≥`min_touches` 个局部高点聚成**颈线**；要求 ≥`min_suppression` 比例的收盘价被压制在颈线下方；窗口内存在双底（≥`min_bottoms`）；当日放量突破（≥`breakout_vol_mult` × 近 5 日均量）才产信号；同时做 **cancel_on close 预判**（识别期就用收盘价挡住次日大概率会触发撤单的废信号）与**当日突破过滤**。形态深度受 `max_h_atr`（H/ATR）上限约束（防暴跌反弹形态），并校验结构盈亏比 ≥`min_rr`。
2. **挂单（T+1 日 09:22 pre_open）**：按计划在 `buy_limit = 颈线 + buy_limit_atr_mult × ATR`（当前 0.5ATR，颈线上方挂低价单等回踩）挂买单，有效期 `max_wait`（当前 8）个交易日；pending 期若盘中 high ≥ `cancel_on = 颈线 + cancel_thresh_mult × H`（当前 2H）则撤单防追高。
3. **持有（盘中 30s 巡检）**：成交后持有至多 `max_holding`（当前 20）个交易日；止损 = 颈线 − `stop_atr_mult × ATR`（当前 1ATR），可选海龟风格时间驱动 trailing（`trailing_grace` 宽限 → `trailing_step` 每日收紧 → `trailing_floor` 卡底；当前全为 0 = 退化为固定止损）。
4. **离场**：分级止盈——TP1 = 颈线 + `tp1_h_mult × H`（当前 1H，卖出 `tp1_portion` = 30% 的 lot1），TP2 = 颈线 + `tp_h_mult × H`（当前 2.5H，lot1+lot2 全平）；止损/超时全平。回测与实盘共用 `decide_exit` 纯函数（`strategies/neckline/execution.py`），杜绝决策分叉。

### 3.2 当前生效参数（21 维 · 唯一 ACTIVE 实验）

来源：`experiment/experiments.db` 的 ACTIVE 版本 `neckline_disc_20260725_25c602`（discovery 冠军，2026-07-27 激活，weight=1.0，`resolve_active()` 实时发放）。默认值 = `method_v0.DEFAULTS` + `backtest.EXEC_DEFAULTS`。

| 层 | 参数 | 默认 | 当前生效 | 含义 |
|---|---|---:|---:|---|
| 识别 | window | 60 | **80** | 颈线识别窗口（近 N 日） |
| 识别 | min_touches | 2 | 2 | 颈线由 ≥N 个顶部聚集连成 |
| 识别 | min_suppression | 0.6 | 0.6 | 压制时长下限（close<颈线比例） |
| 识别 | local_extrema_window | 3 | **5** | 局部极值左右窗 |
| 识别 | min_bottoms | 2 | **3** | 至少双底（含窗口最低点） |
| 识别 | breakout_vol_mult | 1.5 | **1.0** | 突破放量倍数（vs 近5日均量） |
| 识别 | min_rr | 1.5 | **2.0** | 盈亏比下限（结构/实际口径校验） |
| 识别 | max_h_atr | 4.0 | **5.0** | 形态深度上限 H/ATR（防暴跌反弹） |
| 识别 | stop_atr_mult | 1.0 | 1.0 | 止损 ATR 倍数（止损=颈线−N×ATR） |
| 识别 | tp_h_mult | 2.0 | **2.5** | 止盈2 H 倍数（TP2=颈线+N×H） |
| 识别 | decay_tau | None | null | 颈线聚集时间衰减（None=等权） |
| 执行 | max_holding | 15 | **20** | 成交后超时持仓日 |
| 执行 | max_wait | 5 | **8** | 挂单等待回踩成交有效期 |
| 执行 | cooldown | 5 | **8** | 信号去重冷却（相邻信号合并） |
| 执行 | buy_limit_atr_mult | 1.0 | **0.5** | 挂单价 = 颈线 + N×ATR |
| 执行 | tp1_h_mult | 1.0 | 1.0 | 止盈1 = 颈线 + N×H（第一波减仓） |
| 执行 | tp1_portion | 0.5 | **0.3** | 止盈1 减仓比例（lot1 占比） |
| 执行 | cancel_thresh_mult | 1.0 | **2.0** | 撤单阈值 = 颈线 + N×H（None=不撤放飞） |
| trailing | trailing_grace | 0 | 0 | 宽限天数 b（前 b 天不收紧；0=无宽限） |
| trailing | trailing_step | 0.0 | 0.0 | 收紧速度 a（ATR/日；0=固定止损） |
| trailing | trailing_floor | 0.5 | **0.0** | 收紧上限（最低 ATR 倍数） |

> 交易成本口径（回测/实盘同源）：佣金万三（双边）+ 卖出印花 0.05% + 过户 0.001%，单笔佣金 min5。

### 3.3 参数治理链路

`discovery daemon`（跨夜搜索收敛）→ `publish` 产出 experiment **DRAFT** → 人审 promote → **ACTIVE + weight** → `resolve_active()` 实时发放给 `_eod`（scan_live 装配）→ 信号带 `experiment_id` 归因 → `pre_open` 计划确认（人审闸）后才挂单。切换 live 有 ≥5 天（`TRADE_SHADOW_MIN_DAYS`）影子期硬闸（`check_shadow_gate`，fail-closed）。

策略方法论权威参考：[`docs/neckline-method.md`](docs/neckline-method.md)（颈线法完整技术文档）；[`docs/caisen-methodology-summary.md`](docs/caisen-methodology-summary.md)（历史方法论沉淀）。

---

## 4. 启动与运行生命周期

### 4.1 进程模型与启动入口

**单进程模型（C-5/C-7 收编）**：`python -m trading`（即 `scripts/run_trading_engine.bat` / `scripts/start_server.bat` 内容）只做两件事——加载 `.env` + 起 uvicorn `presentation.server.main:app`。TradingEngine、broadcast connect、discovery cron、启动补跑全部在 **uvicorn lifespan** 内装配，消除历史双进程抢 QMT session 的根因（端口 8000 天然单例，第二实例 bind 失败即退出）。

```bash
# 手动/PM2/schtasks 通用（Windows 下建议走 bat，含 chcp 65001 + PYTHONUTF8 三件套）
.venv310\Scripts\python.exe -m trading
# 开机自启（schtasks ONSTART，session 0 后台，不依赖登录）
python -m ops.manage_ops_schtasks --register-server
```

`AUTO_TRADE_MODE=dry_run`（影子模式，只记账不真单）时 uvicorn 开 reload 方便开发；`live` 模式强制 reload=False（防 reloader 子进程抢 session）。启动日志首行即 **banner**：session/account/userdata/mode/confirm + 日期口径（eod=next_trading_day, pre_open=today），一眼发现 .env 漂移。

### 4.2 启动序列（lifespan startup）

按顺序执行，每步独立 try/except 软降级（单步失败不阻断 server 起）：

1. 装配钉钉/企微/Telegram 通知通道（`build_default_manager`）；
2. 按 `LAKE_CONFIG["lakes"]` 加载多湖 parquet（缺失离线降级）；
3. 异步回测调度器（ProcessPoolExecutor concurrency=1 + ReplayScheduler）；
4. 训练 loop 编排器 + 报告 notifier（`reset_interrupted` 清残留）；
5. 加载 symbol→企业名映射；
6. 后台线程扫 stale/missing 数据集并触发补同步；
7. 三路日志装配（本地文件 + 前端 SSE 流 + 控制台）；
8. **TradingEngine 装配**：banner → `TradingEngine()` → `bootstrap()`（网关 connect + 成交回报回调注册 + position_book/state_store 建表迁移）→ `check_shadow_gate()`（影子期不足则不 start scheduler，server 照常起）→ `eng.start()`；
9. **broadcast connect 5 bot** 起常驻（cli/trading_q/data_q/strategy_q/review）；
10. **discovery cron 每小时+5 分（24h 低功率模式）** 注册进 engine.sched（DETACHED subprocess 跑 daemon）；
11. **discovery 启动补跑**：检测跨过昨晚 02:00 → 异步补跑（轮次/seed 幂等去重）；
12. **C-8 全 job 启动补跑**：`asyncio.create_task(run_startup_catchup(engine))`（见 4.4）。

### 4.3 常驻调度与日生命周期

TradingEngine 装配 APScheduler（`max_instances=1` + `misfire_grace_time=300` + `coalesce=True` 防堆积/重叠）：

| 触发点 | 时间 | 职责 |
|---|---|---|
| pipeline_then_eod | 18:00 周一-五（`ENGINE_PIPELINE_CRON`） | 盘后事件链：采集子进程→等完成→按策略声明校验数据→落 data_ready→`_eod` 扫 T 日信号产 T+1 计划→brief 三播报 |
| pre_open | 09:22 周一-五（`ENGINE_PRE_OPEN_CRON`） | 四段 gate（计划确认→网关健康→数据就绪→**regime** A1）→撤昨日未成交单→超期平仓现算→抓日内熔断基线→注入动态白名单→逐单挂单 |
| stop_loss | 每 30s（`ENGINE_STOPLOSS_INTERVAL_SECONDS`） | 盘中巡检：持仓 `decide_exit` 止损/止盈补挂/超时平仓 + pending 期 cancel_on 撤单（非盘中时段自动 no-op） |
| post_close | 15:30 周一-五（`ENGINE_POST_CLOSE_CRON`） | 持仓对账→成交流水兜底纠正→日内 -3% 熔断→trailing 止损推进→超期平仓→清动态白名单 |
| _health_guard | 每 60s | 网关健康自愈：断线探测→退避重连（`_guard_skip_rounds` 防刷柜台） |
| discovery_daemon | 每小时 :05（lifespan 注册，24h 低功率模式） | 参数发现 daemon 搜索（DETACHED 子进程，收敛/轮次幂等） |

典型交易日时序：

```
02:00  discovery daemon（参数搜索，轮次幂等）
09:22  pre_open（确认闸→撤昨日单→熔断基线→挂当日单；错过则 C-8 窗口内补跑）
09:30-11:30 / 13:00-15:00  stop_loss 30s 巡检（止损/TP 补挂/pending 撤单）
15:30  post_close（对账+兜底+熔断+trailing+超期平仓+清白名单）
18:00  pipeline 事件链（采集→校验→data_ready→eod 扫信号→brief 播报）
盘后   研究员在 /review 或钉钉人审确认 T+1 计划（confirmed=True）
```

关键语义：`_eod` 扫 **T 日** 盘后突破、产 **T+1** 计划（落盘 key = `next_trading_day`），pre_open 读 **today** 的计划——口径全链对齐（C-6 单一时间源 `clock.now/today/trading_day`，杜绝 eod/pre_open key 错位）。

### 4.4 启动补跑（C-8 · 生产机不 7x24 的最终一致性）

**job 运行台账**（`logs/trading_job_run.db`，env `TRADING_JOB_LEDGER_DB` 覆盖）：以 `(job_name, business_date)` 为键记录 `running/done/skipped/failed`，cron 与补跑共用（先查后写，谁先完成谁生效）；启动时 `reset_stale_running()` 把崩溃残留 running 置 failed（防永久阻塞）。

启动补跑编排（`trading/catchup.py`，仅 lifespan startup 一次）：

- **pipeline 腿**：`D = expected_latest_trade_day(now)`（最近已收盘交易日），D 未 done 且（D < 今天 或 now ≥ 18:00）→ 补 采集→校验→data_ready→eod→brief；`D == 今天 且 now < 18:00` 不补（今晚 cron 正常处理）。
- **eod 裁剪（政策 A）**：`plan_date = next_trading_day(D)` 已过 pre_open 窗口 → `run_eod=False` 只补数据+brief，不为过期交易日产废计划。
- **brief 腿**：pipeline done 但 `.last_<bot>_brief` 缺失/陈旧 → 补播一次（幂等文件去重）。
- **pre_open 腿**：今天是交易日且 now ∈ [09:22, `ENGINE_PRE_OPEN_CATCHUP_UNTIL` 缺省 10:00) 且未 done → 补挂单；窗口已过且未 done → CRITICAL 钉钉知会（政策 A 不静默）。
- **失败语义**：补跑异常 → 台账 failed + CRITICAL，不停调度、不阻断 uvicorn——留今晚 18:00 cron 自然收敛。

### 4.5 停机与恢复

Ctrl-C / schtasks 结束 → uvicorn lifespan shutdown：cancel 启动补跑任务 → 断开 QMT 网关（logout 释放会话）→ 树杀 connect bots（taskkill /F /T，防孤儿 Claude Code）→ 停 scheduler（不等待 pending job）→ 清理日志 handler。重启后 `reset_stale_running` + 台账守卫自动恢复到一致态（漏跑日由 C-8 补，不逐日补历史）。

---

## 5. 最新功能演进（2026-07 下旬 → 08-02）

### 5.0 2026-08-02 · 回测模块整改（评审 P0-P2）

- **资金/风险模型统一（P0-1）**：`backtest.models.PositionModel` 成为回测净值单源——默认
  `capital × pos_cap(5%)` 单笔仓位、最大并发 6 仓、现金约束、可配滑点；旧 `RISK_FRAC=0.01`
  复利口径经 `PositionModel(risk_frac=0.01)` 显式兼容。资金假设随 `report.metadata.position_model`
  冻结可审计。
- **计算单元 v2（P0-2）**：`compute_unit` 协议升 v2，新增 replay 模式（`--mode replay`），
  Mac 端可直接跑 `backtest.replay` 引擎、产出与 Win 主回测同口径的 ReplayReport 指标；
  engine_hash 指纹同步扩展到完整回测内核（P1-3）。
- **性能与正确性（P1-4/P1-6）**：replay 核心补齐直接单测；`detect_signal` 支持预计算 ATR
  按 T 截断，全市场滚动从 O(n²) 降为 O(n)；`ReplayReport.threshold_recommendation` 等
  caisen 残留字段清理（P1-5）。

### 5.1 2026-08-02 · C-8 启动补跑

- `trading/job_ledger.py` job 运行台账（sqlite 状态机 running/done/skipped/failed + 启动重置）；
- 事件链日期参数化（`pipeline_then_eod(for_date, run_eod)` / `_eod(data_day, plan_date)`，默认路径零变化）；
- `trading/catchup.py` 启动补跑编排（pipeline/brief/pre_open 三腿 + 政策 A 裁剪）；
- lifespan 接线（`create_task` + shutdown cancel）+ 全量回归 1180 passed 基线。

### 5.2 2026-08-01 · 实盘主链路修复（live-mainchain-fixes #1-#10）

1. trade 分支先落账后挂止盈 + 错误分级 L1 + 全链路 e2e（含竞态）；
2. pre_open gate③ 改查 `expected_latest_trade_day` 命中 T 日 data_ready；
3. CSV 加 `kind` 列区分 submit/fill，post_close 只聚合真实成交；
4. 止盈改**差额补挂**（目标量 − 已挂量），提为模块级 `place_take_profit`；
5. 方向反查 DB `order.side` 优先 + `on_stock_order` 透出 order_type + async_response 回填 broker_oid；
6. 风控熔断加 `_risk_halted` 粘滞标志，网关入口统一 `is_blocked`；
7. 超期平仓 DB 幂等防重（EXPIRED_CLOSE）；
8. 熔断撤单计数区分 failed，cancelled 只计成功发出；
9. QMT 状态 51（已报待撤）保守映射 SUBMITTED，等真终态推进；
10. stop_loss_monitor TP 漏挂盘中补挂 + WARNING。

### 5.3 2026-08-01 · E2E 长周期时序回放（C1-C7）

- `tests/e2e_long_cycle/`：23 日 × 全 job 时序回放，`ProbabilisticBroker` 成交回报注入 + TableSnapshotCollector 每日每表快照 + ReportBuilder 交易/持仓列表 + DingTalkLog 真实推送；
- `full_run` 断言真实成交/持仓落表 + 6 根因修复（lake cache、stk_mins 限频降级、熔断语义、data_ready、query_trades 等）；
- 运行方式：`pytest -m e2e_long tests/e2e_long_cycle/`（30-90min，CI 默认排除）。

### 5.4 2026-07-29 → 08-01 · 统一时钟与启动收编（C-5/C-6/C-7）

- C-5 进程模型：engine 合并进 uvicorn 单进程，端口单例，W1 动态白名单实例属性化（server 手动下单路径不污染）；
- C-6 统一时钟：`trading/clock.py` 单一时间源 + 触发点入口缓存；
- C-7 start-all 收编：`ops/start_all.py` 删除，broadcast connect 5 bot + discovery cron/补跑收编 lifespan，`--register-server` 注册 ONSTART。

### 5.5 2026-07-28 → 07-31 · 策略统一与执行硬化（U5/C-4）

- U5 策略统一：`strategies/neckline/` 收口为唯一活跃策略入口，`decide_exit` 成为回测/实盘共用离场单源，trailing 3 维参数（grace/step/floor）落进 21 维配置；
- state_store 重建（6 表统一交易状态）+ 止盈幂等迁移到 DB（has_order 跨重启持久）；
- C-4 错误分级 L1/L2 + scheduler 硬化（max_instances/misfire/coalesce + 停调度 flag）；
- neckline 算法修正（颈线聚集/突破判定）+ position_book 持仓账本 + live readiness 清单 + compute_unit 设计 + broadcast 机器人总管（market 播报下线）。

---

## 6. 环境依赖

### 6.1 Python 后端

```bash
pip install -r requirements.txt
```

主要依赖：`fastapi`、`uvicorn`、`pandas`、`numpy`、`tushare`、`akshare`、`jqdatasdk`、`apscheduler`、`aiohttp`、`pyarrow`、`fastparquet`、`pydantic`、`python-dotenv`、`yfinance` 等。实盘 QMT 接入用 Python 3.10 venv（`.venv310`）。

### 6.2 前端

```bash
cd presentation/web && npm install
```

---

## 7. `.env` 配置

参照 `.env.example` 创建 `.env`：

```dotenv
# 数据源
TUSHARE_TOKEN=                 # Tushare Pro(数据中心主源)
JQDATA_USERNAME=               # JQData 分钟级(高频微观动量)
JQDATA_PASSWORD=
FRED_API_KEY=                  # 宏观(可选)
ALPHA_VANTAGE_API_KEY=         # 美债/外盘(可选)

# QMT 实盘（Phase 1）
QMT_USERDATA_PATH=             # MiniQMT userdata_mini 完整路径
QMT_ACCOUNT_ID=
QMT_SESSION_ID=123456
QMT_ALLOW_LIVE_TRADE=false     # 风控挡板（环境级总闸）
QMT_ORDER_MAX_AMOUNT=1000
QMT_ORDER_MAX_SHARES=100
QMT_SYMBOL_WHITELIST=510300.SH,511010.SH,510500.SH,159915.SZ

# 自动交易引擎（二期）
AUTO_TRADE_MODE=dry_run        # dry_run=影子只记账；live=真单（需影子期硬闸）
TRADE_SHADOW_MIN_DAYS=5
ENGINE_PIPELINE_CRON=0 18 * * 1-5
ENGINE_PRE_OPEN_CRON=22 9 * * 1-5
ENGINE_STOPLOSS_INTERVAL_SECONDS=30
ENGINE_POST_CLOSE_CRON=30 15 * * 1-5
ENGINE_PRE_OPEN_CATCHUP_UNTIL=10:00
TRADE_POS_CAP=0.05
TRADE_MAX_TOTAL_EXPOSURE=0.80
TRADE_CAPITAL=1000000
TRADE_PLAN_DIR=logs/trading_plans

# 钉钉（push 播报 3 + connect 对话/人审 5）
DINGTALK_WEBHOOK=
DINGTALK_SECRET=
TRADING_BOT_ROBOT_CODE=        # push: 每日交易播报
STRATEGY_BOT_ROBOT_CODE=       # push: 每日策略播报
DATA_BOT_ROBOT_CODE=           # push: 每日数据播报
TRADING_BOT_UNIFIED_APP_ID=    # connect: quanter交易
DATA_BOT_UNIFIED_APP_ID=       # connect: quanter数据
STRATEGY_BOT_UNIFIED_APP_ID=   # connect: quanter策略
CLI_BOT_UNIFIED_APP_ID=        # connect: yzzhanCli通用对话
REVIEW_BOT_UNIFIED_APP_ID=     # connect: 训练人审
DINGTALK_ALLOWED_STAFF_IDS=    # 身份闸
BROADCAST_GROUP_ID=
```

> **优雅降级**：任一凭证缺失，对应模块不抛异常阻断启动——数据湖缺失则离线模式（查询返空）、JQData 缺失则分钟级返空、钉钉缺失则告警仅写日志、网关缺失则 dry_run 照常记账。各模块独立可用，按拥有的凭证增量启用。

---

## 8. 数据中心同步

**Tushare 通用同步器**（配置驱动：新增数据集只需在 `config/registry.py` 注册一行）：

```bash
# 全量同步(quick/slow 批)
python data/tools/sync_all_tushare.py

# 单数据集
python data/tools/sync_tushare.py <dataset_key>

# 每日增量（18:00 schtasks 入口）：quick 批（by=date/single）+ by=symbol 日频扩展
# （fund_daily/fund_nav/fund_share/cyq_perf/cyq_chips）；weekly/monthly/index_member
# 走周期守卫（湖够新自动跳过，避免每日全历史重拉）
python data/tools/sync_incremental.py
```

数据集资产元信息（source / market / granularity / script / freshness）的**单一真相源** = `config/registry.py` 的 `DATASET_REGISTRY` + `TUSHARE_DATASETS`，前端 `DataLakeView` 经 `/api/v1/data/datasets` 反射本表。盘后增量采集已收编进 pipeline 事件链（`ops/data_pipeline.py` 子进程，18:00 触发，`await proc.wait()` 确定性等完成）。

辅助数据流（历史保留）：

```bash
python data/tools/sync_macro_credit.py    # 宏观信贷(CreditRegime 输入)
python data/tools/sync_sector_daily.py    # 板块 + 活跃股日线
python data/tools/sync_jqdata_1min.py     # JQData 分钟级(配额双机制防封)
```

- **前视红线**：财报类 `date_col=ann_date`（公告日），**绝不用** `end_date`（报告期）——报告期早于公告日会导致前视偏差。
- **JQData 防暴雷**：配额双机制（手动计数 + `get_query_count` 校准，spare < 5 万即停 + 钉钉告警）+ 断点续传。
- **多湖读取**：`DataLakeReader` 按 `LAKE_CONFIG["lakes"]` 多湖缓存到内存，`get_*(lake=)` 按 key 查询，毫秒级截面/时序切片。
- **完整性 gate**：`data/integrity.filter_universe_by_continuity` 在 `_eod`/回测入口统一过滤缺停牌复牌段的残缺数据（300214.SZ 漏采教训）。

---

## 9. 启动后端与前端

### 9.1 后端（单入口）

```bash
# 生产/开发统一入口（等价 scripts/run_trading_engine.bat）
.venv310\Scripts\python.exe -m trading
```

默认 `http://127.0.0.1:8000`，API 文档 `/docs`。lifespan 自动装配引擎 + 调度 + 补跑（见 §4）。直接 `uvicorn presentation.server.main:app` 也走同一 lifespan（引擎装配软降级不阻断）；差异仅在 `python -m trading` 会先 `load_dotenv(override=True)` 并强制 live 模式不 reload。

### 9.2 前端（6 视图）

```bash
cd presentation/web && npm run dev
```

- `/caisen` —— **形态扫描**：颈线候选 + 颈线/盈亏比/止损可视化。
- `/param-lab` —— **参数训练**：异步回测 + 参数扫描 + AI 分析。
- `/dashboard` —— **驾驶舱**（宏观 CTA / CreditRegime 已下线；板块资金流端点保留，前端视图待适配）。
- `/live` —— **实盘驾驶舱**：QMT 网关持仓/订单/风控（心跳四态 unavailable/disconnected/live/vetoed_by_risk，2s 轮询）。
- `/data-lake` —— **数据中心**：Tushare 数据集资产表 + 同步触发。
- `/review` —— **审核**：候选计划 approve/reject + 钉钉远程审核。

### 9.3 参数发现引擎 CLI（离线入口）

```bash
python -m discovery oos       # 当前冠军 2025/2026 holdout 嵌套 OOS，固化去偏水平(落 SQLite)
python -m discovery verify    # 复核指定 trial 的快照/引擎双指纹一致性
python -m discovery daemon    # L4 生产入口:多轮搜索 + 帕累托收敛 + ≥N 天硬闸后 publish（02:00 cron/补跑调用）
python -m discovery publish   # 手动把冠军 publish 为 experiment DRAFT 版本
```

> **daemon 纪律**：daemon 进程必须串行单实例（多实例同跑会产生重复参数全去重、ρ 恒 0 的伪收敛）；每轮 seed 按 `42 + run_count` 派生。2026-08 起生产触发点 = engine.sched cron 02:00 + 启动补跑（lifespan），旧的 schtasks 注册已退役（`manage_ops_schtasks` 提供幂等清退）。

### 9.4 Mac 远程计算单元（compute_unit）

**定位**：Mac（封闭工作机 · 只允许 git pull 的离线计算节点）分摊 Win 主机的参数回测算力。
Win 端把参数批导出成 `task.json` push 进 git → Mac `git pull` 后离线跑批 → 生成钉钉友好
摘要由人带回（路径 2：结果不回库，好参数 Win 手动补跑，trial_id 一致自动去重）。

**协议 v2 双模式**（`Task.mode`）：

- `discovery`（默认，兼容 v1）：kelly/calmar 参数搜索评估，inner/outer 两段。
- `replay`：`backtest.replay` 引擎（与 Win 主回测同源），ReplayReport 口径指标
  （n_hits/胜率/均rr/回撤/年化）；默认按 `split` 的 inner/outer 各跑一段，也可显式
  `--start/--end` 只评单段；`--position-model` 可覆盖资金模型（空=默认 pos_cap 5%/6 仓）。

**Win 端导出任务**：

```bash
# discovery 模式（sobol_batch.json = params dict 列表）
python -m compute_unit.task_export --params-file sobol_batch.json --out tasks/<id>.json

# replay 模式（v2 · 基于最新策略回测）
python -m compute_unit.task_export --params-file params.json --out tasks/<id>.json \
  --mode replay --start 2025-01-01 --end 2026-07-31 \
  --position-model '{"pos_cap": 0.05, "max_positions": 6}'
```

**Mac 端执行**：

```bash
git pull                                   # 代码 + a_shares_daily.parquet + task.json 全走 git
python -m compute_unit verify tasks/<id>.json        # 三件哈希 + snapshot 双校验（漂移退出码 3）
python -m compute_unit run tasks/<id>.json -o result.json --n-proc 8
python -m compute_unit summary result.json --top 3   # 钉钉友好 top-N 摘要
```

> **跨机一致性**：`git_commit` / `engine_hash`（覆盖完整回测内核：strategies/neckline/*
> + backtest/replay.py + backtest/models.py + discovery/objective.py）/ `parquet_sha256`
> 三件哈希 + snapshot 双校验，任一漂移拒跑（退出码 3）——先 `git pull` 对齐再跑。

### 9.5 测试体系

```bash
pytest                        # 常规回归（默认排除 slow/e2e_long）
pytest -m slow                # 发现引擎端到端（读全 data_lake，3-4min+）
pytest -m e2e_long tests/e2e_long_cycle/   # C1-C7 长周期时序回放（30-90min，含真推钉钉）
```

---

## 10. 业务模块速览

| 模块 | 视图 / 入口 | 说明 |
|------|-------------|------|
| **自动交易引擎** | `python -m trading` | 单进程 uvicorn + TradingEngine：事件链 pipeline/pre_open/盘中巡检/post_close/网关自愈/启动补跑（§4） |
| **颈线法策略** | CaisenScreen / `strategies/neckline/` | 当前唯一活跃策略，21 维参数由 experiment 发放（§3） |
| **参数发现引擎** | `python -m discovery` / 02:00 cron | Plan 1-4 闭环：L0 快照 → L1 OOS 裁判 → L2 采样 → L3 帕累托/TPE → L4 daemon → L5 publish |
| **远程计算单元** | `python -m compute_unit` | Mac 离线跑批：discovery/replay 双模式，跨机三件哈希防漂移（§9.4） |
| **实验版本中心** | experiment API | 版本切换 + 权重校验 + 审计日志，`resolve_active()` 实时发生效配置 |
| **回测引擎** | ParamLab / CLI | `replay` 策略中立回测 + 异步任务队列 + 参数优化；回测/实盘共用 `decide_exit` |
| **数据中心** | DataLake | Tushare 20+ 数据集，registry 反射 + 同步状态（healthy/stale）+ 启动 stale sweep |
| **实盘接入** | LiveCockpit | miniQMT 极速交易（gateway QMT 唯一，EMT 已废弃），网关健康自愈 + 熔断 |
| **钉钉机器人** | push 3 + connect 5 | push（trading/data/strategy 每日播报）+ connect（cli/trading_q/data_q/strategy_q/review 对话与人审），dws 接入 |
| **E2E 长周期回放** | `pytest -m e2e_long` | C1-C7 23 日时序回放，真实成交/持仓落表断言 + 报表推送 |
| ~~宏观驾驶舱~~ | Dashboard | 宏观 CTA / CreditRegime 已下线；板块资金流端点保留 |

---

## 11. 设计文档与计划

specs（设计）/ plans（实现计划）均在 `docs/superpowers/`，按时间倒序。近期主线（反映当前真实架构）：

- **C-8 启动补跑（2026-08-02）**：[design](docs/superpowers/specs/2026-08-02-c8-startup-catchup-design.md) / [plan](docs/superpowers/plans/2026-08-02-c8-startup-catchup.md)
- **实盘主链路修复（2026-08-01）**：[design](docs/superpowers/specs/2026-08-01-live-mainchain-fixes-design.md) / [plan](docs/superpowers/plans/2026-08-01-live-mainchain-fixes.md)
- **E2E 长周期回放（2026-08-01）**：[design](docs/superpowers/specs/2026-08-01-e2e-long-cycle-design.md) / [plan](docs/superpowers/plans/2026-08-01-e2e-long-cycle.md) / [真实交易持仓列表](docs/superpowers/specs/2026-08-01-e2e-real-trade-position-lists-design.md)
- **C-7 start-all 收编（2026-08-01）**：[design](docs/superpowers/specs/2026-08-01-c7-start-all-consolidation-design.md) / [plan](docs/superpowers/plans/2026-08-01-c7-start-all-consolidation.md)
- **C-6 统一时钟（2026-08-01）**：[design](docs/superpowers/specs/2026-08-01-c6-unified-clock-design.md) / [plan](docs/superpowers/plans/2026-08-01-c6-unified-clock.md)
- **C-5 进程模型与 gate（2026-07-31）**：[design](docs/superpowers/specs/2026-07-31-c5-process-model-and-gate-design.md) / [plan](docs/superpowers/plans/2026-07-31-c5-process-model-and-gate.md)
- **错误分级与调度硬化（2026-07-31）**：[design](docs/superpowers/specs/2026-07-31-error-grading-and-scheduler-hardening-design.md) / [plan](docs/superpowers/plans/2026-07-31-error-grading-and-scheduler-hardening.md)
- **调度编排（2026-07-30）**：[design](docs/superpowers/specs/2026-07-30-scheduling-orchestration-design.md) / [plan](docs/superpowers/plans/2026-07-31-scheduling-orchestration.md)
- **state_store 重建（2026-07-29）**：[design](docs/superpowers/specs/2026-07-29-trading-state-store-redesign.md) / [plan](docs/superpowers/plans/2026-07-29-trading-state-store-redesign.md)
- **执行韧性（2026-07-29）**：[design](docs/superpowers/specs/2026-07-29-trading-execution-resilience-design.md) / [plan](docs/superpowers/plans/2026-07-29-trading-execution-resilience.md)
- **回测/实盘统一（2026-07-28）**：[design](docs/superpowers/specs/2026-07-28-strategy-unify-backtest-live-design.md) / [plan](docs/superpowers/plans/2026-07-28-strategy-unify-backtest-live.md) / [live readiness](docs/superpowers/specs/2026-07-28-live-readiness-design.md)
- **颈线算法修正（2026-07-27）**：[design](docs/superpowers/specs/2026-07-27-neckline-algorithm-fix-design.md) / [plan](docs/superpowers/plans/2026-07-27-neckline-algorithm-fix.md)
- **持仓账本（2026-07-27）**：[design](docs/superpowers/specs/2026-07-27-trading-position-book-design.md) / [plan](docs/superpowers/plans/2026-07-27-trading-position-book.md)
- **参数发现引擎**：[design](docs/superpowers/specs/2026-07-23-param-discovery-engine-design.md) / [Plan1 L0-L1](docs/superpowers/plans/2026-07-24-discovery-credibility-l0-l1.md) / [Plan2 L2-L3](docs/superpowers/plans/2026-07-24-discovery-l2-l3-search.md) / [Plan3 L3-L4](docs/superpowers/plans/2026-07-24-discovery-l3-l4-convergence.md) / [Plan4 L4-L5](docs/superpowers/plans/2026-07-24-discovery-plan4-l4-daemon-l5-publish.md)
- **实验系统**：[design](docs/superpowers/specs/2026-07-22-experiment-system-design.md) / [plan](docs/superpowers/plans/2026-07-22-experiment-system.md)
- **Layer2 解耦（trading/broker/backtest 三层定型）**：[design](docs/superpowers/specs/2026-07-22-layer2-decoupling-design.md) / [plan](docs/superpowers/plans/2026-07-22-layer2-decoupling-plan.md)
- **自动交易引擎（历史）**：[design](docs/superpowers/specs/2026-07-21-auto-trading-engine-design.md) / [plan](docs/superpowers/plans/2026-07-21-auto-trading-engine.md)
- **数据中心与数据治理**：[design](docs/superpowers/specs/2026-07-14-data-center-and-data-governance-design.md)
- **Tushare 数据快照扩容**：[design](docs/superpowers/specs/2026-07-25-tushare-data-snapshot-design.md) / [completion](docs/superpowers/specs/2026-07-25-tushare-data-snapshot-completion.md)
- **compute unit**：[design](docs/superpowers/specs/2026-07-26-compute-unit-design.md)
- **broadcast 机器人总管**：[design](docs/superpowers/specs/2026-07-26-broadcast-robot-manager-design.md)
- **实盘接入（QMT/miniQMT）**：[design](docs/superpowers/specs/2026-07-22-miniqmt-access-gap-design.md)

执行轨迹（每 Task 的实现/审查/修复证据）见 `.superpowers/sdd/progress.md`。

---

## 12. 钉钉机器人（dws 接入）

**push 播报类（出站 · 一次性）**：trading / data / strategy 三个机器人，由 pipeline 事件链尾部 `ops/brief_all.py` 串行触发（也可 schtasks 到点 / 手动 `python -m broadcast --bot <bot> --force`），各自独立 robotCode + 幂等文件（`logs/.last_<bot>_brief`，同日不重发；market 行情播报已于 2026-07-26 下线）。

**connect 对话/人审类（dws dev connect 常驻 · 入站）**：cli（yzzhanCli 通用对话）/ trading_q / data_q / strategy_q（claudecode 通道，身份闸 `DINGTALK_ALLOWED_STAFF_IDS` + `--agent-approval-mode ask` 审批闸）+ review（custom 通道 → `infra/tools/dingtalk_review_bridge.py` → `POST /api/v1/training/review` 训练人审）。5 个 bot 由 **lifespan 启动时统一拉起、shutdown 树杀**（C-7 收编）。

> 旧 `scripts/start_dingtalk_bots.md` 的手动启动步骤已收编进 lifespan；仅开发/调试场景仍需手动起。

---

## 许可与贡献

本项目为个人量化研究工程，代码与策略仅供学习交流。贡献请遵循 `CLAUDE.md` 的「全中文 + 显式实现 + 极端边界拷问」工程协议。
