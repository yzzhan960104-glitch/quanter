# Quanter —— 颈线法量化研究平台

## 1. 项目定位

Quanter 是一套面向 **A 股** 的量化研究平台,以**颈线法形态学(纯多头)**为主策略,配套参数发现引擎、实验版本中心、数据中心、实盘接入与远程协同:

- **主策略 · 颈线法**:颈线聚集带定位 + 压制时长验证 + 挂单回踩进场 + 分级止盈(`strategies/neckline/`)。策略本体与回测/执行解耦,经 `Strategy` Protocol 注入。
- **参数发现引擎 · discovery**:Plan 1-4 闭环(L0-L5 可信度),快照冻结 → 2025/2026 holdout 嵌套 OOS → 分层裁判 → Sobol/TPE 搜索 → 帕累托前沿 → daemon 生产入口 → 冠军 publish 至 experiment。CLI `python -m discovery {oos,verify,daemon,publish}`。
- **实验版本中心 · experiment**:实盘下单的策略版本配置中心,`resolve_active()` 给 scan 发放当前生效的 `(strategy_name, params, weight)` 列表,支持版本切换 + 审计日志 + 权重校验。
- **回测引擎 · backtest**:`replay` 策略中立回测器 + 异步任务队列(worker/scheduler/tasks_db)+ 参数优化(`optimize/training_*`)+ 回测撮合模拟器(MockBroker)。**单向依赖铁律**:只依赖 `trading.compute`(离场纯函数)+ `strategies` + `data`,严禁触碰 `trading.engine`/`broker`(回测求变、交易求稳,分离防污染)。
- **数据中心**:Tushare 通用同步器(20+ 数据集,配置驱动),AKShare / JQData 辅助。
- **实盘接入**:东财 EMT 极速交易(MiniQMT 监管停用,按 env 路由 EMT/QMT gateway)。
- **后端引擎**:FastAPI(异步)+ 纯 Python 量化内核(Pandas/NumPy 显式向量化,拒绝黑盒)。
- **前端交互**:Vue 3 + Vite + ECharts,6 视图。
- **远程协同**:钉钉机器人经 dws dev connect 接入(对话 + 训练人审两职责)。

设计哲学遵循「**显式实现、拒绝黑盒**」:核心指标(形态识别、盈亏比、ATR、筹码分布等)均以平铺直叙的数学运算实现;策略、撮合、状态机均配像素级中文注释。

> **架构演进注**:本平台早期以 `caisen/` 门面包组织(见 `2026-07-15-backend-layering-refactor-design.md`)。`2026-07-16 step4-execution-layer` 起执行链重组,`execution/` 包解散,caisen 门面在 Task 1.3 退役——回测基础设施归 `backtest/`、执行编排归 `trading/`、券商网关归 `broker/`、形态执行链(ExecutionEngine)删除。当前 README 反映 `2026-07-22 layer2-decoupling` 之后的真实分层。
>
> **T7 架构治理(2026-07-26)**:core/factors/viz 三相死代码删除;server/core/ 正名 server/http/(消除 core 命名歧义);web/+server/ 收编进 presentation/ 伞盖(README §2 接口层语义落地文件夹)。详见 `docs/superpowers/specs/2026-07-26-arch-t7-subtraction-and-presentation-design.md`。

---

## 2. 后端分层架构(四层 + 发现/回测两条副线)

接口层经应用服务编排各域;模型/执行/数据层单向依赖;发现与回测为两条策略副线。

```
quanter/
├─ 接口层 presentation/         web/+server/ 收编(README §2 语义落地)
│  ├─ web/                      前端 6 视图(CaisenScreen/ParamLab/Dashboard/LiveCockpit/DataLake/Review)
│  └─ server/                   FastAPI 应用
│     ├─ api/v1/                HTTP 路由(caisen·data·macro·review·trading·training·logs + sse)
│     ├─ services/              应用服务(编排用例,聚合各域)
│     ├─ schemas/               请求/响应 DTO
│     ├─ http/                  HTTP 运行时基建(auth/config/_responses,原 server/core/ 正名)
│     └─ main.py                app 装配
│
├─ 执行编排层 trading/          实盘执行引擎 + 状态机(compute/io/orchestrate/state/types + engine)
├─ 网关层 broker/               券商网关抽象(base/mock/qmt/qmt_quote)
├─ 策略层 strategies/           纯叶子(base/registry/signal + neckline/)
├─ 回测副线 backtest/           策略中立回测器(replay/worker/scheduler/optimize/mock_broker)
├─ 发现副线 discovery/          参数发现引擎(Plan 1-4 · L0-L5 闭环)
├─ 实验中心 experiment/         实盘版本配置中心(models/resolver/store)
├─ 数据层
│  ├─ data/                     取数(clients/fetcher/cleaner/lake_reader,只放 .py)
│  ├─ data_lake/                parquet 存储(只放数据,禁放 .py,不入库)
│  └─ config/                   按层拆配置(8 子文件包)
└─ 横切
   ├─ infra/                    通知(notifier)+ LLM(llm/glm)
   └─ broadcast/                行情播报(brief/push/name_resolver)
```

