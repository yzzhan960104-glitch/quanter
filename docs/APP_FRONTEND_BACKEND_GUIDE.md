# 前后端应用功能详解（2026-08-30 定稿版）

> 本文档面向需要理解系统全貌的读者（开发者/运维/新成员），覆盖数据层到前端展示的完整链路。
> 配套文档：`docs/PANORAMA_AND_STARTUP_SOP.md`（冷启动 SOP）· `docs/STRATEGY_ALGORITHM_GUIDE.md`（策略算法）。

---

## 一、系统架构总览

```
┌─────────────────────────────────────────────────────────────────┐
│                        掘金终端 (emgm3.exe)                       │
│  ┌────────────┐  ┌────────────┐  ┌────────────┐               │
│  │ NECK 主腿   │  │ NECK-EXP  │  │  7002 网关  │               │
│  │ d9324346   │  │ 5557c9cb  │  │ (REST API) │               │
│  │ 20万仿真   │  │ 20万仿真   │  │            │               │
│  └─────┬──────┘  └─────┬─────┘  └──────┬─────┘               │
│        │                │                │                      │
│  ┌─────┴────────────────┴────────────────┴─────┐               │
│  │           7001 掘金 SDK 服务                  │               │
│  └─────────────────────────────────────────────┘               │
└─────────────────────────────────────────────────────────────────┘
         │                                    │
         │ 09:31/15:36 策略调度               │  REST 代理
         ▼                                    ▼
┌─────────────────────┐          ┌─────────────────────────────┐
│   FastAPI Server     │          │     Vue3 前端 (cockpit)     │
│   127.0.0.1:8000    │◄────────│   presentation/web/         │
│                     │  HTTP    │                             │
│  lifespan 托管：     │          │  视图：                     │
│  · pipeline 18:00   │          │  · Dashboard (三卡)        │
│  · discovery 02:00  │          │  · ExperimentView          │
│  · digest 18:30     │          │  · DiscoveryLabView       │
│  · 晨检 09:40       │          │                             │
│  · 台账 15:40       │          │  组件：                     │
│  · EOD报告 15:45    │          │  · StatusCard (心跳)       │
│  · 双腿对照 15:50   │          │  · AssetCard (资金)        │
│  · audit巡检 16:05  │          │  · TradesTable (成交流水)  │
│  · gm看护 5min      │          │  · LegSelector (腿选择)    │
│                     │          │  · AbDailyCard (A/B对照)   │
│  API 路由：          │          │  · RoundCard (轮次档案)    │
│  /api/v1/gm/*      │          │  · AuditExplorer (事件流)  │
│  /api/v1/research/* │          │                             │
│  /api/v1/trading/* │          │                             │
└─────────┬───────────┘          └─────────────────────────────┘
          │
          ▼
┌─────────────────────────────────┐
│        数据湖 (data_lake/)       │
│  · a_shares_daily (主湖 10.5M) │
│  · daily_basic (估值/换手)      │
│  · moneyflow (资金流)           │
│  · stock_basic (行业/上市)      │
│  · index_daily / index_member  │
│  · 40+ parquet 表               │
└─────────────────────────────────┘
```

---

## 二、后端详解

### 2.1 FastAPI Server（`presentation/server/main.py`）

**启动命令**：
```bash
.venv310/Scripts/python.exe -m uvicorn presentation.server.main:app --host 127.0.0.1 --port 8000
```
或双击桌面 `启动Quanter服务器.bat`（幂等：8000 在听即跳过）。

**lifespan 启动时装配**：

| 组件 | 功能 | 触发 |
|---|---|---|
| ops_sched (APScheduler) | 全部定时任务宿主 | 随 server 生命周期 |
| pipeline_then_eod | 数据管道（采集→校验→brief→数据集同步） | cron 18:00 周一至五 |
| discovery daemon | 低功率参数探索 | cron 02:00 |
| research digest | 研究摘要推钉钉 | cron 18:30 |
| ops_morning_check | 晨检（掘金腿状态摘要推钉钉） | cron 09:40 |
| ops_emquant_ingest | 掘金腿 audit CSV → experiments.db | cron 15:40 |
| ops_eod_report | EOD 报告（当日交易摘要） | cron 15:45 |
| ops_gm_ab_compare | 双腿 A/B 对照 | cron 15:50 |
| ops_audit_ssot | 7 项 SSOT 巡检 | cron 16:05 |
| ops_gm_guard | 终端/策略进程看护 | interval 5min |
| broadcast connect | 5 个钉钉 bot 连接 | 随 lifespan |

