# 设计文档：企业级容灾基建 + 专业交易终端重构

- **日期**：2026-06-30
- **分支**：`feat/resilience-terminal`
- **状态**：已与研究员对齐并获批，待实现
- **范围**：后端 3 个高可用基建模块 + 前端 3 个终端组件 + 2 处地基补丁（共 6 新建 + 7 改动）
- **红线**：不改任何数学因子与回测逻辑；不伪造任何券商/即时通讯 API 参数

---

## 1. 背景与现状（探查结论）

### 1.1 后端：异步外壳 + 同步内核
- FastAPI 路由全部 `async def`，但 pandas/hmmlearn 等 CPU 密集业务经 `starlette.concurrency.run_in_threadpool` 卸载到线程池（事件循环保护红线）。
- `data/fetcher.py`（1188 行）为**纯同步** SDK（fredapi/tushare），**无超时、无重试、无熔断**；异常策略是 `try/except Exception` 分类记日志后返回**空 DataFrame**。
- `trading/` 已有 `mock_broker.py`（回测模拟 broker）+ `order_state.py`（`OrderState` 枚举 + `OrderStateMachine` 有限状态机），**无实盘执行层、无 QMT 适配**。
- **无 `core/` 根包**（仅 `server/core/config.py`）；**无事件总线**；日志为模块级散落 `logging.getLogger(__name__)`，无统一配置。

### 1.2 前端：Vue3 + Vite6 + TS
- 栈：`vue@^3.5` + `vite@^6.0` + `typescript@^5.6` + Element Plus `^2.9.0`（全量注册）+ ECharts `^5.5.0` / vue-echarts `^7.0.0` + axios + vue-router `^4.4.0`（2 路由 `/`、`/portfolio`）。
- **无 Pinia**、**无 dark mode**（`App.vue` 硬编码浅色 `#f5f7fa`）、**无 WebSocket/SSE**。`<script setup lang="ts">` Composition API 已是标准范式。
- 布局扁平写在 `App.vue`（`el-container` + 顶部 tab），注释明示"不引入侧边栏，保持扁平"。

### 1.3 回测数据契约（关键缺口）
单资产 `POST /api/v1/backtest/run` → `BacktestResponse`（`server/schemas/backtest.py:182`）：

| 字段 | 类型 | 内容 | ProChart 可用性 |
|---|---|---|---|
| `metrics` | `MetricsResponse` | 绩效指标（年化/夏普/回撤/胜率…） | ✅ MetricCards |
| `nav_series` | `List[NavPoint]` | `{date, nav, return, cumulative_return}`（JSON 键名是 `return`） | ✅ 净值叠加线 |
| `drawdown_series` | `List[DrawdownPoint]` | 回撤时序 | ✅ 副图（可选） |
| `trades` | `List[TradeRecord]` | `{date, direction:"buy"/"sell"/"failed", shares, price, cost}` | ✅ 买卖点 markPoint |
| **ohlcv** | **缺失** | 引擎消费了 OHLCV 但 `_serialize_backtest_result` 丢弃 | ❌ **必须补** |
| **positions** | **缺失** | `daily_records` 内部有 `positions/position_values`，序列化时丢弃 | ❌ **补透传**（末行快照） |

- OHLCV 来源列名：`open/high/low/close/volume/amount`（全小写英文，`DatetimeIndex`，tz `Asia/Shanghai`）。
- 组合 `/portfolio` 多一个 `weight_series: List[WeightPoint]`。

---

## 2. 目标与非目标（YAGNI）

### 目标
1. 后端可装饰的熔断器 + 令牌桶限流器，真实兜住 fetcher 外部请求。
2. 异步实盘执行抽象层，含严谨的本地↔券商持仓对账（Reconciliation）。
3. 异步单例多通道预警通知（Telegram + 企业微信）。
4. 全屏暗黑终端布局 + 专业 K 线图 + 沉浸式日志终端，全部接真实数据。

### 非目标
- 不接入真实 QMT/CTP 下单（仅抽象基类 + Mock 参考实现，杜绝幻觉参数）。
- 不重写因子/回测数学逻辑。
- 不引入 Redis / Celery / 消息队列（本地内存即够，Karpathy 极简）。
- 不引入 Pinia（组件内 `ref/reactive` + props/emits 维持现状）。

---

## 3. 后端模块设计

### 3.1 `data/resilience.py`（新建）
纯 Python 显式实现，**不引 tenacity/circuitbreaker 黑盒**。线程安全 + async 自适应（装饰器检测被包函数是否为 coroutine）。

**`CircuitBreaker`**
- 构造：`CircuitBreaker(name, failure_threshold=3, recovery_timeout=60, expected_exception=Exception, on_open=None, on_close=None, half_open_max_calls=1)`。
- 状态机：`CLOSED → OPEN`（连续 `failure_threshold` 次抛出 `expected_exception`）→ `HALF_OPEN`（`recovery_timeout` 秒后允许 `half_open_max_calls` 次试探）→ 成功则 `CLOSED`、失败则重开 `OPEN`。
- OPEN 期间调用直接抛 `CircuitOpenError`（不触达被保护函数）。
- 装饰器语法 `@cb_protect`；`on_open` 回调用于联动 `notifier`（异步回调走 `asyncio.create_task` 或线程安全队列，避免阻塞同步 fetcher）。

