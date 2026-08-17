# 交易系统全景：策略 · 流程 · 风控

> **时效标注**：本文截至 **2026-08-17**（live 引擎实跑核对版）。所有会漂的内容（cron、参数
> 生效值、遗留债状态）均以代码与 `.env` 当日实测为准；数字过期不构成文档失效，但改代码时
> 须同步刷新对应小节。
>
> **权威源分级**（冲突时以高者为准）：
> 1. 代码本体（行号引用见文内）；
> 2. 本文档（交易全景单源）；
> 3. `README.md`（架构总览）、`docs/guardrails.md` §六（熔断/鉴权 fail-closed 语义与解锁
>    SOP 权威）、`docs/neckline-method.md`（策略方法论权威）。
> 本文档与 README 冲突处以本文为准并回改 README（已发现 README §4.3 的 discovery 02:00
> cron 已过时——现为每小时+5 分 24h 低功率模式）。

---

## 一、总览：一个交易日的时间轴

```
（时点均为北京时间，交易日口径）
─────────────────────────────────────────────────────────────────────────
每小时 :05   discovery daemon（24h 低功率模式，DETACHED 子进程，轮次幂等）
09:22        pre_open —— 四段 gate → 撤昨日单 → 熔断基线 → 逐单挂今日计划
09:15-15:00  stop_loss 每 30s —— 持仓 decide_exit 巡检 + pending 撤单 + 组合熔断评估(5min节流)
15:30        post_close —— 对账 → 归因 → 日内熔断 → trade_event 补写 → 清白名单
18:00        pipeline_then_eod —— 采集→校验→data_ready→regime 前置→eod 扫信号→brief 播报
每周六 02:00 weekly_scan —— 全市场完整性周扫（只告警不自动补）
随时         health_guard 每 60s —— 网关断线探测与退避重连（风控熔断态除外）
─────────────────────────────────────────────────────────────────────────
口径红线（C-6 单一时间源 trading/clock.py）：eod 落盘 key = next_trading_day(T)，
pre_open 读 today 的计划——全链对齐，杜绝 key 错位。
```

单进程模型：`python -m trading` = uvicorn(:8000) + TradingEngine + broadcast connect 5 bot +
discovery cron + 启动补跑，全部在 lifespan 内装配。端口 8000 天然单例，第二实例 bind 失败即退。

---

## 二、策略层：颈线法（唯一活跃策略）

### 2.1 四段生命周期

| 段 | 时点 | 内容 | 代码 |
|---|---|---|---|
| ① 识别 | T 日盘后 | 80 日窗口内 ≥2 个局部高点聚成颈线；≥60% 收盘被压制；≥3 双底；当日放量突破（≥1.0×近5日均量）才产信号；cancel_on 收盘预判挡废信号；H/ATR≤5 防暴跌反弹形态；结构盈亏比 ≥2.0 | `strategies/neckline/method_v0.py` |
| ② 挂单 | T+1 09:22 | `buy_limit = 颈线 + 0.5×ATR` 限价买等回踩；有效期 max_wait=8 交易日；pending 期 high ≥ `颈线+2H` 撤单防追高 | `trading/phases/pre_open.py` |
| ③ 持有 | 盘中 30s | 止损 = `颈线 − 1×ATR`；超时 max_holding=20 交易日；**trailing 当前全 0 = 退化为固定止损**（见 §九.1） | `trading/phases/stop_loss.py` |
| ④ 离场 | 触发即挂 | 分级止盈：TP1 = `颈线+1H` 卖 30%，TP2 = `颈线+2.5H` 清仓；止损/超时全平。**回测与实盘共用 `decide_exit` 纯函数单源**（`strategies/neckline/execution.py`），杜绝决策分叉 | 同上 |

价位公式单源：`strategies/neckline/price_levels.py`（golden 钉死，回测/实盘/诊断共用）。

### 2.2 参数治理链（21 维 · experiment 发放）

