# Quanter —— 颈线法量化研究平台

## 1. 项目定位

Quanter 是一套面向 **A 股** 的量化研究平台，以**颈线法形态学（纯多头）**为主策略，配套参数训练、数据中心、实盘接入与远程协同：

- **主策略 · 颈线法**：颈线聚集带定位 + 压制时长验证 + 挂单回踩进场 + 分级止盈（scripts/neckline_method_v0.py + neckline_backtest.py）。回测引擎与策略解耦（strategies/ 包 + Strategy Protocol），颈线法为唯一活跃策略。
- **参数训练**：Parameter Lab 异步回测 + 参数扫描 + AI 分析闭环（GLM 驱动）。
- **数据中心**：Tushare 通用同步器（20+ 数据集，配置驱动），AKShare / JQData 辅助。
- **实盘接入**：东财 EMT 极速交易（MiniQMT 监管停用，按 env 路由 EMT/QMT gateway）。
- **后端引擎**：FastAPI（异步）+ 纯 Python 量化内核（Pandas/NumPy 显式向量化，拒绝黑盒）。
- **前端交互**：Vue 3 + Vite + ECharts，6 视图。
- **远程协同**：钉钉机器人经 dws dev connect 接入（对话 + 训练人审两职责）。

设计哲学遵循「**显式实现、拒绝黑盒**」：核心指标（形态识别、盈亏比、ATR、筹码分布等）均以平铺直叙的数学运算实现；策略、撮合、状态机均配像素级中文注释。

---

## 2. 后端分层架构（四层）

接口层只依赖门面、不摸模型内脏；模型层单向依赖；数据层被各层读。

```
quanter/
├─ 接口层 Presentation
│  ├─ web/                前端 6 视图(CaisenScreen/ParamLab/Dashboard/LiveCockpit/DataLake/Review)
│  ├─ server/api/v1/      HTTP 路由(caisen·data·macro·review·trading·training·logs + sse)
│  └─ server/services/    应用服务(编排用例，【只调门面】)
│
├─ 模型层 caisen/ (门面包)
│  ├─ facade.py           CaisenFacade —— 唯一对外契约(10 用例：scan/list_plans/approve/activate/...)
│  ├─ engines/            策略本体(patterns + plan + risk + config，纯逻辑·无 IO)
│  ├─ optimize/           参数优化(training_analyzer/loops_db/loop/dingtalk，可异步·可重跑)
│  ├─ advisor/            AI 决策(预留占位)
│  └─ infra/              待迁项(storage/execution/replay/viz，Step4 移出 caisen 包)
│
├─ 数据层
│  ├─ data/               取数(clients/fetcher/cleaner/tushare_sync/lake_reader，只放 .py)
│  ├─ data_lake/          parquet 存储(只放数据 + .syncing 状态，禁放 .py)
│  └─ config/             按层拆配置(8 子文件包，非上帝文件，dotenv 包入口)
│
├─ 横切
│  ├─ infra/              通知(notifier，从 core 解散迁入)
│  ├─ factors/            纯计算因子(atr)
│  └─ viz/                可视化
│
├─ 执行编排 trading/       emt/qmt/mock gateway + order_state + risk_shield
└─ core/                  解散中(macro_regime 暂留，最终归模型层·宏观域)
```

**依赖铁律**：接口层 → facade → engines；optimize / advisor / infra 单向依赖 engines；**engines 绝不反向** import 它们。`caisen/facade.py` 是模型层唯一对外契约，server 零穿透 caisen 内部——caisen 内部任意重组对 server 不可见。

> **当前态**：后端分层重构（Step 1/2/3）已完成——`config.py`(857 行)拆为 `config/` 8 子文件包、`core/` 杂物间解散（indicator→factors、notifier→infra，保留转发垫片）、`caisen/` 立 facade 门面 + 分 engines/optimize/infra/advisor 四子包。执行编排层（trading + caisen/infra 双 risk 合并、replay_worker 反向依赖收口）为后续 **Step 4**。

---

## 3. 环境依赖

### 3.1 Python 后端

```bash
pip install -r requirements.txt
```

主要依赖：`fastapi`、`uvicorn`、`pandas`、`numpy`、`tushare`、`akshare`、`jqdatasdk`、`celery`、`redis`、`aiohttp`、`pyarrow`、`fastparquet`、`pydantic`、`yfinance` 等。实盘 EMT 接入用 Python 3.10 venv（`.venv310`）。

### 3.2 前端

```bash
cd web && npm install
```

---

## 4. `.env` 配置

参照 `.env.example` 创建 `.env`：

