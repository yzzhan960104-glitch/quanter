# Quanter 工业级蜕变 — 总体设计文档

> **状态**：已通过架构评审，待用户终审 → 转入 writing-plans
> **日期**：2026-07-01
> **范围**：5 大 Epic 全栈升级（数据湖 / GLM 情感 / 因子沙盒 / SSE 回测流 / 宏观+钉钉）
> **原则基线**：CLAUDE.md（全中文 + Karpathy 极简 + Grill Me 拷问 + 反黑盒）

---

## 0. 背景与现状校准

本设计并非绿地搭建，而是对**已相当成熟**的 `quanter`（FastAPI + Vue3 + 纯 Python 量化引擎）做能力扩展。下列 spec 中"新建"项与"已存在需复用/扩展"项已严格区分，杜绝重造轮子：

| 能力 | 现状 | 本轮动作 |
|---|---|---|
| 熔断器 `CircuitBreaker` / 令牌桶 `RateLimiter` | ✅ `data/resilience.py`（装饰器 + 手动 API 双路径，支持 async） | **复用**，不重造 |
| 通知 `NotificationManager` + `NotificationChannel` ABC | ✅ `core/notifier.py`（Telegram/企微，全异步，软降级） | **扩展**：加钉钉通道 |
| SSE 全局日志流 `LogStreamHub` | ✅ `server/api/v1/logs.py`（跨线程 `call_soon_threadsafe`） | **参考**其模式做 per-run 流 |
| 回测引擎 `BacktestEngine.run` | ✅ `backtest/engine.py`（1112 行事件驱动） | **最小侵入**：加可选 `event_emitter` |
| Tushare 数据获取器 | ✅ `data/fetcher.py`（`pro.daily()` **不复权**） | **不复用其取数**：数据湖要 `pro_bar(adj='qfq')` 前复权 |
| yfinance | ⚠️ 依赖已声明、代码零引用 | **新建** client |
| 数据湖 / GLM / 情感因子 / 动量矩阵 / IC 分层 / Alpha Vantage / Celery | ❌ 全新 | **新建** |
| `build_default_manager()` 接入启动 | ❌ lifespan 漏调 | **修复** |

### 0.1 用户已确认的 4 个走向（澄清问答结论）
1. **推进方式**：一份覆盖全部 5 Epic 的总体设计 + 统一实现。
2. **Epic 3 调度**：坚持 **Celery + Redis**（已加 Redis 宕机降级安全网）。
3. **Epic 1 数据源**：**tushare 为主**（已有 token、字段全）。
4. **数据湖范围**：**全市场常驻内存**（~5000 标的 × ~2500 交易日 ≈ 12.5M 行 / parquet 300-600MB / 内存 ~1-2GB）。
5. **凭证**：GLM / Alpha Vantage / 钉钉**三套用户均已有**，按"可实战"标准设计，同时保留凭证缺失时的优雅降级。

---

## 1. 横切关注点（地基，先于各 Epic）

### 1.1 新增依赖（`requirements.txt`，最小可辩护集）
```
pyarrow>=14.0          # parquet 引擎（现仅运行时隐式依赖，显式化）
openai>=1.30           # GLM 走标准 OpenAI SDK + base_url 覆盖
aiohttp>=3.9           # 钉钉 webhook（决策点③，spec 指定）
celery[redis]>=5.3     # Epic 3 任务队列
redis>=5.0             # Celery broker / backend
psutil>=5.9            # CPU 探针（Windows 无 getloadavg，必须 psutil）
tqdm>=4.66             # 数据湖同步进度条
```
**明确不引入**：`akshare`（universe 用 `pro.stock_basic`）、`sse-starlette`（用零依赖 `StreamingResponse`）、`alphalens`（自研 IC/分层）、`scipy`（秩相关用 pandas rank + corr）、`alpha_vantage` 官方包（REST + httpx）。