```
discovery daemon（每小时+5 低功率搜索收敛）
  → publish 产 experiment DRAFT → 人审 promote → ACTIVE(weight=1.0)
  → resolve_active() 实时发放 (strategy_name, params, weight)
  → _eod scan_live 装配 → 信号带 experiment_id 归因
  → pre_open 计划确认闸 → 挂单
```

- 当前 ACTIVE：`neckline_disc_20260725_25c602`（discovery 冠军，2026-07-27 激活）。
- 切 live 有 **≥5 天影子期硬闸**（`TRADE_SHADOW_MIN_DAYS=5`，`check_shadow_gate` fail-closed——
  影子期不足则 engine 不 start scheduler，server 照常起）。

### 2.3 策略可信度边界（治理背景，诚实声明）

颈线法可信度评审结论（`docs/architecture/` 评审系列 + 记忆沉淀）：**regime 过拟合风险 + 熊市
负期望实证（2022 折外 calmar=-0.62）+ 4% Kelly 薄边缘 + 回测理想化**——单独裸奔不建议上实盘。
落地对策：
- **A1 已落**：regime 环境闸（§5.1）——BEAR/UNKNOWN 全面停新单；
- **A2 已落**：`min_yearly_calmar` 裁判门槛（discovery 搜索目标）；
- **A3/A4 未动工**：盈亏比真实口径复核、执行成本压力测试——**当前最高优先空洞**。

---

## 三、计划生命周期（trade_event 状态机）

真相源 = `logs/trading_state.db` 的 `trade_event` 表（SSoT Phase C 后 JSON 落盘已删、入 BANNED）。

```
SIGNAL ──→ CONFIRMED ──→ ORDERED ──→ FILLED ──→ TP1_FILLED ──→ TP2_FILLED/CLOSED
   │           │
   │           └──→ VETOED（终局防线：人审否决，晚于 CONFIRMED 也判未确认）
   └──（仅 SIGNAL 无 CONFIRMED = 未确认，pre_open 不放行）
```

- `trade_id = {account_id}_{symbol}_{plan_date}`（`build_trade_id` 单点）；
- **日期轴陷阱**：计划日只在 trade_id 后缀（`substr(trade_id,-10)`），`timestamp` 是 T 日盘后
  写入时间——按 timestamp 查计划恒空；
- 确认语义（`state_store.is_trade_confirmed` 单点）：latest ∈ {None, SIGNAL, VETOED} = 未确认，
  其余（含 ORDERED/FILLED 等生命周期后态）= 已确认；
- AUTO_CONFIRM：eod 产 SIGNAL 后自动写 CONFIRMED（08-14 起实跑 `auto_confirmed=True`）；
  人工仍可经 `/review` 或钉钉 review bot 走 veto 终局。

---

## 四、执行流程（六 job 详解）

### 4.1 pipeline_then_eod · 18:00 周一-五（`ENGINE_PIPELINE_CRON`）

事件链（`trading/orchestrate/pipeline.py`）：

1. **采集**：子进程跑日频增量（`ops/data_pipeline.py`，`await proc.wait()` 确定性等完成）；
2. **校验**：按策略声明的数据集逐项检查 → 全绿落 `data_ready(T)`；任一未绿 → eod 跳过 +
   CRITICAL；
3. **连续性 scan + 异步补采**（T13-B：`_scan_and_spawn_repair`，缺段后台补不阻塞）；
4. **A1 regime 前置闸**（pipeline.py:218-228）：BEAR/UNKNOWN → **eod 停产 T+1 计划**（台账
   skipped + WARN 播报），brief 一并跳过——**只断新单，存量退出照常**；
5. **eod**（`engine._eod`）：完整性 gate（`data/integrity.filter_universe_by_continuity`，300214
   漏采教训）→ `resolve_active()` 装配策略 → 全市场扫描 → 产 SIGNAL/CONFIRMED（plan_date =
   next_trading_day）→ 风控参数（`trading/critical.py:_trade_cfg`，pos_cap 单笔 5% 算 qty）；
6. **brief 三播报**（`ops/brief_all.py` 子进程串行 trading/strategy/data；trading brief 含
   「明日（T+1）交易计划」段，2026-08-17 补）。