**API 路由**（`/api/v1/`前缀，挂载层统一 `require_read_cookie` 鉴权）：

| 路由组 | 功能 | 数据源 |
|---|---|---|
| `/gm/legs` | 双腿目录（注册表+runtime+策略名） | gc.LEGS + 7002 代理 |
| `/gm/overview` | 终端总览（策略列表+账户连接状态） | 7002 /v3/strategies + /v3/account-statuses |
| `/gm/asset?leg=` | 资金（nav/available/frozen/market_value） | 7002 /v3/account-trade/cash |
| `/gm/positions?leg=` | 持仓（volume/vwap/fpnl/available） | 7002 /v3/account-trade/positions |
| `/gm/orders?leg=` | 当日委托（含状态/拒因） | 7002 /v3/account-trade/orders |
| `/gm/trades?leg=` | 成交流水（execrpts 成交价量） | 7002 /v3/account-trade/execrpts |
| `/gm/ab` | 双腿当日对照（工程闸+信号 diff+红旗） | ops.emquant_ab_compare |
| `/gm/round` | A/B 轮次档案 | emquant/ab_round.json |
| `/gm/audit?leg=&date=` | 事件流下钻（terminal_audit 按腿/日过滤） | experiments.db（只读） |
| `/research/discovery/*` | 参数发现（敏感性/热力图/状态） | discovery_trials.db |

### 2.2 掘金策略进程（`emquant/emquant_neckline_pilot.py`）

**物理位置**：`C:\Users\yzzhan\.emgm3\projects\<strategy_id>\main.py`

**目录结构**：
```
<strategy_id>/
├── main.py                    # 策略代码（组装产物，勿手改）
├── config/runtime.json        # token + strategy_id + account_id
├── relaunch_strategy.ps1      # 进程级重启脚本（杀旧+验无双+按终端命令行重启）
├── state/state.pkl            # 策略唯一持久状态（JSON，schema v1）
├── state/amihud_pct_latest.json  # [已退役] v1 writer 的分位文件（保留为数据资产）
├── audit/audit_YYYYMMDD.csv   # 对拍审计逐行追加
├── agent_run.log              # 进程 stdout（SDK 连接日志）
├── agent_run.err.log          # 进程 stderr
└── main.py.bak_<stamp>        # 版本备份链（可回退任意一步）
```

**state.pkl schema v1**：
```json
{
  "version": 1,
  "scan_done": ["2026-08-28"],       // 当日已扫描标记（幂等防重扫）
  "last_signal": {"300803.SZ": "2026-08-28"}, // 冷却锚点
  "placed": {"2026-08-28": ["cl_ord_id_1"]},  // 当日已挂（单日闸计数用）
  "orders": {"cl_ord_id_1": {...}},   // 全量订单档案
  "positions": {"300433.SZ": {...}}   // 持仓档案
}
```

**audit CSV 事件族**：

| 事件 | 含义 | 关键字段 |
|---|---|---|
| INIT | 进程启动 | build_stamp / account / strategy_id / subscribed |
| SIGNAL | 信号产出 | symbol / neckline / entry_price / rr / atr / formed_at |
| SIGNAL_COOLDOWN_SKIP | 冷却跳过 | symbol / last_signal / cooldown |
| SIGNAL_SKIP_HELD | 持仓/在途跳过 | symbol / held / open_buy |
| SIGNAL_FILTERED_AMIHUD | amihud 过滤剔除 | symbol / amihud60 / asof |
| AMIHUD_FILTER_EXEMPT | 豁免（≤5/有效不足） | n_signals / n_valid / keep_top |
| ORDER_PLACED | 挂单成功 | symbol / price / qty / cl_ord_id / cancel_on |
| ORDER_BLOCKED | 挂单被闸拒 | symbol / reason（四闸各自的拒绝文案） |
| BLOCK_SKIP | 人工风控拦截 | msg |
| WARN | 各类告警 | type / detail |

### 2.3 数据管道（`ops/data_pipeline.py` + `trading/orchestrate/pipeline.py`）

**触发**：server lifespan cron 18:00 周一至五（`pipeline_then_eod`）

**步骤**（串行，故障隔离）：
```
① T1 检查（T-1 完整性，告警级）
② 日频采集（sync_daily_incremental: 分页拉 T 日 daily + adj_factor → 前复权 → append 湖）
③ T2 检查（T 完整性，FAIL 重采→熔断 eod）
④ amihud 分位写入（ops/amihud_pct_writer → 全市场截面分位 → 主腿 state/）
```