### 1.2 新增环境变量（新建 `.env.example`）
```dotenv
# ---- Epic 1 数据湖 ----
DATA_LAKE_PATH=data_lake/a_shares_daily.parquet
TUSHARE_TOKEN=                         # 已存在于 .env

# ---- Epic 2 GLM ----
ZHIPU_API_KEY=
ZHIPU_BASE_URL=https://open.bigmodel.cn/api/paas/v4/
ZHIPU_MODEL=glm-4-flash

# ---- Epic 3 Celery ----
REDIS_URL=redis://localhost:6379/0
CELERY_EXPLORER_QUEUE=explorer

# ---- Epic 5 宏观 + 钉钉 ----
ALPHA_VANTAGE_API_KEY=
DINGTALK_WEBHOOK=
DINGTALK_SECRET=                       # 加签密钥
```

### 1.3 `config.py` 增量（扁平字典，沿用现有风格）
```python
LAKE_CONFIG = {"default_path": "data_lake/a_shares_daily.parquet",
               "shard_dir": "data_lake/shards", "years_default": 10}
LLM_CONFIG = {"base_url": os.getenv("ZHIPU_BASE_URL", "https://open.bigmodel.cn/api/paas/v4/"),
              "model": os.getenv("ZHIPU_MODEL", "glm-4-flash"), "timeout": 15}
MACRO_CLIENT_CONFIG = {"yfinance_symbols": {"SPX":"^GSPC","CL":"CL=F","GC":"GC=F","VIX":"^VIX"},
                       "av_treasury_maturities": ["3MO","2Y","10Y","30Y"]}
CELERY_CONFIG = {"broker_url": os.getenv("REDIS_URL","redis://localhost:6379/0"),
                 "queue": os.getenv("CELERY_EXPLORER_QUEUE","explorer"),
                 "cpu_gate_percent": 80.0}
```

### 1.4 `server/main.py` lifespan 增量
```python
async def lifespan(app: FastAPI):
    loader = StrategyLoader(); loader.scan(); app.state.strategy_loader = loader
    # 日志 SSE（已有）
    log_handler = RingBufferLogHandler(log_stream_hub); ...
    # ★ 新增：通知装配（现状漏接）
    build_default_manager()
    # ★ 新增：数据湖常驻内存（缺失则离线降级，不阻断启动）
    DataLakeReader.get_instance().load()
    # ★ 新增：GLM 客户端单例
    GLMClient.get_instance()
    yield
    logging.getLogger().removeHandler(app.state.log_handler)
```

### 1.5 新增目录
`scripts/`、`data/clients/`（+ `__init__.py`）。

---

## 2. 🗄️ Epic 1：极速本地数据湖

### 2.1 `scripts/sync_data_lake.py`（独立 CLI，断点续传）
**职责**：拉取全市场（剔除 ST/退市）过去 N 年日线**前复权** OHLCV，合并为 MultiIndex 超级大表。

**关键接口**：
```python
def load_universe(pro) -> list[str]:
    """pro.stock_basic(list_status='L') → 过滤 list_date 超出范围 + 名称含 'ST'/'退'"""

def fetch_qfq(pro, ts_code, start, end) -> pd.DataFrame:
    """★ 关键：pro_bar(ts_code, adj='qfq', start_date, end_date, freq='D')
    复权一致性拷问：现有 TushareDataFetcher 用 pro.daily() 是【不复权】，不可复用。"""

def main(years=10, out=LAKE_PATH, resume=True):
    codes = load_universe(pro)
    for ts_code in tqdm(codes):
        shard = f"{SHARD_DIR}/{ts_code}.parquet"
        if resume and exists(shard): continue          # 断点续传
        tushare_rate_limiter.acquire(1.0)              # 复用现有令牌桶防封
        if not tushare_breaker.allow_request():        # 复用现有熔断器
            time.sleep(throttle); continue
        df = fetch_qfq(pro, ts_code, start, end)
        if df.empty: logger.warning(...); continue     # 停牌/退市/空 → 跳过不中断
        df.to_parquet(shard); time.sleep(throttle)
    concat_shards_to_multiindex(out)                   # 合并 → sort → MultiIndex(date,symbol) → pyarrow
```

**错误处理 / 边界**：
- 单标的失败（限频/熔断/空数据）→ 记日志跳过，**绝不中断全量同步**；断点续传保证可重跑。
- 前复权是**时点快照**：发生新拆分/红利后历史会变 → 文档标注"建议季频重同步"。
- tz 统一 `Asia/Shanghai`；列 `[open,high,low,close,volume,amount]`。

