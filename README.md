# Quanter —— A 股 ETF 量化回测与策略研究平台

## 1. 项目定位

Quanter 是一套面向 **A 股 ETF 组合** 的量化交易研究平台，技术栈由三部分组成：

- **后端引擎**：FastAPI（异步 Web 框架）+ 纯 Python 量化内核（Pandas/NumPy 显式向量化实现）。
- **前端交互**：Vue 3（组合式 API）+ Vite，提供策略编排、回测配置与结果可视化。
- **离线任务**：Celery（Redis 作 broker）承载因子沙盒等耗时计算。

设计哲学遵循「**显式实现、拒绝黑盒**」：所有核心指标（移动均线、Hurst 指数、横截面动量、波动率调整、IC 分析、Greeks 等）均以清晰、平铺直叙的数学运算或向量化代码实现，不引入过度封装的重型第三方量化框架；策略逻辑、撮合规则与状态机均配有像素级中文注释，说明其物理意图与边界。

---

## 2. 环境依赖安装

### 2.1 Python 后端依赖

```bash
pip install -r requirements.txt
```

主要依赖：`fastapi`、`uvicorn`、`pandas`、`numpy`、`tushare`、`celery`、`redis`、`requests`、`yfinance`、`hmmlearn`、`plotly` 等。

### 2.2 前端依赖

```bash
cd web
npm install
```

前端构建工具为 Vite，UI 基于 Vue 3 + 组合式 API。

---

## 3. `.env` 配置

在项目根目录参照 `.env.example` 创建 `.env` 文件，按需填入以下凭证：

```dotenv
# ============ 数据湖（Epic 1）============
DATA_LAKE_PATH=data_lake/a_shares_daily.parquet
TUSHARE_TOKEN=

# ============ GLM 情感（Epic 2）============
ZHIPU_API_KEY=
ZHIPU_BASE_URL=https://open.bigmodel.cn/api/paas/v4/
ZHIPU_MODEL=glm-4-flash

# ============ Celery（Epic 3）============
REDIS_URL=redis://localhost:6379/0
CELERY_EXPLORER_QUEUE=explorer

# ============ 宏观 + 钉钉（Epic 5）============
ALPHA_VANTAGE_API_KEY=
DINGTALK_WEBHOOK=
DINGTALK_SECRET=
```

各字段含义：

| 变量 | 用途 |
|------|------|
| `DATA_LAKE_PATH` | 数据湖 Parquet 落盘路径（全市场前复权日线） |
| `TUSHARE_TOKEN` | Tushare Pro 接口令牌，用于数据湖同步与实时行情 |
| `ZHIPU_API_KEY` | 智谱 GLM 大模型 API Key，驱动情感因子分析 |
| `ZHIPU_BASE_URL` / `ZHIPU_MODEL` | GLM 服务地址与模型名（默认 `glm-4-flash`） |
| `REDIS_URL` | Redis 连接串，Celery broker 与缓存共用 |
| `CELERY_EXPLORER_QUEUE` | 因子沙盒独占消费队列名（默认 `explorer`） |
| `ALPHA_VANTAGE_API_KEY` | Alpha Vantage 令牌，拉取美债收益率等宏观指标 |
| `DINGTALK_WEBHOOK` / `DINGTALK_SECRET` | 钉钉机器人 webhook 与加签密钥，风控告警推送 |

> **优雅降级**：当上述任意凭证缺失（留空）时，对应模块不会抛异常阻断启动——数据湖缺失则回退至在线 fetcher、GLM 缺失则情感因子返回中性、宏观缺失则跳过美债锚定、钉钉缺失则告警仅写日志、Redis 缺失则 Celery 任务转同步执行。各模块独立可用，按你拥有的凭证增量启用即可。

---

## 4. 数据湖同步

```bash
python scripts/sync_data_lake.py --years 10
```

该脚本拉取 **全市场（剔除 ST/退市）过去 N 年的日线【前复权】OHLCV**：

- **前复权口径**：使用 `pro_bar(adj='qfq')` 重算历史价，确保除权除息无断崖跳变（与在线 fetcher 的 `pro.daily()` 不复权原始价严格分离，避免拼接失真）。
- **断点续传**：每标的独立落 shard（`data_lake/shards/{ts_code}.parquet`），已存在即跳过。全市场数千只标的逐只拉取耗时数小时，中途因限频或断线失败时重跑从断点继续，不会从头再来。
- **限频防护**：复用 `data/resilience.py` 的令牌桶（匀速补令牌）+ 熔断器（连续失败 OPEN 期间停止触达），防止连环超限被封 IP/账号；空数据（停牌/退市）属正常业务态，跳过不中断。

---

## 5. 启动后端与前端

### 5.1 后端（FastAPI）

```bash
uvicorn server.main:app --reload
```

默认监听 `http://127.0.0.1:8000`，API 文档见 `/docs`（Swagger UI）。

### 5.2 前端（Vue 3 + Vite）

```bash
cd web
npm run dev
```

默认开发服务器监听 `http://127.0.0.1:5173`，已配置代理转发至后端。

---

## 6. Celery Worker（因子沙盒）

因子沙盒的批量计算（如全市场横截面动量扫描、Hurst 指数网格）由 Celery 异步承载，需先启动 Redis，再起 worker：

```bash
celery -A server.celery_app worker -Q explorer -l info
```

- `-Q explorer`：独占消费 `explorer` 队列，与其它任务隔离，避免长耗时计算饿死后端 API。
- `-l info`：info 级日志便于观察任务生命周期。
- 若本地无 Redis，因子沙盒会降级为同步执行（仅失去并发能力，功能不丢）。

---

## 7. 五大工业级 Epic 模块速览

| Epic | 模块 | 一句话说明 |
|------|------|-----------|
| **Epic 1** | 极速数据湖 | 全市场前复权日线 Parquet 落盘，单例 `DataLakeReader` 提供毫秒级横截面/时序切片，在线 fetcher 与离线湖双通道互补。 |
| **Epic 2** | GLM 情感因子 | 调用智谱 `glm-4-flash` 对财经新闻做情感分析，输出 `SentimentResult(score, reasoning)`，每日得分接入因子库；缺 Key 时回退中性。 |
| **Epic 3** | 因子沙盒（Celery） | `explorer` 队列承载因子网格批量计算（IC 分析、分位数回测、Hurst 指数等），Redis broker 异步化，长任务不阻塞 API。 |
| **Epic 4** | SSE 实时回测流 | `BacktestEngine.run(event_emitter=...)` 透传进度/成交/风控事件至 SSE 通道，前端流式渲染回测过程而非轮询。 |
| **Epic 5** | 宏观 + 钉钉风控网关 | Alpha Vantage 拉取美债收益率做宏观锚定，钉钉机器人 `fire_and_forget` 推送风控告警（涨跌停、流动性枯竭、敞口越限等）。 |

---

## 8. 设计文档与计划

完整的架构设计与实施计划见以下文档：

- **设计规格**：[`docs/superpowers/specs/2026-07-01-quanter-industrial-design.md`](docs/superpowers/specs/2026-07-01-quanter-industrial-design.md)
- **实施计划**：[`docs/superpowers/plans/2026-07-01-quanter-industrial.md`](docs/superpowers/plans/2026-07-01-quanter-industrial.md)

上述文档涵盖五大 Epic 的需求拆解、数据流图、接口契约、类型一致性约束与回归红线。

---

## 许可与贡献

本项目为个人量化研究工程，代码与策略仅供学习交流。如需贡献，请遵循 `CLAUDE.md` 中的「全中文 + 显式实现 + 极端边界拷问」工程协议。