**湖数据**：
- 主湖 `a_shares_daily.parquet`：前复权日线，1997 起，10.5M 行
- 定点前复权（adjust_end_time 钉死）保证同日重取幂等可重复
- `skip_suspended=True`：停牌日无 bar（日线根数=实际成交日数）

### 2.4 运维定时任务（全部收编 server lifespan，W9 2026-08-30）

| 任务 | 时刻 | 脚本 | 日志 |
|---|---|---|---|
| 晨检 | 09:40 daily | ops.emquant_morning_check | logs/ops_morning_check.log |
| 台账入库 | 15:40 daily | ops.emquant_audit_ingest | logs/ops_emquant_ingest.log |
| EOD 报告 | 15:45 daily | ops.emquant_eod_report | logs/ops_eod_report.log |
| 双腿对照 | 15:50 daily | ops.emquant_ab_compare | logs/ops_gm_ab_compare.log |
| audit 巡检 | 16:05 daily | scripts/audit_ssot.py | logs/audit_schtask.log |
| gm 看护 | 5min interval | ops.gm_terminal_guard --once | logs/gm_guard_cron.log |
| 数据管道 | 18:00 mon-fri | trading.orchestrate.pipeline | （进程内） |

所有任务走 `_spawn_venv_subprocess`（DETACHED 子进程 + 日志重定向），与
discovery/digest 同范式——server 重启不杀正在跑的子进程。

---

## 三、前端详解

### 3.1 技术栈

- Vue 3 + TypeScript + Vite
- UI 库：Element Plus
- 图表：ECharts（vue-echarts）
- HTTP：Axios（`@/api/client.ts` 统一拦截：token 注入 + 错误 Toast + 防二次解构）

### 3.2 视图（`presentation/web/src/views/`）