### 2.2 `data/lake_reader.py`（单例 `DataLakeReader`）
**职责**：启动时一次性加载 parquet 到内存，提供截面/时序查询。

**关键接口**：
```python
class DataLakeReader:                       # 双重检查锁单例，仿 NotificationManager
    def load(self, path=LAKE_PATH) -> None:
        if not exists(path): logger.warning("数据湖缺失，进入离线模式"); self._loaded=False; return
        self._df = pd.read_parquet(path)    # MultiIndex(date, symbol)
        # ★ 前视偏差拷问：仅价格列沿【时间轴】ffill（停牌沿用末次成交价，无前视）；
        #   volume/amount 不 ffill（停牌日成交应为 0，ffill 会造假量）
        self._ffill = (self._df[["open","high","low","close"]]
                       .groupby(level="symbol").ffill())
        self._loaded = True
    def get_cross_section(self, date) -> pd.DataFrame:   # 某日全市场截面（价格取 _ffill 版）
    def get_timeseries(self, symbol, start, end) -> pd.DataFrame:  # 单标的原始时序（不 ffill）
```
**降级**：未加载时查询返回空 DataFrame，**不抛异常**。

---

## 3. 🧠 Epic 2：GLM 大模型 + 另类情感因子

### 3.1 `core/llm_client.py`（单例 `GLMClient`，openai SDK）
**职责**：解耦的大模型情感打分客户端，强制结构化输出，超时/异常一律降级中性。

```python
class SentimentResult(BaseModel):
    score: float = Field(ge=-1.0, le=1.0)    # [-1 极空, 0 中性, 1 极多]
    reasoning: str

SYS_PROMPT = "你是冷酷客观的量化分析师，仅输出 JSON {score, reasoning}..."

class GLMClient:                              # get_instance() 单例
    def __init__(self):
        self._enabled = bool(os.getenv("ZHIPU_API_KEY"))
        self._client = AsyncOpenAI(base_url=LLM_CONFIG["base_url"], api_key=...)
    async def analyze_sentiment(self, news_text: str) -> SentimentResult:
        if not self._enabled: return SentimentResult(0.0, "凭证缺失，降级中性")
        try:
            resp = await self._client.chat.completions.create(
                model=LLM_CONFIG["model"], response_format={"type":"json_object"},
                messages=[{"role":"system","content":SYS_PROMPT},
                          {"role":"user","content":news_text}], timeout=LLM_CONFIG["timeout"])
            return SentimentResult.model_validate_json(resp.choices[0].message.content)
        except Exception:                     # 超时/限频/JSON 非法 → 绝不上抛
            return SentimentResult(0.0, "降级中性")
```

### 3.2 `factors/alternative_sentiment.py`
```python
class NewsSentimentFactor:
    def __init__(self, client: GLMClient): self._client = client
    async def compute_daily_score(self, news_list: list[str]) -> float:
        results = await asyncio.gather(
            *(self._client.analyze_sentiment(t) for t in news_list), return_exceptions=True)
        scores = [r.score for r in results if isinstance(r, SentimentResult)]  # 单条失败不炸整批
        return float(np.average(scores)) if scores else 0.0                    # 全失败 → 0.0
```

---

## 4. 🔬 Epic 3：异步因子探索沙盒（Celery + Redis）

### 4.1 `factors/exploratory_momentum.py`（纯 Pandas 向量化，零黑盒）
- `cross_sectional_momentum(returns: pd.DataFrame, window=20) -> pd.DataFrame`
  滚动收益率 → `rank(pct=True)` 横截面百分位排名。
- `vol_adjusted_momentum(returns, high, low, close, window=20, atr_window=20) -> pd.DataFrame`
  ATR = `(high-low).rolling(atr_window).mean()`；动量 = 滚动收益 / ATR（防除零：ATR→ε）。
- `hurst_exponent(series: pd.Series, max_k=50) -> float`
  R/S 重标极差法：对每个 lag k 计算 `R/S = (累计均值偏离极差) / 标准差`，log-log 回归斜率即 H。逐标的标量，循环可接受（非 tick 热点）。