### 4.2 pre_open · 09:22 周一-五（`ENGINE_PRE_OPEN_CRON`）

`trading/phases/pre_open.py`，顺序执行：

1. **四段 gate**（`engine._pre_open_gate`，先便宜后贵，任一未绿即早返不触网关）：
   - ① 计划确认：`load_plan(today)` 无计划 / `confirmed=False` → skip；
   - ② 网关健康：gw 为 None / `_connected=False` / miniQMT 客户端未就绪 → skip；
   - ③ 数据就绪（W5 单口）：`get_ready(T)` = data_ready 内容校验 AND pipeline 台账 done；
   - ④ **regime**（A1）：eod 后隔夜可能转空，挂单前二次复核（同一 classify 单源+当日缓存）。
2. **撤昨日未成交单**（防残留敞口）；
3. **超期平仓现算**（SSoT Phase B：`scan_expired_positions`（holding_days > max_holding）+
   `close_expired_positions`（跌停价挂卖）——从 post_close 迁入，盘前清超期）；
4. **抓日内熔断基线**（开盘权益快照 → account_daily.start_total_asset）；
5. **注入动态白名单** + 逐单挂限价买单。

gate 失败：live 推 CRITICAL 钉钉；台账 skipped（补跑窗口 [09:22, 10:00) 内可重试）。

### 4.3 stop_loss · 每 30s（`ENGINE_STOPLOSS_INTERVAL_SECONDS`）

`trading/phases/stop_loss.py`（最高风险模块）：

- ① 盘中时段判定（09:15-15:00，非盘中 no-op）；
- ② 取网关与持仓（网关锁态 → 健康闸 skip + CRITICAL）；
- ③ 批量取现价 + 当日累积 high/low（R7 bar 防御：get_full_tick 是累积值口径）；
- ④ holding 期巡检：每持仓构造 state+bar → **`decide_exit` 执行单源**（主路径）→ 按 action
  分发（挂止损/差额补挂止盈/超时平仓）；DB 查失败时「已挂判定」保守视为已挂（§5.7）；
  fallback 路径 `should_trigger_stop`（D12 兜底）；
- ⑤ pending 期 cancel_on 巡检：high ≥ 撤单阈值 → 撤未成交买单（防追高）；
- **CR-3 组合级熔断评估**（⑤后、聚合告警前，5min 节流 `PortfolioBreakerThrottle`）：
  - 基线 = `account_daily.start_total_asset`（缺失 → T-1 close 兜底，I-1）；
  - 当前权益 = `query_asset`（断线/锁定/超时一律返 `{}` 防脏读）；
  - 分支① 触发（≤ -3%）：撤全部未终态单 + `emergency_halt()` + CRITICAL——**不停调度**
    （停调度会杀死止损监控自身，盘中不可接受）；
  - 分支② 评估失败（返空/异常）：只观测，连续 ≥3 轮才升 CRITICAL；
  - 分支③ 基线缺失（live）：`emergency_halt` + CRITICAL（转 halt 形态保监控存活）。

### 4.4 post_close · 15:30 周一-五（`ENGINE_POST_CLOSE_CRON`）

`trading/phases/post_close.py`（各段独立 try-except 软降级）：

1. **对账**（W3.4 broker 权威）：真实持仓 ↔ position_book 纠偏，成交流水兜底纠正；
2. **aggregate_fills 盘后归因**（降级：不重写账本，仅日志/播报）；
3. **日内熔断三步**（盘后闸）：基线读取 → `query_asset` → `check_daily_loss_limit`
   （`trading/compute/breaker.py`，`CIRCUIT_DAILY_LOSS_LIMIT=-0.03`；**curr 缺失 live 同口径
   fail-closed**，CR-4：CRITICAL + 停调度）；
4. ~~trailing 盘后演进~~ **已删**（SSoT review P2 死计算，§九.1）；
5. ~~max_holding 超期标记~~ 迁至 pre_open 现算（4.2.3）；
6. trade_event 补写（CLOSED/TP1_FILLED 终态对齐）+ **清动态白名单**。