**依赖铁律(实证)**:

- `strategies / infra / experiment / config` 为**纯叶子**(零下游扇出),是健康的底层/横切。
- `backtest → strategies / trading.compute / data`(单向,不碰 broker/engine)——已实证合规。
- `discovery → strategies / experiment / infra`(发现链自洽内聚)。
- `trading → broker`(执行层调网关,正向);`broker → trading.compute.types`(**疑似反向**,Layer2 follow-up #4c 类型迁移遗留,待收尾)。
- `trading → presentation.server.services`(5 处,执行层倒挂接口层);`trading/protocols.py` 定义了 `ExecutionExecutor` Protocol 拟反转此依赖,但**当前为孤儿契约、未接通**——已记入架构债。
- `presentation.server` 经 services 扇出 7 个域(原 caisen 门面收敛已随退役消失)。

> 详细跨包依赖矩阵与层次违规清单见 `docs/superpowers/specs/2026-07-22-layer2-decoupling-design.md`。

---

## 3. 环境依赖

### 3.1 Python 后端

```bash
pip install -r requirements.txt
```

主要依赖:`fastapi`、`uvicorn`、`pandas`、`numpy`、`tushare`、`akshare`、`jqdatasdk`、`celery`、`redis`、`aiohttp`、`pyarrow`、`fastparquet`、`pydantic`、`yfinance` 等。实盘 EMT 接入用 Python 3.10 venv(`.venv310`)。

### 3.2 前端

```bash
cd presentation/web && npm install
```

---

## 4. `.env` 配置

参照 `.env.example` 创建 `.env`:

```dotenv
# 数据源
TUSHARE_TOKEN=                 # Tushare Pro(数据中心主源)
JQDATA_USERNAME=               # JQData 分钟级(高频微观动量)
JQDATA_PASSWORD=
FRED_API_KEY=                  # 宏观(可选)
ALPHA_VANTAGE_API_KEY=         # 美债/外盘(可选)

# Celery 因子沙盒(可选)
REDIS_URL=redis://localhost:6379/0
CELERY_EXPLORER_QUEUE=explorer

# 钉钉(DingTalkNotifier webhook 告警/报告回显;机器人接入见 scripts/start_dingtalk_bots.md)
DINGTALK_WEBHOOK=
DINGTALK_SECRET=
```

> **优雅降级**:任一凭证缺失,对应模块不抛异常阻断启动——数据湖缺失则离线模式(查询返空)、JQData 缺失则分钟级返空、钉钉缺失则告警仅写日志、Redis 缺失则 Celery 降级同步。各模块独立可用,按拥有的凭证增量启用。

---

## 5. 数据中心同步

**Tushare 通用同步器**(配置驱动:新增数据集只需在 `config/registry.py` 注册一行,不再为每个接口写同步脚本):

```bash
# 全量同步(quick/slow 批)
python data/tools/sync_all_tushare.py

# 单数据集
python data/tools/sync_tushare.py <dataset_key>
```

数据集资产元信息(source / market / granularity / script / freshness)的**单一真相源** = `config/registry.py` 的 `DATASET_REGISTRY` + `TUSHARE_DATASETS`,前端 `DataLakeView` 经 `/api/v1/data/datasets` 反射本表。

辅助数据流(历史保留):

```bash
python data/tools/sync_macro_credit.py    # 宏观信贷(CreditRegime 输入)
python data/tools/sync_sector_daily.py    # 板块 + 活跃股日线
python data/tools/sync_jqdata_1min.py     # JQData 分钟级(配额双机制防封)
```

- **前视红线**:财报类 `date_col=ann_date`(公告日),**绝不用** `end_date`(报告期)——报告期早于公告日会导致前视偏差。
- **JQData 防暴雷**:配额双机制(手动计数 + `get_query_count` 校准,spare < 5 万即停 + 钉钉告警)+ 断点续传。
- **多湖读取**:`DataLakeReader` 按 `LAKE_CONFIG["lakes"]` 多湖缓存到内存,`get_*(lake=)` 按 key 查询,毫秒级截面/时序切片。

---

## 6. 启动后端与前端

### 6.1 后端

```bash
uvicorn presentation.server.main:app --reload
```

默认 `http://127.0.0.1:8000`,API 文档 `/docs`。启动期按 `LAKE_CONFIG["lakes"]` 加载存在的湖,缺失则离线降级。

### 6.2 前端(6 视图)

```bash
cd presentation/web && npm run dev
```

- `/caisen` —— **形态扫描**:颈线候选 + 颈线/盈亏比/止损可视化。
- `/param-lab` —— **参数训练**:异步回测 + 参数扫描 + AI 分析。
- `/dashboard` —— **驾驶舱**:(宏观 CTA / CreditRegime 已于 2026-07 下线;`/macro/sector/flow` 板块资金流端点保留,前端视图待适配)
- `/live` —— **实盘驾驶舱**:EMT 网关持仓/订单/风控。
- `/data-lake` —— **数据中心**:Tushare 数据集资产表 + 同步触发。
- `/review` —— **审核**:候选计划 approve/reject + 钉钉远程审核。

### 6.3 参数发现引擎 CLI(离线入口)

```bash
python -m discovery oos       # 当前冠军 2025/2026 holdout 嵌套 OOS,固化去偏水平(落 SQLite)
python -m discovery verify    # 复核指定 trial 的快照/引擎双指纹一致性
python -m discovery daemon    # L4 生产入口:多轮搜索 + 帕累托收敛 + ≥N 天硬闸后 publish
python -m discovery publish   # 手动把冠军 publish 为 experiment DRAFT 版本
```

> **daemon 纪律**(运维红线):daemon 进程必须**串行**单实例运行(多实例同跑会产生重复参数全去重、ρ 永远 0 的伪收敛);schtasks 定时任务**只汇报状态、不自动拉起 daemon**(人工确认环境后再启);每轮 seed 按 `42 + run_count` 派生,避免固定 seed 致夜跑/重跑产相同参数。

---

## 7. 业务模块速览

| 模块 | 视图 / 入口 | 说明 |
|------|-------------|------|
| **参数发现引擎** | `python -m discovery` | Plan 1-4 闭环:L0 快照冻结 → L1 holdout OOS 裁判 → L2 采样并发 → L3 帕累托/DSR/TPE → L4 daemon → L5 publish 冠军至 experiment |
| **实验版本中心** | experiment API | 实盘策略版本配置中心:版本切换 + 权重校验 + 审计日志,`resolve_active()` 发生效配置 |
| **回测引擎** | ParamLab / CLI | `replay` 策略中立回测 + 异步任务队列 + 参数优化(training_loop),回测/实盘共用 `check_exit` 杜绝决策分叉 |
| **颈线法策略** | CaisenScreen | 多空转折形态学(纯多头),颈线聚集带 + 压制验证 + 挂单回踩 + 分级止盈,当前唯一活跃策略 |
| **数据中心** | DataLake | Tushare 20+ 数据集,registry 反射 + 同步状态(healthy/stale) |
| ~~宏观驾驶舱~~ | Dashboard | (宏观 CTA / CreditRegime 已于 2026-07 下线;板块资金流端点保留,前端待适配) |
| **实盘接入** | LiveCockpit | 东财 EMT 极速交易(MiniQMT 监管停用),gateway 按 env 路由 |
| **行情播报** | `python -m broadcast` | 每日 19:00 schtasks 触发,大盘 8 宽基 + 板块榜 + 主力资金 + 龙虎榜,幂等去重 |
| **钉钉机器人** | `scripts/start_dingtalk_bots.md` | dws dev connect 统一接入(对话 + 训练人审两职责) |

---

## 8. 设计文档与计划

specs(设计)/ plans(实现计划)均在 `docs/superpowers/`,按时间倒序。近期主线(反映当前真实架构):

- **参数发现引擎(当前主线)**:[design](docs/superpowers/specs/2026-07-23-param-discovery-engine-design.md) / [Plan1 L0-L1](docs/superpowers/plans/2026-07-24-discovery-credibility-l0-l1.md) / [Plan2 L2-L3](docs/superpowers/plans/2026-07-24-discovery-l2-l3-search.md) / [Plan3 L3-L4](docs/superpowers/plans/2026-07-24-discovery-l3-l4-convergence.md) / [Plan4 L4-L5](docs/superpowers/plans/2026-07-24-discovery-plan4-l4-daemon-l5-publish.md)
- **实验系统**:[design](docs/superpowers/specs/2026-07-22-experiment-system-design.md) / [plan](docs/superpowers/plans/2026-07-22-experiment-system.md)
- **Layer2 解耦(trading/broker/backtest 三层定型)**:[design](docs/superpowers/specs/2026-07-22-layer2-decoupling-design.md) / [plan](docs/superpowers/plans/2026-07-22-layer2-decoupling-plan.md) / [followup](docs/superpowers/specs/2026-07-23-layer2-followup-design.md)
- **自动交易引擎**:[design](docs/superpowers/specs/2026-07-21-auto-trading-engine-design.md) / [plan](docs/superpowers/plans/2026-07-21-auto-trading-engine.md) / [rehearsal](docs/superpowers/specs/2026-07-23-auto-trading-rehearsal-design.md)
- **Step4 执行层(caisen 退役 · execution 包解散)**:[design](docs/superpowers/specs/2026-07-16-step4-execution-layer-design.md) / [plan](docs/superpowers/plans/2026-07-16-step4-execution-layer.md)
- **后端分层重构(caisen 门面·历史)**:[design](docs/superpowers/specs/2026-07-15-backend-layering-refactor-design.md) / [plan](docs/superpowers/plans/2026-07-15-backend-layering-refactor.md)
- **数据中心与数据治理**:[design](docs/superpowers/specs/2026-07-14-data-center-and-data-governance-design.md)
- **Tushare 数据快照扩容**:[design](docs/superpowers/specs/2026-07-25-tushare-data-snapshot-design.md) / [completion](docs/superpowers/specs/2026-07-25-tushare-data-snapshot-completion.md)
- **钉钉 claude 桥 / ops cockpit**:[bridge design](docs/superpowers/specs/2026-07-12-dingtalk-claude-bridge-design.md) / [ops design](docs/superpowers/specs/2026-07-21-dingtalk-ops-cockpit-design.md)
- **EMT 实盘接入**:[design](docs/superpowers/specs/2026-07-08-emt-broker-access-design.md)

策略方法论权威参考:

- [`颈线法形态学策略 · 完整技术文档`](docs/neckline-method.md)(颈线聚集带定位 + 压制验证 + 挂单回踩进场 + 分级止盈,当前唯一活跃策略)
- [`蔡森形态学方法论摘要`](docs/caisen-methodology-summary.md)(历史参考;caisen 门面包已退役,方法论沉淀为策略本体)

执行轨迹(每 Task 的实现/审查/修复证据)见 `.superpowers/sdd/progress.md`。

---

## 9. 钉钉机器人(dws 统一接入)

三个钉钉机器人,按职责分两类接入:

**对话 / 人审类(dws dev connect 常驻·入站)**:
- **yzzhanCli 通用**(对话):@机器人 → dws → Claude Code 对话(`--channel claudecode`,带 `--allowed-users` 身份闸 + `--agent-approval-mode ask` 审批闸)。
- **yzzhan 参数优化**(训练人审):@机器人 → dws → `infra/tools/dingtalk_review_bridge.py` → `POST /api/v1/training/review` → training loop 人审关卡。

**单向播报类(dws send-by-bot 出站 + schtasks 定时·无入站)**:
- **行情播报**(每日播报):schtasks 每日 19:00 触发 `python -m broadcast` → 取 `data_lake` 行情 → 纯 pandas 聚合 → 模板 Markdown → `dws chat message send-by-bot` 推群。大盘 8 宽基 + 板块榜 + 主力资金 + 龙虎榜,幂等去重(周末/节假日不播)。建号/拉群见 [`scripts/setup_broadcast_bot.md`](scripts/setup_broadcast_bot.md),定时注册见 [`scripts/setup_broadcast_schtasks.md`](scripts/setup_broadcast_schtasks.md)。

对话/人审两机器人的启动步骤(两个 dws dev connect 常驻 + 一个 uvicorn)详见 [`scripts/start_dingtalk_bots.md`](scripts/start_dingtalk_bots.md);播报机器人无常驻进程(schtasks 触发即跑)。

---

## 许可与贡献

本项目为个人量化研究工程,代码与策略仅供学习交流。贡献请遵循 `CLAUDE.md` 的「全中文 + 显式实现 + 极端边界拷问」工程协议。