**`RateLimiter`（令牌桶）**
- 构造：`RateLimiter(name, capacity, refill_rate, timeout=None)`；`refill_rate` 为 token/秒。
- `acquire(tokens=1, timeout=None) -> bool`：按 `monotonic()` 时间差线性补令牌，令牌不足则阻塞等待或超时返回 `False`。
- 装饰器 `@rate_limit`，按函数调用消费 1 个令牌。

**fetcher 集成（必要外科手术，不改取数逻辑）**
- 新增类型 `DataFetchError(Exception)`。在 `data/fetcher.py` 的基础设施异常分支（超时 / 429 / 连接错误）**记日志后抛出 `DataFetchError`**；"无数据"场景仍返回空 DataFrame。
- `TushareDataFetcher`/`FredDataFetcher` 的 `fetch_*` 方法外层叠加 `@rate_limit` + `@cb_protect`。CB 默认统计 `DataFetchError`。
- 同步路径用 `threading.Lock`；async 路径（gateway/notifier 复用时）用对应异步原语。

### 3.2 `trading/execution_gateway.py`（新建）
异步抽象执行层，**复用 `trading/order_state.py` 的 `OrderState/OrderStateMachine`**，不另起状态契约。

**抽象基类 `BaseExecutionGateway`（ABC，全 `async`）**
- `async connect() -> None` / `async disconnect() -> None`
- `async submit_order(order: OrderRequest) -> OrderResult`：返回带 `OrderState` 的结果。
- `async cancel_order(order_id: str) -> OrderResult`
- `async sync_positions(local_positions: Mapping[str, float], tolerance: float = 0.0) -> ReconciliationResult`：**核心对账**。内部先 `await self._fetch_broker_positions()` 取券商真实持仓，再与 `local_positions` 比对。
- 抽象 `_fetch_broker_positions() -> Mapping[str, float]`（子类实现真实券商取数）。

**对账数据结构（纯函数式，可独立单测）**
```python
@dataclass(frozen=True)
class PositionDrift:
    symbol: str
    local_qty: float
    broker_qty: float
    delta: float          # broker_qty - local_qty

@dataclass(frozen=True)
class ReconciliationResult:
    matched: list[PositionDrift]       # |delta| <= tolerance
    drifted: list[PositionDrift]       # |delta| > tolerance（敞口偏差）
    only_local: list[PositionDrift]    # 券商无、本地有（疑似未成交/丢单）
    only_broker: list[PositionDrift]   # 券商有、本地无（疑似外部成交/手动单）
    max_abs_drift: float
    is_ok: bool                        # drifted/only_* 均空
```

**参考实现 `MockExecutionGateway(BaseExecutionGateway)`**：`_fetch_broker_positions` 内部读 `MockBroker.positions`，可注入人为偏差用于测试对账逻辑。**真实 `QMTExecutionGateway` 留抽象占位**，不写任何 xtquant 调用。

### 3.3 `core/notifier.py`（新建，含 `core/__init__.py`）
异步单例 `NotificationManager`。

- **单例**：模块级 `_instance` + `NotificationManager.get_default()`，避免重复建连。
- **通道解耦**：`NotificationChannel` 抽象（`async send(text: str) -> None`）→ `TelegramChannel(bot_token, chat_id)`、`WeComChannel(webhook_url)`。底层 `httpx.AsyncClient`（复用连接池）。
- **`notify_risk_event(msg: str, level: Literal["INFO","WARN","ERROR","CRITICAL"])`**：按 level 加 emoji 前缀与颜色标签，`asyncio.gather(*channels, return_exceptions=True)` 并发推；单通道异常软降级、记日志，不阻塞其它通道。
- **凭证来源**：`config.py` 字典 + `.env`（`TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` / `WECOM_WEBHOOK`）。**绝不硬编码 token**。
- **解耦触发示例**：CB `on_open=lambda: asyncio.create_task(mgr.notify_risk_event("Tushare 熔断", "ERROR"))`；对账 `is_ok is False` 时推送。

---

## 4. 前端模块设计

### 4.1 全局布局（重构 `App.vue`）+ 强制暗黑
- **暗黑**：`main.ts` 引入 `element-plus/theme-chalk/dark/css-vars.css`，启动时 `document.documentElement.classList.add('dark')`；ECharts 注册 dark theme（自定义 `terminal-dark`）。
- **Grid 终端**（`100vh` 无滚动）：
  - `grid-template-columns: 300px 1fr 250px`
  - `grid-template-rows: 100%`（左/右通栏）；中央列内部再 `grid-template-rows: 70% 30%`
  - 左 = `ParamForm`；中上 = `ProChart`；中下 = `TerminalLogs`；右 = `MetricCards` + `PositionsTable`
- `/portfolio` 路由按需适配（右栏改显权重而非单资产持仓）。