### 4.5 常驻辅助

- **health_guard 每 60s**：断线探测 → 退避重连。**`_risk_halted=True` 时只告警不自愈**（风控
  熔断不得自动解除，防熔断→自愈→再熔断循环放血）；纯网络断线才走自愈。
- **weekly_scan 周六 02:00**：全历史完整性周扫，FAIL 只告警 + `logs/integrity_weekly.json`
  （全历史补采量大，不自动 repair 防周末撞限频）。
- **discovery daemon 每小时 :05**（24h 低功率）：DETACHED 子进程单实例，轮次/seed 幂等。
- **broadcast connect 5 bot**：cli / trading_q / data_q / strategy_q / review（人审通道）。

### 4.6 C-8 启动补跑与 job 台账

- **台账**（`logs/trading_job_run.db`，`trading/job_ledger.py`）：`(job_name, business_date)` 键，
  running/done/skipped/failed；启动 `reset_stale_running` 清崩溃残留。
- **补跑三腿**（`trading/catchup.py`，仅 lifespan 一次）：
  - pipeline 腿：D = 最近已收盘交易日，未 done 且（D < 今天 或 now ≥ 18:00）→ 补全链；
    **政策 A 裁剪**：plan_date 已过 pre_open 窗口 → run_eod=False 不产废计划；
  - brief 腿：pipeline done 但 brief 台账非 done → 补播（幂等）；
  - pre_open 腿：今天是交易日且 now ∈ [09:22, 10:00) 且未 done → 补挂单；窗口已过 → CRITICAL。
- 失败语义：补跑异常 → 台账 failed + CRITICAL，不停调度——留当晚 18:00 cron 自然收敛。

---

## 五、风控全景（分层闸门地图）

### 5.0 总表

| 层 | 闸门 | 触发条件 | 动作 | 代码 |
|---|---|---|---|---|
| L0 环境 | **regime 闸** | HS300 ≤ MA200 或 宽度 ≤50%（任一） | eod 停产 + pre_open 拒挂（只断新单） | `trading/compute/regime.py`、pipeline.py:218、engine `_pre_open_gate`④ |
| L1 计划 | 确认闸 | SIGNAL 无 CONFIRMED / VETOED | pre_open 不放行 | `load_plan` + `is_trade_confirmed` |
| L1 计划 | 影子期闸 | 切 live < 5 天 | 不 start scheduler | `check_shadow_gate` |
| L2 前置 | pre_open 四段 | ①计划②网关③数据④regime 任一未绿 | 早返不触网关 + live CRITICAL | `engine._pre_open_gate` |
| L3 单笔 | session 闸 | 非 A 股时段（enforce_session 时） | 拒单 | `compute/risk.py:check_order` |
| L3 单笔 | dry_run 闸 | 请求级模拟 | 不真下单 | 同上 |
| L3 计划侧 | 仓位上限 | 单笔 pos_cap=5% | eod 算 qty 时约束 | `trading/critical.py:_trade_cfg` |
| L4 组合 | **日内 -3% 熔断** | 权益回撤 ≤ -3% | 盘中(5min节流)+盘后双评估点：撤单+emergency_halt | `compute/breaker.py`、stop_loss CR-3、post_close ③ |
| L4 组合 | 基线缺失 | start_equity 缺失/≤0 | **fail-closed**：live 停调度/halt | breaker DG-G3 + CR-4 |
| L4 组合 | 评估失明 | query_asset 返空（断线） | 盘中只观测；盘后 live 同基线缺失口径 | CR-3 分支② / CR-4 |
| L5 设施 | 鉴权闸 | live 无 QUANTER_API_TOKEN | **拒绝启动**（DG-G2） | `trading/__main__.py:374` |
| L5 设施 | 生产链闸 | QUANTER_REQUIRE_LIVE=1 且非 live | 拒绝启动（A5，防 dry_run 接管生产） | 同上 |
| L5 设施 | 环境总闸 | QMT_ALLOW_LIVE_TRADE=false | 拒真单强制模拟 | `compute/risk.py` |
| L5 设施 | 单例锁 | 端口 8000 + QMT session 锁 | 第二实例退出 | `_assert_single_instance` |