### 4.2 `factors/analyzer.py`（`FactorAnalyzer`，禁 Alphalens）
```python
class FactorAnalyzer:
    def compute_ic(self, factor: pd.DataFrame, fwd_returns: pd.DataFrame) -> dict:
        # 秩相关 IC：逐日横截面 factor.rank().corr(fwd.rank()) → 纯 pandas，无需 scipy
        ic = factor.rank().corrwith(fwd_returns.rank(), axis=1)
        return {"ic_series": ic, "ic_mean": ic.mean(),
                "ic_ir": ic.mean()/ic.std(), "t_stat": ic.mean()/ic.std()*len(ic)**0.5}
    def fractile_analysis(self, factor, fwd_returns, n_groups=5) -> dict:
        # pd.qcut 逐日分层 → 各层远期收益 → 多空 top-bottom 价差曲线
```

### 4.3 `server/celery_app.py` + `server/api/v1/explorer.py`
```python
# celery_app.py
celery_app = Celery("quanter", broker=CELERY_CONFIG["broker_url"], backend=CELERY_CONFIG["broker_url"])

@celery_app.task(name="explorer.run_factor_grid")
def run_factor_grid(spec: dict) -> str:
    # Worker：调 FactorAnalyzer/exploratory_momentum，结果落 reports/explorer/{task_id}.json
    ...

# explorer.py
@router.post("/grid")
async def submit_grid(spec: FactorGridSpec):
    if psutil.cpu_percent(interval=0.1) > CELERY_CONFIG["cpu_gate_percent"]:   # ★ 探针 >80% 拒绝
        raise HTTPException(429, "CPU 负载过高，拒绝调度")
    try:
        task = run_factor_grid.delay(spec.model_dump()); return {"task_id": task.id}
    except redis.ConnectionError:                       # ★ Redis 宕机降级
        await NotificationManager.get_default().notify_risk_event(
            "Redis 不可用，因子网格降级到线程池执行", "WARN")
        result = await run_in_threadpool(run_factor_grid_impl, spec.model_dump())
        return {"result": result, "degraded": True}

@router.get("/result/{task_id}")
async def get_result(task_id): ...   # 查 Celery AsyncResult 状态 + 结果
```
**风控红线**：Redis 不可用 → 钉钉告警 + 降级线程池，**绝不阻断**；CPU 探针超阈值直接拒绝。

---

## 5. 🌟 Epic 4：SSE 实时回测流（per-run）

### 5.1 `backtest/engine.py`（最小侵入，向后兼容）
```python
def run(self, df, signal, symbol="600000.SH",
        event_emitter: Callable[[dict], None] | None = None) -> dict:
    for i, (date, row) in enumerate(aligned_df.iterrows()):
        ...  # 原逻辑完全不变
        if event_emitter:
            event_emitter({"type":"progress","date":str(date),"i":i,"n":n})
        # _execute_trade 成交时 → {"type":"trade","direction":...,"shares":...,"price":...,"date":...}
        # 涨跌停/流动性枯竭/现金不足 → {"type":"risk","level":"WARN","msg":...,"date":...}
```
默认 `event_emitter=None` → 现有所有调用方零改动。

### 5.2 `server/api/v1/backtest.py`（两步式；EventSource 只支持 GET）
```python
# 内存 run 注册表（run_id → 请求参数，TTL 清理），仿 LogStreamHub 跨线程投递
_run_registry: dict[str, BacktestRequest] = {}
_run_streams: dict[str, RunStreamHub] = {}     # 每个 run 一个独立 hub

@router.post("/run/async")
async def create_run(req: BacktestRequest):
    run_id = str(uuid4()); _run_registry[run_id] = req; return {"run_id": run_id}

@router.get("/run/stream/{run_id}")
async def stream_run(run_id: str):
    req = _run_registry.get(run_id)
    if req is None: raise HTTPException(404, "run 不存在或已过期")
    hub = RunStreamHub(); _run_streams[run_id] = hub
    async def gen():
        q = hub.subscribe()
        # 在线程池跑回测；event_emitter 经 hub.publish 跨线程安全投递（call_soon_threadsafe）
        async def runner():
            result = await run_in_threadpool(_run_with_emitter, req, hub.publish)
            hub.publish({"type":"result","data":result})
        asyncio.create_task(runner())
        try:
            while True:
                ev = await q.get()
                yield f"data: {json.dumps(ev, ensure_ascii=False, default=str)}\n\n"
                if ev.get("type") == "result": break
            yield "data: [DONE]\n\n"
        finally:
            hub.unsubscribe(q); _run_streams.pop(run_id, None)   # ★ 防泄漏
    return StreamingResponse(gen(), media_type="text/event-stream")   # 零依赖，不用 EventSourceResponse
```
**协程安全（复用 `logs.py` 三件套）**：`call_soon_threadsafe` 跨线程投递 + `QueueFull` 静默丢弃（绝不阻塞事件循环）+ `finally unsubscribe`（客户端断开 → 生成器取消 → 清理，无积压/泄漏）。