### 4.2 `components/ProChart.vue`（新建，替换 NavChart 为主图）
- vue-echarts + `CandlestickChart`：主图 K 线（消费新 `ohlcv`）+ 净值叠加折线（消费 `nav_series`，右轴）。
- 副图成交量 `Bar`；主副图 `dataZoom` 联动（`connect` 或 `axisPointer link`）。
- `markPoint`：`trades.direction === "buy"` → 绿色 B（`coord: [date, price]`），`"sell"` → 红色 S；`"failed"` → 灰色 ✕。
- 暗色主题、`tooltip` 显示 OHLCV + 净值。
- Props：`ohlcv`, `navSeries`, `trades`（类型与后端契约对齐）。

### 4.3 `components/TerminalLogs.vue`（新建 + SSE）
- 黑底（`#0d1117`）、等宽字体（`ui-monospace`）、按级别配色：`[INFO]` 灰 / `[SUCCESS]` 绿 / `[WARN]` 黄 / `[ERROR]` 红。
- 自动滚到底（`watch(logs) → nextTick → scrollTop = scrollHeight`），带"跟随/暂停"切换避免用户上翻时被强制下拉。
- 数据源：`new EventSource('/api/v1/logs/stream')`；`onmessage` 追加；`onerror` 退避重连。

---

## 5. 地基补丁（为"真实数据"的增量，零逻辑改动）

### 5.1 后端 OHLCV/positions 透传
- `server/schemas/backtest.py`：`BacktestResponse` 加 `ohlcv: List[OhlcvPoint]`（`{date, open, high, low, close, volume}`）；`portfolio.py` 同步加。
- `server/services/backtest_service.py` `_serialize_backtest_result`：从 `df_clean` 透传 `open/high/low/close/volume`（沿用 fetcher 小写列名，日期按 `Asia/Shanghai` 格式化为 ISO）。
- **补 `positions` 字段**（`List[PositionRow]`，取 `daily_records` 末行快照 `{symbol, qty, market_value}`）供 `PositionsTable`。定死走透传路线，不在前端从 `trades` 重算持仓（避免重复逻辑、保持单一真相源）。

### 5.2 后端 SSE 日志端点
- 新增 `GET /api/v1/logs/stream`（`StreamingResponse`，`media_type="text/event-stream"`）。
- 一个挂到 Python `logging` 根 logger 的内存环形缓冲 handler（`collections.deque(maxlen=1000)`）；SSE 端点用 `asyncio.Queue` 把新日志推给已连接客户端。
- 日志行格式：`{ts, level, logger, message}` → SSE `data:` 帧。

### 5.3 前端类型同步
- `web/src/api/backtest.ts`：`SingleBacktestResponse` 加 `ohlcv: OhlcvPoint[]`。

---

## 6. 触及文件清单

**新建（6）**：
- `data/resilience.py`
- `trading/execution_gateway.py`
- `core/__init__.py`、`core/notifier.py`
- `web/src/components/ProChart.vue`
- `web/src/components/TerminalLogs.vue`

**改动（7）**：
- `data/fetcher.py`（异常分类 → `DataFetchError`，最小外科手术）
- `server/schemas/backtest.py`、`server/schemas/portfolio.py`（加 `ohlcv`）
- `server/services/backtest_service.py`（透传 OHLCV）
- `server/api/v1/`（新增 SSE 端点 + 注册日志 handler）
- `web/src/App.vue`（Grid 终端布局）、`web/src/main.ts`（dark mode + ECharts 主题）
- `web/src/api/backtest.ts`（类型同步）

---

## 7. 风险与红线（拷问清单）

1. **不伪造 API**：QMT 仅抽象占位；Telegram/企微凭证走 `.env`，不硬编码。
2. **fetcher 异常分类属必要外科手术**：仅区分"基础设施错误（抛 `DataFetchError` 供熔断统计）"与"无数据（返回空 DF）"，不动取数数学。
3. **OHLCV 透传为纯加字段**：不改回测引擎逻辑。
4. **同步/异步边界**：fetcher 同步 → resilience 同步锁；gateway/notifier 异步 → resilience 自适应异步路径。CB 的 `on_open` 异步回调不能阻塞同步 fetcher 调用方。
5. **SSE 长连接**：客户端断开需及时清理 `asyncio.Queue`，避免内存泄漏（生命周期与请求绑定）。
6. **部分成交/断线**：`sync_positions` 的 `only_local`/`only_broker` 正是用于暴露"丢单/外部成交"风险敞口，对账结果应可触发 `notifier` ERROR。

---

## 8. 测试策略

- `resilience.py`：CB 三态迁移（连续失败跳闸、冷却后半开、半开成功/失败）、令牌桶补充速率与阻塞超时、并发安全（多线程）。
- `execution_gateway.py`：对账纯函数单测——构造 local/broker dict，断言 `matched/drifted/only_local/only_broker/max_abs_drift/is_ok`；MockExecutionGateway 端到端。
- `notifier.py`：mock httpx 响应，断言多通道并发与单通道软降级；级别前缀正确。
- 前端：ProChart 给定 mock ohlcv+trades 渲染 B/S markPoint；TerminalLogs 自动滚动与重连。
