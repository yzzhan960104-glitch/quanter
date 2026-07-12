# Quanter —— 自上而下宏观 CTA 量化研究平台

## 1. 项目定位

Quanter 是一套面向 **A 股** 的「**自上而下信贷指引 + 纯血高频微观动量**」量化研究平台：

- **架构主线**：宏观信贷（月频）→ 板块资金（日频）→ 微观量价（分钟频）层层递进的漏斗型数据湖；`CreditRegime` 作为全系统一票否决的宏观锚。
- **后端引擎**：FastAPI（异步）+ 纯 Python 量化内核（Pandas/NumPy 显式向量化，拒绝黑盒）。
- **前端交互**：Vue 3 + Vite + ECharts，双路由（`/` 回测终端 + `/dashboard` 宏观驾驶舱）。
- **离线任务**：Celery（Redis broker）承载因子沙盒等耗时计算。

设计哲学遵循「**显式实现、拒绝黑盒**」：所有核心指标（CreditRegime、ATR/Risk Parity、赫斯顿指数、横截面动量、IC 分析等）均以平铺直叙的数学运算实现；策略、撮合、状态机均配像素级中文注释。**本轮重构摒弃了外部大模型（LLM）与基本面财务因子，Tushare 数据流保留 dormant 备用，新数据流全走 AKShare + JQData。**

---

## 2. 环境依赖安装

### 2.1 Python 后端依赖

```bash
pip install -r requirements.txt
```

主要依赖：`fastapi`、`uvicorn`、`pandas`、`numpy`、`akshare`、`jqdatasdk`、`thriftpy2`、`celery`、`redis`、`aiohttp`、`pyarrow`、`fastparquet`、`yfinance`、`hmmlearn` 等。

### 2.2 前端依赖

```bash
cd web && npm install
```

---

## 3. `.env` 配置

参照 `.env.example` 创建 `.env`：

```dotenv
# 数据湖
DATA_LAKE_PATH=data_lake/a_shares_daily.parquet
TUSHARE_TOKEN=                 # dormant 备用（新数据流走 AKShare，不调用 Tushare）

# JQData 分钟级（高频微观动量源，试用账号 100 万条/天）
JQDATA_USERNAME=
JQDATA_PASSWORD=

# Celery 因子沙盒
REDIS_URL=redis://localhost:6379/0
CELERY_EXPLORER_QUEUE=explorer

# 钉钉风控告警
DINGTALK_WEBHOOK=
DINGTALK_SECRET=

# 宏观（上轮保留，可选）
ALPHA_VANTAGE_API_KEY=
FRED_API_KEY=
```

> **优雅降级**：任一凭证缺失，对应模块不抛异常阻断启动——数据湖缺失则离线模式（查询返空）、JQData 缺失则分钟级返空、钉钉缺失则告警仅写日志、Redis 缺失则 Celery 降级同步。各模块独立可用，按拥有的凭证增量启用。

---

## 4. 数据湖同步（自上而下四级漏斗）

数据流：**宏观信贷（月频）→ 板块两融（日频）→ 50 只活跃股日线（日频）→ 这 50 只分钟级（分钟频）**。

```bash
# 1. 宏观信贷：AKShare 社融/M1M2剪刀差/DR007 → 日频向前 ffill（防前视）
python scripts/sync_macro_credit.py

# 2. 板块两融 + 活跃股初筛：融资融券明细 → top3 信贷扩张板块 → 50 只活跃股 → 拉日线
python scripts/sync_sector_daily.py

# 3. JQData 分钟级：对上述 50 只活跃股拉近 3 月 1m/5m（配额双机制防封）
python scripts/sync_jqdata_1min.py

# 4. (可选) Binance 加密沙盒：aiohttp 下载 BTCUSDT 1m ZIP → 7x24 极端市场测试
python scripts/sync_binance_vision.py
```

- **前视红线**：宏观月频→日频仅向前 ffill；volume/amount 绝不 ffill（防流动性测算失真）。
- **JQData 防暴雷**：单例锁防并发 + 配额双机制（手动计数 + `get_query_count` 校准，spare<5 万即停 + 钉钉告警）+ 断点续传。
- **多湖读取**：`DataLakeReader` 单例加载 Macro/Sector/Daily/1Min/Crypto 五湖到内存，`get_*(lake=)` 按 key 查询，毫秒级截面/时序切片。
- Tushare 同步脚本 `scripts/sync_data_lake.py` 保留作 dormant 备用（新流程不调用）。

---

## 5. 启动后端与前端

### 5.1 后端

```bash
uvicorn server.main:app --reload
```

默认 `http://127.0.0.1:8000`，API 文档 `/docs`。启动期按 `LAKE_CONFIG["lakes"]` 加载存在的湖，缺失则离线降级。

### 5.2 前端（双路由）

```bash
cd web && npm run dev
```

- `/` —— 回测终端：ProChart（支持日级/分钟级 K 线 + 止损止盈移动止损标注）+ SSE 实时日志流（按 `[INFO]/[WARN-STOPLOSS]/[TRADE]` 级别高亮）+ 绩效指标 + 末态持仓。
- `/dashboard` —— 宏观·板块驾驶舱：CreditRegime 状态卡 + 历史色带、社融/M1M2剪刀差/DR007 三联折线、板块融资增速热力/条形（top3 高亮）、活跃股池表。