保留原 `POST /run`（向后兼容 + 测试）。

### 5.3 前端（`useTerminalState.ts` / `ParamForm.vue`）
```ts
async function execute(req: SingleBacktestParams) {
  state.loading = true; state.error = ''; logs.value = []
  const { run_id } = await axios.post('/api/v1/backtest/run/async', req)   // 仅建 run 用 axios
  const es = new EventSource(`/api/v1/backtest/run/stream/${run_id}`)
  es.onmessage = (e) => {
    if (e.data === '[DONE]') { es.close(); state.loading = false; return }
    const ev = JSON.parse(e.data)
    if (ev.type === 'result') state.result = markRaw(ev.data)    // → ProChart/NavChart 渲染
    else logs.value.push(ev)                                     // trade/risk/progress → 终端按级别高亮
  }
  es.onerror = () => { state.error = '流中断'; es.close(); state.loading = false }
}
```
- `ParamForm.vue` emit 不变；`App.vue` 调 `execute`。
- 策略列表/schema 仍走 axios（非流式，无需改）。

---

## 6. 🌍 Epic 5：宏观锚点 + 钉钉风控网关

### 6.1 `core/notifier.py`（扩展）
```python
class DingTalkChannel(NotificationChannel):
    def __init__(self, webhook: str, secret: str): ...
    async def send(self, text: str) -> None:                    # aiohttp（决策点③）
        timestamp = str(int(time.time()*1000))
        string_to_sign = f"{timestamp}\n{secret}"
        sign = base64.b64encode(hmac.new(secret.encode(), string_to_sign.encode(),
                                        hashlib.sha256).digest()).decode()
        url = f"{self._webhook}&timestamp={timestamp}&sign={quote(sign)}"   # 显式加签，无黑盒
        # Markdown + 固定安全词【Quanter】，标题按级别着色
        async with aiohttp.ClientSession() as s:
            async with s.post(url, json={"msgtype":"markdown",
                "markdown":{"title":"【Quanter】风控告警","text":f"**【Quanter】**\n{text}"}}) as r:
                r.raise_for_status()

# build_default_manager() 增补：读 DINGTALK_WEBHOOK/SECRET 装配 DingTalkChannel
# ★ 并在 server/main.py lifespan 调用 build_default_manager()（现状漏接）
```

### 6.2 `data/clients/yfinance_client.py`（标普/原油/黄金/VIX）
```python
yfinance_breaker = CircuitBreaker(
    name="yfinance", failure_threshold=3, recovery_timeout=60,
    on_open=lambda: asyncio.create_task(
        NotificationManager.get_default().notify_risk_event("yfinance 接口熔断","WARN")))

class YFinanceClient:
    def get_history(self, symbol, start, end) -> pd.DataFrame:   # yfinance 库本身同步
        if not yfinance_breaker.allow_request(): return empty_df()             # ★ 不抛，返回空 DF
        try:
            raw = yf.download(symbol, start=start, end=end, progress=False)
            yfinance_breaker.record_success()
            return _cleanse(raw)                # 对齐 DatetimeIndex、数值列、剔 NaN
        except Exception:
            yfinance_breaker.record_failure()
            return empty_df()                   # ★ 红线：绝不抛到核心引擎
```