```dotenv
# 数据源
TUSHARE_TOKEN=                 # Tushare Pro（数据中心主源）
JQDATA_USERNAME=               # JQData 分钟级（高频微观动量）
JQDATA_PASSWORD=
FRED_API_KEY=                  # 宏观（可选）
ALPHA_VANTAGE_API_KEY=         # 美债/外盘（可选）

# Celery 因子沙盒（可选）
REDIS_URL=redis://localhost:6379/0
CELERY_EXPLORER_QUEUE=explorer

# 钉钉（DingTalkNotifier webhook 告警/报告回显；机器人接入见 scripts/start_dingtalk_bots.md）
DINGTALK_WEBHOOK=
DINGTALK_SECRET=
```

> **优雅降级**：任一凭证缺失，对应模块不抛异常阻断启动——数据湖缺失则离线模式（查询返空）、JQData 缺失则分钟级返空、钉钉缺失则告警仅写日志、Redis 缺失则 Celery 降级同步。各模块独立可用，按拥有的凭证增量启用。

---

## 5. 数据中心同步

**Tushare 通用同步器**（配置驱动：新增数据集只需在 `config/registry.py` 注册一行，不再为每个接口写同步脚本）：

```bash
# 全量同步（quick/slow 批）
python scripts/sync_all_tushare.py

# 单数据集
python scripts/sync_tushare.py <dataset_key>
```

数据集资产元信息（source / market / granularity / script / freshness）的**单一真相源** = `config/registry.py` 的 `DATASET_REGISTRY` + `TUSHARE_DATASETS`，前端 `DataLakeView` 经 `/api/v1/data/datasets` 反射本表。

辅助数据流（历史保留）：

```bash
python scripts/sync_macro_credit.py    # 宏观信贷（CreditRegime 输入）
python scripts/sync_sector_daily.py    # 板块 + 活跃股日线
python scripts/sync_jqdata_1min.py     # JQData 分钟级（配额双机制防封）
python scripts/sync_binance_vision.py  # (可选) 加密沙盒，7x24 极端市场测试
```

- **前视红线**：财报类 `date_col=ann_date`（公告日），**绝不用** `end_date`（报告期）——报告期早于公告日会导致前视偏差。
- **JQData 防暴雷**：配额双机制（手动计数 + `get_query_count` 校准，spare < 5 万即停 + 钉钉告警）+ 断点续传。
- **多湖读取**：`DataLakeReader` 按 `LAKE_CONFIG["lakes"]` 多湖缓存到内存，`get_*(lake=)` 按 key 查询，毫秒级截面/时序切片。

---

## 6. 启动后端与前端

### 6.1 后端

```bash
uvicorn server.main:app --reload
```

默认 `http://127.0.0.1:8000`，API 文档 `/docs`。启动期按 `LAKE_CONFIG["lakes"]` 加载存在的湖，缺失则离线降级。

### 6.2 前端（6 视图）

```bash
cd web && npm run dev
```

- `/caisen` —— **形态扫描**：蔡森形态候选 + 颈线/盈亏比/止损可视化。
- `/param-lab` —— **参数训练**：异步回测 + 参数扫描 + AI 分析。
- `/dashboard` —— **宏观驾驶舱**：CreditRegime 状态卡 + 社融/M1M2/DR007。
- `/live` —— **实盘驾驶舱**：EMT 网关持仓/订单/风控。
- `/data-lake` —— **数据中心**：Tushare 数据集资产表 + 同步触发。
- `/review` —— **审核**：候选计划 approve/reject + 钉钉远程审核。

### 6.3 蔡森 CLI（离线入口）

```bash
python -m caisen --help
```

形态学流水线离线入口（扫描/回放，不经 HTTP），供脚本化批跑。

### 6.4 Celery Worker（因子沙盒，可选）

```bash
celery -A server.celery_app worker -Q explorer -l info
```

需先启动 Redis；无 Redis 时因子沙盒降级为同步执行（CPU 探针 > 80% 拒绝调度）。

---

## 7. 业务模块速览

| 模块 | 视图 / 入口 | 说明 |
|------|-------------|------|
| **蔡森形态学** | CaisenScreen / Review | 多空转折形态学（纯多头），12 招结构，facade 10 用例（scan → approve → activate → replay） |
| **参数训练** | ParamLab | 异步回测 + 参数扫描 + AI 分析闭环（GLM），训练循环可重跑 |
| **AI training loop** | training API | GLM 驱动的参数训练闭环 + 钉钉远程审核，训练轮次落 SQLite |
| **数据中心** | DataLake | Tushare 20+ 数据集，registry 反射 + 同步状态（healthy/stale） |
| **宏观驾驶舱** | Dashboard | CreditRegime 信贷周期状态机 + 宏观三联指标 |
| **实盘接入** | LiveCockpit | 东财 EMT 极速交易（MiniQMT 监管停用），gateway 按 env 路由 |
| **钉钉机器人** | `scripts/start_dingtalk_bots.md` | dws dev connect 统一接入（对话 + 训练人审两职责） |