| 视图 | 路由 | 功能 | 数据源 |
|---|---|---|---|
| DashboardView | / | 三卡主页（心跳/资金/成交流水）+ 腿选择器 | /gm/overview + /gm/asset + /gm/trades |
| ExperimentView | /experiment | A/B 实验对照（双腿信号 diff + 工程闸 + 红旗） | /gm/ab |
| DiscoveryLabView | /discovery | 参数发现实验室（敏感性仪表板/热力图/搜索进展） | /research/discovery/* |

### 3.3 核心组件（`presentation/web/src/components/`）

| 组件 | 功能 | 消费 API |
|---|---|---|
| StatusCard | 终端心跳三态（连接中/断连/单腿） | /gm/overview |
| AssetCard | 资金卡（nav/available/frozen/market_value） | /gm/asset?leg= |
| TradesTable | 成交流水表（当日成交价量） | /gm/trades?leg= |
| LegSelector | 双腿切换器（main/incumbent ↔ exp/challenger） | /gm/legs |
| AbDailyCard | A/B 当日对照卡（工程闸绿灯/红旗） | /gm/ab |
| RoundCard | A/B 轮次档案（ab_round.json 透传） | /gm/round |
| AuditExplorer | 事件流下钻（按腿/日期/事件过滤） | /gm/audit |

### 3.4 数据流

```
掘金终端 7002 REST API
        │
        ▼ FastAPI /api/v1/gm/* （只读代理，token 只活在 server 进程）
        │
        ▼ Axios (client.ts → VITE_API_TOKEN → require_read_cookie)
        │
        ▼ Vue 组件（按 leg 参数切换数据源）
        │
        ▼ ECharts / Element Plus 渲染
```

**鉴权**：前端 `VITE_API_TOKEN`（.env）→ 请求头 Cookie → server `require_read_cookie`
校验 → 掘金 token 绝不进前端 bundle（内网假设不扩散到终端 token）。

### 3.5 构建与部署

```bash
cd presentation/web
npm run build          # → presentation/web/dist/
# server 静态托管 dist/，同一端口（8000）出页面和 API
```

---

## 四、掘金终端操作面

### 4.1 终端 API（7002 REST，Bearer token 鉴权）

| 端点 | 功能 | 权限 |
|---|---|---|
| GET /v3/strategies | 策略列表（含 stage/status） | 只读 |
| GET /v3/account-statuses | 账户连接状态 | 只读 |
| GET /v3/account-trade/cash/{id} | 资金 | 只读 |
| GET /v3/account-trade/positions/{id} | 持仓 | 只读 |
| GET /v3/account-trade/orders/{id} | 委托 | 只读 |
| GET /v3/account-trade/execrpts/{id} | 成交流水 | 只读 |
| POST /v3/strategy-commands/{sid}/stop | 停策略 | 变更（须用户指令） |
| PUT /v3/strategies/{sid} | 策略配置 | 变更 |
| POST /v3/account-trade/login/{id} | 账户激活 | 变更 |

### 4.2 仿真账户入金（2026-08-29 打通）

```bash
# 云端 broker-rpcgw（不是本机 7002）
curl -X POST http://61.129.248.86:7102/v3/accounts/<account_id>/cash-inout \
  -H "Authorization: Bearer <encryptedToken>" \
  -H "Grpc-Metadata-X-USERID: 13336" \
  -H "Grpc-Metadata-X-ORGCODE: eastmoney-bus-simu" \
  -H "Content-Type: application/json" \
  -d '{"accountId": "<account_id>", "amount": 100000}'
```
纯现金入金，不动持仓不重置。本机 7002 无此路由；update-account 的 initCash 路被
"账号非断开状态"锁死不可走。

### 4.3 策略生命周期

| 操作 | 命令 | 说明 |
|---|---|---|
| 启动/重启 | `powershell relaunch_strategy.ps1` | 杀旧+验无双+按终端命令行重启 |
| 验证 | audit INIT 行三锚（stamp/account/strategy_id） | 比 GUI 可靠 |
| 停止 | `POST /v3/strategy-commands/{sid}/stop` | schema 自 UI 包反编译 |
| 版本核对 | `grep PILOT_BUILD_STAMP main.py` vs `git log -1` | Ctrl+F 搜 stamp |

---

## 五、监控与播报

### 5.1 钉钉推送

| 机器人 | 推送内容 | 触发 |
|---|---|---|
| Trading bot | 交易计划/晨检/EOD 报告/A-B 对照 | 各定时任务 |
| Strategy bot | 研究摘要/漂移告警/发现进展 | digest 18:30 |
| Data bot | 数据管道状态/湖健康 | pipeline 18:00 |

### 5.2 健康检查

| 检查项 | 方式 | 判定 |
|---|---|---|
| server 活 | curl 127.0.0.1:8000 → 401 | 401=鉴权正常应答=活 |
| 策略进程活 | audit INIT 行 stamp 最新 | 不匹配=错版 |
| 终端活 | 7002 /v3/strategies → 200 | 非 200=终端未开 |
| 湖新鲜 | 湖尾日期 vs 今天 | 滞后>1 交易日=管道断 |
| 看护 | gm_guard_cron.log 尾行 "ok": true | problems 非空=有异常 |

---

## 六、代码目录速查

```
E:\quanter\
├── strategies/neckline/          # 识别内核（C2 冻结）
│   ├── method_v0.py             #   形态检测
│   ├── signal.py                #   Signal 数据类
│   └── backtest.py              #   simulate_exit / dedup
├── backtest/models.py            # PositionModel + build_equity_curve
├── emquant/
│   ├── build_pilot.py           # 组装器（内核+快照+body → 单文件产物）
│   ├── pilot_body.py            # §2-§7 执行编排源码
│   ├── emquant_neckline_pilot.py     # 组装产物（主腿）
│   ├── emquant_neckline_pilot_exp.py # 组装产物（实验腿）
│   └── config/
│       ├── params_snapshot.json # 参数定稿快照
│       └── universe.json        # 300 只 universe
├── presentation/
│   ├── server/                  # FastAPI 后端
│   │   ├── main.py             #   app + lifespan + ops_sched
│   │   └── api/v1/              #   路由（gm/research/trading）
│   └── web/                     # Vue3 前端
│       └── src/
│           ├── views/           #   页面视图
│           ├── components/      #   UI 组件
│           └── api/             #   API facade
├── ops/                         # 运维工具族
│   ├── data_pipeline.py        #   管道 supervisor
│   ├── amihud_pct_writer.py    #   分位文件写入
│   ├── emquant_morning_check.py #  晨检
│   ├── emquant_audit_ingest.py  #  台账入库
│   ├── emquant_eod_report.py    #  EOD 报告
│   ├── emquant_ab_compare.py    #  双腿对照
│   ├── gm_terminal_guard.py     #  看护
│   └── manage_ops_schtasks.py   #  schtasks 管理（已退役，清退用）
├── data_lake/                   # parquet 数据湖（40+ 表）
├── data/tools/                  # 数据工具
│   ├── sync_daily_incremental.py # 增量采集
│   └── repair_amount_hole.py     # amount 断层修复
├── discovery/                   # 参数发现框架
├── diag/                        # 探索/诊断脚本族
├── tests/                       # 测试（tests/emquant/ + tests/server/ + ...）
├── docs/                        # 文档（本文件所在地）
└── .agents/skills/              # 操作 skill（goldminer-terminal 等）
```