### 5.1 L0 环境层：regime 闸（A1 · DG-G4）

三态判定，**双腿确认、非对称收紧**（`trading/compute/regime.py`）：

```
BULL    = HS300 收盘 > MA200  ∧  宽度 > 0.5         → 允许新单
BEAR    = 任一不满足                             → 停手（断新单，存量退出照常）
UNKNOWN = 数据缺失（指数<201根 / 宽度样本<500只 / 异常）→ 视同 BEAR（fail-closed）
```

- 宽度 = 全市场**末位有效 K 线** close > 各自 MA200 的占比（时点宽度，防权重股假多头）；
- 阈值（MA200/0.5/500 只）为固定经验值，**红线绝不进 TPE**（防搜索过拟合 regime 本身），改值
  须走 ADR；
- 双拦截点：**eod 前置**（pipeline.py:218 停产计划）+ **pre_open ④**（挂单前二次复核，eod 后
  隔夜可能转空）；当日缓存，盘中不重判（环境闸非交易信号）；
- 实弹记录：2026-08-17 09:22 首次实弹拦截（HS300 4666≤4702；宽度 23%≤50%，15 单全拒）。

### 5.2–5.3 计划层与执行前置

见 §三（确认状态机）与 §4.2（四段 gate）。要点：gate 顺序「先便宜后贵」（DB 读 < 探测 <
查询），任一未绿**绝不触达网关写操作**。

### 5.4 L3 单笔层（A-2 裁定后的实际存留）

2026-08-06 A-2 裁定（D1-D3/D5）后，请求级挡板**仅剩两道**：

- `dry_run`（请求级模拟，非错误）；
- A 股交易时段闸（09:15 起，enforce_session=True 时生效）。

**已删除并依赖上游/柜台兜底**：confirm（由计划确认闸承担）、前端白名单（D3 放开）、涨跌停/
金额/股数（柜台与交易所兜底）。⚠️ `.env` 的 `QMT_ORDER_MAX_AMOUNT=200000` /
`QMT_ORDER_MAX_SHARES=10000` **现为遗留无消费方**（grep 全库无读取），勿误信仍在挡。

仓位约束在**计划生成侧**：`TRADE_POS_CAP=0.05`（单笔 = capital×5% 算 qty）。
⚠️ `TRADE_MAX_TOTAL_EXPOSURE=0.80` 同为**遗留无消费方**（总敞口上限当前无代码执行）。

### 5.5 L4 组合层：日内 -3% 熔断

- **判定单源** `check_daily_loss_limit`（`compute/breaker.py`）：`(curr-start)/start ≤ -0.03`
  （`<=` 宁早一拍）；`CIRCUIT_DAILY_LOSS_LIMIT` 可覆盖，缺省 -0.03；
- **双评估点**：盘中 stop_loss（CR-3 前移，5min 节流）+ 盘后 post_close ③；
- **触发三件套**：撤全部未终态单 + `emergency_halt()`（粘滞锁拒新单）+ CRITICAL；
- **`emergency_halt` 真实系统态**与人工解锁 SOP：**见 `docs/guardrails.md` §六**（权威）。
  核心边界：lock_down 期间止损监控被健康闸跳过（残余持仓无止损覆盖），解锁 = 进程内
  `gw.clear_risk_halt()` 唯一口，**先查 `logs/alerts.log` 熔断原因再解锁，勿盲清**。

### 5.6 L5 基础设施层

- **DG-G2 鉴权 fail-closed**：live 无 token 拒启动；默认 host 锁 127.0.0.1（防下单端点裸奔
  局域网）；
- **A5 生产链闸**：`QUANTER_REQUIRE_LIVE=1`（start_server/run_trading_engine bat 置）时非
  live 拒启动——防 08-05 式 dry_run 实例反复起停接管生产端口；