---

## 8. 设计文档与计划

specs（设计）/ plans（实现计划）均在 `docs/superpowers/`，按时间倒序。近期主线：

- **后端分层重构**：[design](docs/superpowers/specs/2026-07-15-backend-layering-refactor-design.md) / [plan](docs/superpowers/plans/2026-07-15-backend-layering-refactor.md)
- **蔡森 AI training loop**：[design](docs/superpowers/specs/2026-07-14-caisen-ai-training-loop-design.md) / [plan](docs/superpowers/plans/2026-07-15-caisen-ai-training-loop.md)
- **数据中心与数据治理**：[design](docs/superpowers/specs/2026-07-14-data-center-and-data-governance-design.md)
- **Tushare 采集（股/ETF/宏观）**：[stock](docs/superpowers/plans/2026-07-14-tushare-stock-collection.md) / [etf](docs/superpowers/plans/2026-07-14-tushare-etf-collection.md) / [macro](docs/superpowers/plans/2026-07-14-tushare-macro-collection.md)
- **Parameter Lab**：[design](docs/superpowers/specs/2026-07-14-param-lab-design.md) / [plan](docs/superpowers/plans/2026-07-14-param-lab.md)
- **蔡森形态注册表**：[design](docs/superpowers/specs/2026-07-13-caisen-pattern-registry-design.md) / [plan](docs/superpowers/plans/2026-07-13-caisen-pattern-registry.md)
- **蔡森回放异步化**：[design](docs/superpowers/specs/2026-07-13-caisen-replay-async-design.md) / [plan](docs/superpowers/plans/2026-07-13-caisen-replay-async.md)
- **钉钉 claude 桥**：[design](docs/superpowers/specs/2026-07-12-dingtalk-claude-bridge-design.md) / [plan](docs/superpowers/plans/2026-07-12-dingtalk-claude-bridge.md)
- **EMT 实盘接入**：[design](docs/superpowers/specs/2026-07-08-emt-broker-access-design.md)

策略方法论权威参考：[`颈线法形态学策略 · 完整技术文档`](docs/neckline-method.md)（颈线聚集带定位 + 压制验证 + 挂单回踩进场 + 分级止盈，当前唯一活跃策略）。

执行轨迹（每 Task 的实现/审查/修复证据）见 `.superpowers/sdd/progress.md`。

---

## 9. 钉钉机器人（dws 统一接入）

三个钉钉机器人，按职责分两类接入：

**对话 / 人审类（dws dev connect 常驻·入站）**：
- **yzzhanCli通用**（对话）：@机器人 → dws → Claude Code 对话（`--channel claudecode`，带 `--allowed-users` 身份闸 + `--agent-approval-mode ask` 审批闸）。
- **yzzhan参数优化**（训练人审）：@机器人 → dws → `scripts/dingtalk_review_bridge.py` → `POST /api/v1/training/review` → training loop 人审关卡。

**单向播报类（dws send-by-bot 出站 + schtasks 定时·无入站）**：
- **行情播报**（每日播报）：schtasks 每日 19:00 触发 `python -m broadcast` → 取 `data_lake` 行情 → 纯 pandas 聚合 → 模板 Markdown → `dws chat message send-by-bot` 推群。大盘 8 宽基 + 板块榜 + 主力资金 + 龙虎榜，幂等去重（周末/节假日不播）。建号/拉群见 [`scripts/setup_broadcast_bot.md`](scripts/setup_broadcast_bot.md)，定时注册见 [`scripts/setup_broadcast_schtasks.md`](scripts/setup_broadcast_schtasks.md)。

对话/人审两机器人的启动步骤（两个 dws dev connect 常驻 + 一个 uvicorn）详见 [`scripts/start_dingtalk_bots.md`](scripts/start_dingtalk_bots.md)；播报机器人无常驻进程（schtasks 触发即跑）。

---

## 许可与贡献

本项目为个人量化研究工程，代码与策略仅供学习交流。贡献请遵循 `CLAUDE.md` 的「全中文 + 显式实现 + 极端边界拷问」工程协议。