### 6.3 `data/clients/alpha_vantage_client.py`（美债收益率，双装饰器）
```python
av_limiter = RateLimiter("av", capacity=5, refill_rate=5/60)   # 5 calls/60s → 令牌桶语义映射
av_breaker = CircuitBreaker(name="alpha_vantage",
    on_open=lambda: asyncio.create_task(notifier.notify_risk_event("Alpha Vantage 熔断","WARN")))

class AlphaVantageClient:
    @av_limiter
    @av_breaker                                                  # ★ spec 要求双装饰器叠加
    async def get_treasury_yield(self, maturity: str, start, end) -> pd.DataFrame:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(f"https://www.alphavantage.co/query",
                            params={"function":"TREASURY_YIELD","interval":"daily",
                                    "maturity":maturity,"apikey":API_KEY})
            r.raise_for_status()
        return _cleanse(r.json())                  # 对齐 DatetimeIndex、数值列

# ★ service 层兜底：捕获 CircuitOpenError/DataFetchError → 返回空 DF（不抛）
```

---

## 7. 风控红线与边界自查（对齐架构师红线）

| 红线 | 落地保证 |
|---|---|
| 外部 I/O 不阻断核心 | yfinance/AV/tushare-lake/GLM/Redis 全部：限流+熔断+返回空/中性/降级，**绝不抛** |
| 限流/宕机告警 | yfinance/AV 熔断 `on_open` + Redis 宕机 → 钉钉异步告警 |
| SSE 协程安全 | `call_soon_threadsafe` + `QueueFull` 丢弃 + `finally unsubscribe`，无积压/泄漏 |
| 外部数据洗净 | yfinance/AV/tushare client 内部对齐 `DatetimeIndex` + 数值列 + 剔 NaN，对外只吐纯净 DF |
| 前视偏差 | 数据湖仅价格列沿时间 ffill，volume 不 ffill；qfq 快照标注重同步 |
| 事实无幻觉 | 所有接口签名已对照真实源码核实（见第 0 节现状表） |

## 8. 测试策略（对齐现有 17 个 pytest 文件风格）
- **resilience 复用**：不为已测的 `CircuitBreaker`/`RateLimiter` 重写测试。
- 每个新模块单测：
  - `tests/test_lake_reader.py`（ffill 仅价格、离线降级、截面/时序）
  - `tests/test_sync_data_lake.py`（mock pro，断点续传、空数据跳过）
  - `tests/test_llm_client.py`（凭证缺失降级、超时降级、JSON 非法降级）
  - `tests/test_sentiment_factor.py`（gather 单条失败不炸、全失败→0.0）
  - `tests/test_exploratory_momentum.py`（动量/波动员/赫斯特数值正确性）
  - `tests/test_factor_analyzer.py`（IC 方向性、分层单调性）
  - `tests/test_explorer_api.py`（CPU 探针拒绝、Redis 宕机降级 mock）
  - `tests/test_dingtalk_channel.py`（加签算法、monkeypatch aiohttp）
  - `tests/test_backtest_stream.py`（event_emitter 注入、流式 yield、断开清理）
- 现有回测/因子测试须**保持全绿**（`event_emitter` 默认 None 不破坏现有行为）。

## 9. 决策点记录（用户已认可）
| # | 决策 | 备选（未选） |
|---|---|---|
| ① | SSE 两步式 `POST /run/async` → `GET /run/stream/{id}` | fetch+ReadableStream POST 流（非原生 EventSource） |
| ② | 零依赖 `StreamingResponse` | sse-starlette `EventSourceResponse` |
| ③ | 钉钉用 `aiohttp`（spec 指定） | 统一 httpx |
| ④ | 数据湖 `pro_bar(adj='qfq')` | daily + adj_factor 手算 |
| ⑤ | Celery+Redis + psutil 探针 + Redis 宕机降级 | （用户已定 Celery） |
| ⑥ | GLM 走 openai SDK，默认 `glm-4-flash`，降级中性 | 默认 glm-4 |
| ⑦ | 新增 7 依赖（见 1.1） | — |

## 10. 建议实现顺序（供 writing-plans 参考）
1. **横切地基**（1.1–1.5：依赖、`.env.example`、config、lifespan 钩子）— 其余皆依赖它。
2. **Epic 5 容灾通道**（钉钉 + yfinance + AV）— 先通"告警/降级"基建，后续 Epic 异常才有出口。
3. **Epic 1 数据湖**（sync + reader）— 因子沙盒与截面推演的数据底座。
4. **Epic 2 GLM 情感**（独立，无下游阻塞）。
5. **Epic 3 因子沙盒**（依赖 1 + Celery）。
6. **Epic 4 SSE 回测流**（前后端联动，放最后做端到端联调）。