- **单例**：端口 bind 失败即退 + QMT session 锁（`logs/trading_engine_<sid>.lock`）；
- **错误分级 L1/L2**（C-4）：L1 = 不知是否发过单/落账失败（升 CRITICAL 停手），L2 = 可观测
  降级；scheduler 硬化（max_instances=1 + misfire_grace 300s + coalesce）。

### 5.7 风险取向显式声明（有意设计，非遗漏）

**超卖 vs 漏挂，系统性选择防超卖**：DB 查询失败时止损/止盈「已挂判定」保守视为已挂（宁可
漏挂人工补，不重复挂双倍卖）。漏挂方向由 CR-5 反向扫描（`audit_ssot.check_fill_position`：
fill 净额≠0 而 position 缺行即告警）+ 人工补挂兜底。

---

## 六、数据链与完整性

```
Tushare(主) / AKShare / JQData(辅)
  → 18:00 pipeline 采集子进程（quick 批 + by=symbol 日频扩展；weekly/monthly 周期守卫）
  → 按策略声明数据集校验 → data_ready(T) 落库
  → 连续性 scan（日级，缺段异步补采；unfillable sidecar 记录停牌真值）
  → weekly_scan 周六全历史兜底（只告警）
  → eod 入口 filter_universe_by_continuity 过滤残缺段（300214 教训）
```

- **前视红线**：财报类 `date_col=ann_date`（公告日），绝不用 `end_date`（报告期）；
- **停牌真值**（2026-08-16 新债波收口）：日级判定 + 长洞市场共识启发式 + unfillable sidecar
  ——unjustified 缺口 16371→0，daemon fail-closed 闸条件达成；
- data_ready **单口判定**（W5）：内容校验① AND pipeline 台账②，消除「台账 done、内容缺、
  播报 healthy」三张嘴漂移。

---

## 七、观测与播报

| 通道 | 内容 | 触发 |
|---|---|---|
| trading brief | 当日成交/拦截/资金/持仓/止损 + **明日（T+1）计划段**（08-17 补） | pipeline 尾部 + C-8 补跑 |
| strategy brief | 信号数/参数迭代/近期回测健康度 | 同上 |
| data brief | 数据集健康度双口径（mtime + 内容最新日）+ get_ready 对账 | 同上 |
| alerts.log + 钉钉 CRITICAL/WARN | 熔断/基线缺失/gate 拒绝/网关异常 | 事件驱动 |
| review connect bot | 钉钉人审（veto 终局防线） | 常驻 |
| /review 前端 | 候选计划 approve/reject | 手动 |

已知运维点：**dws 登录态 30 天过期**（~09-15 到期）——brief 推送失败 `returncode=5` 先查
`dws auth status`；推送链经 npm `.cmd` 垫片已修为直调 node（`broadcast/push.py`，42408e9c）。

---

## 八、环境变量与参数清单（2026-08-17 实测）

### 8.1 交易风控类

| 变量 | 当前值 | 消费点 | 状态 |
|---|---|---|---|
| `AUTO_TRADE_MODE` | **live** | 全引擎 | 生效 |
| `QMT_ALLOW_LIVE_TRADE` | true | risk.py 环境总闸 | 生效 |
| `TRADE_POS_CAP` | 0.05 | critical.py 单笔仓位 | 生效 |
| `CIRCUIT_DAILY_LOSS_LIMIT` | -0.03 | breaker.py 组合熔断 | 生效 |
| `TRADE_SHADOW_MIN_DAYS` | 5 | 影子期硬闸 | 生效 |
| `TRADE_MAX_TOTAL_EXPOSURE` | 0.80 | — | **遗留无消费** |
| `QMT_ORDER_MAX_AMOUNT` | 200000 | — | **遗留无消费**（A-2 删挡板） |
| `QMT_ORDER_MAX_SHARES` | 10000 | — | **遗留无消费**（同上） |
| `TRADE_STOP_ATR_MULT` 等 `TRADE_*` | 缺省 | critical.py（缺省对齐回测 DEFAULTS） | 生效（env 可覆盖） |