---

## 6. Celery Worker（因子沙盒）

```bash
celery -A server.celery_app worker -Q explorer -l info
```

需先启动 Redis；无 Redis 时因子沙盒降级为同步执行（CPU 探针 >80% 拒绝调度）。

---

## 7. 宏观 CTA 五大 Epic 速览

| Epic | 模块 | 一句话说明 |
|------|------|-----------|
| **Epic 1** | 四级数据湖 | AKShare 宏观/板块/日线 + JQData 分钟 + Binance 可选；`DataLakeReader` 多湖缓存，价格 ffill/volume 不 ffill 防前视。 |
| **Epic 2** | 因子沙盒 | `CreditRegime`（日频 +1/0/-1 宏观信贷状态机）+ 微观动量爆发 + ATR Risk Parity 头寸（波动越大头寸越小）。 |
| **Epic 3** | 执行网关 + 订单 | `BaseExecutionGateway` 异步抽象（QMT/EMT/Mock 子类）+ 止损/止盈/ATR 移动止损 + T+1 底仓冻结感知（变相 T+0）。（注：`MacroAwareGateway` 宏观一票否决已于蔡森专精化后移除——caisen 为纯价量形态学、不消费 CreditRegime；宏观 regime 经 `risk.macro_position_coef` 仓位系数体现。） |
| **Epic 4** | SSE 实时回测流 | `run_minute(event_emitter=)` 透传进度/成交/风控（触及止损止盈）事件至前端 EventSource，分钟级跑码视觉。 |
| **Epic 5** | 钉钉风控网关 | `DingTalkChannel`（aiohttp + 加签 + errcode 校验 + 结构化卡片）+ `fire_and_forget` 跨线程告警：JQData 流量耗尽 / 外部熔断 / 宏观 -1 否决 / 回撤超阈。 |

---

## 8. 设计文档与计划

- **宏观 CTA 重构（当前）**：
  - 设计：[`docs/superpowers/specs/2026-07-01-macro-cta-refactor-design.md`](docs/superpowers/specs/2026-07-01-macro-cta-refactor-design.md)
  - 计划：[`docs/superpowers/plans/2026-07-01-macro-cta-refactor.md`](docs/superpowers/plans/2026-07-01-macro-cta-refactor.md)
- **上轮工业级蜕变（历史）**：
  - 设计：[`docs/superpowers/specs/2026-07-01-quanter-industrial-design.md`](docs/superpowers/specs/2026-07-01-quanter-industrial-design.md)
  - 计划：[`docs/superpowers/plans/2026-07-01-quanter-industrial.md`](docs/superpowers/plans/2026-07-01-quanter-industrial.md)

执行轨迹（每 Task 的实现/审查/修复证据）见 `.superpowers/sdd/progress.md`。

---

## 钉钉远程驱动 Claude 旁路桥

独立守护进程，用手机钉钉远程驱动本机 `claude` CLI（全放行模式，等同你在终端每个确认按 `y`）。
适合「人不在电脑前，但要远程让 claude 跑一段分析/改一处代码/看一份回测」的场景。

### 配置（`.env`）

```
DINGTALK_APP_KEY=<企业内部应用 Client ID>
DINGTALK_APP_SECRET=<Client Secret>
DINGTALK_ALLOWED_STAFF_IDS=<你的 staffId，逗号分隔>
```

凭证在钉钉开放平台「应用开发 → 企业内部应用 → 凭证与基础信息」获取；机器人需开通 Stream 模式并发布上线。其余可选项（`CLAUDE_BIN` / `CLAUDE_WORKDIR` / `BRIDGE_ASK_TIMEOUT` / `BRIDGE_IDLE_TTL` / `BRIDGE_RATE_LIMIT_PER_MIN`）见 `.env.example`。

### 启动

```bash
python -m bridge            # 等价：python scripts/dingtalk_claude_bridge.py
```

启动后在钉钉群/单聊 @机器人 发消息即可。机器人把消息透传给本机常驻的 `claude` 子进程，回复分段推回钉钉。

### 安全须知（全放行模式）

- claude 拥有完整文件读写 + 命令执行能力（等同终端每个确认按 `y`）。
- 仅 `DINGTALK_ALLOWED_STAFF_IDS` 内用户可触发——全放行模式下唯一身份闸。
- 全量审计：`logs/dingtalk_bridge_audit.jsonl`（每条消息一行，含 sender / conversation_id / text / action）。
- 高危操作（碰 `trading/`、`.env`、`rm`、下单函数等）实时推钉钉告警（事中知情）。
- 会话历史由 claude 存本地 `~/.claude/`，进程崩溃后 `--resume <session_id>` 自动续上下文。
- 降级：把 `bridge/claude_pool.py` 的 `--permission-mode bypassPermissions` 改 `acceptEdits` 即可禁掉命令执行（仍可读写文件）。

### 指令

- 直接发消息 = 与 claude 对话
- `/new` 重置当前会话上下文（杀进程 + 清映射 + 清 store）
- `/status` 查看桥的活跃会话列表
- `/help` 显示帮助

---

## 许可与贡献

本项目为个人量化研究工程，代码与策略仅供学习交流。贡献请遵循 `CLAUDE.md` 的「全中文 + 显式实现 + 极端边界拷问」工程协议。