### 8.2 调度类

| 变量 | 当前值 | 说明 |
|---|---|---|
| `ENGINE_PIPELINE_CRON` | （缺省 18:00 mon-fri） | 事件链 |
| `ENGINE_PRE_OPEN_CRON` | 22 9 * * mon-fri | 挂单 |
| `ENGINE_STOPLOSS_INTERVAL_SECONDS` | 30 | 盘中巡检 |
| `ENGINE_POST_CLOSE_CRON` | 30 15 * * mon-fri | 盘后收口 |
| `ENGINE_PRE_OPEN_CATCHUP_UNTIL` | 10:00 | 补挂单窗口 |
| `ENGINE_EOD_PLAN_CRON` | 0 19 * * 1-5 | **遗留**（独立 eod_plan job 已并入 pipeline，无注册方） |

### 8.3 鉴权/基础设施

`QUANTER_API_TOKEN`（live 硬闸，DG-G2）、`QUANTER_REQUIRE_LIVE`（bat 置 1）、`SERVER_HOST`
（默认锁 127.0.0.1）、`QMT_SESSION_ID`、`QMT_USERDATA_PATH`、`TRADING_JOB_LEDGER_DB`。

---

## 九、已知边界与技术债（如实清单）

1. **trailing 收紧链路停摆**：`_evolve_trailing_stops` 已删（SSoT review P2 死计算，无消费
   方）。当前 ACTIVE 参数 trailing 三件全 0 = 固定止损，**无实际影响**；但 env 三件套与
   `compute_stop_price` 保留，重实现列为独立 live P0 task。
2. **account_daily 基线链未闭**：08-10 起每日 `start_total_asset` NULL（08-17 仍复现，当日行
   缺失）。T-1 close 兜底已消假阳性 halt（I-1），但「正基线链」未修——runbook ② 持续核查项。
3. **A3/A4 空洞**：盈亏比真实口径复核、执行成本压力测试——策略可信度最高优先待办。
4. **遗留 env 三件**：`TRADE_MAX_TOTAL_EXPOSURE` / `QMT_ORDER_MAX_AMOUNT` /
   `QMT_ORDER_MAX_SHARES` / `ENGINE_EOD_PLAN_CRON` 无消费方（§八）——待清理或恢复消费。
5. **dws 登录态 30 天周期**：下次 ~09-15 到期，brief 推送 returncode=5 先查 auth。
6. **600519 阈值语义待用户确认**：holding=15 恰在 max_holding 阈值（`>` 判定推迟一日平仓）。

---

## 十、关键文件索引

| 域 | 文件 | 职责 |
|---|---|---|
| 引擎 | `trading/engine.py` | TradingEngine 装配 + job 注册 + `_eod` + `_pre_open_gate` 四段 |
| 执行 | `trading/phases/{pre_open,stop_loss,post_close}.py` | 三阶段执行体 |
| 编排 | `trading/orchestrate/pipeline.py` | 18:00 事件链（含 regime 前置） |
| 补跑 | `trading/catchup.py` + `trading/job_ledger.py` | C-8 三腿 + 台账 |
| 风控 | `trading/compute/{risk,breaker,regime,stop}.py` | 单笔挡板/组合熔断/环境闸/止损价（纯函数） |
| 网关 | `trading/gateway_service.py` + `broker/qmt_*.py` | 网关单例/emergency_halt/下单 |
| 状态 | `trading/state_store.py` + `trading_plan.py` | 6 表真相源 + 计划读取（trade_event） |
| 策略 | `strategies/neckline/{method_v0,execution,price_levels}.py` | 识别/离场单源/价位单源 |
| 实验 | `experiment/` | resolve_active 参数发放 |
| 播报 | `broadcast/{brief_trading,push}.py` + `ops/brief_all.py` | 文案/出站/串推 |
| 时钟 | `trading/clock.py` + `trading/calendar.py` | 单一时间源 + 交易日历 |
