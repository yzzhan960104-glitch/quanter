# Quanter 宏观 CTA 究极重构 — 总体设计文档

> **状态**：设计中，待用户终审 → 转入 writing-plans
> **日期**：2026-07-01
> **分支**：feat/macro-cta-refactor
> **范围**：自上而下信贷指引 + 纯血高频微观动量 的 Macro CTA 架构重构（5 Epic 后端 + 全链路前端可视化），合并 Data Lake Phase 2（JQData/Binance）
> **原则基线**：CLAUDE.md（全中文 + Karpathy 极简 + Grill Me 拷问 + 反黑盒）

---

## 0. 背景与战略转向

本轮是**战略级重构**，非增量功能。三大转向（用户已确认）：

| 转向 | 处置 | 理由 |
|---|---|---|
| **绝对禁止 Tushare** | 现有 Tushare 代码（`TushareDataFetcher` / `scripts/sync_data_lake.py`）**保留 dormant**，新数据流全走 **AKShare** | 用户要求 dormant 备用；不删降低风险，新流程不调用即合规 |
| **摒弃外部大模型（LLM）** | **移除** `core/llm_client.py` + `factors/alternative_sentiment.py` + 对应 tests；撤 `server/main.py` lifespan 的 GLMClient 装配 + `requirements.txt` 的 `openai` + `.env` 的 `ZHIPU_*` | 用户明确移除 |
| **纯 A 股 CTA** | Binance 加密沙盒**保留为可选**（低优先，最后做） | 用户保留作 7x24 极端市场测试 |

**架构主线**：宏观信贷（月频）→ 板块资金（日频）→ 微观量价（分钟频）的**自上而下漏斗**，层层递进筛选标的与仓位；`CreditRegime` 作为全系统一票否决的宏观锚。

### 0.1 复用 vs 新建 vs 移除（对齐已合并 master = b00a237）

| 能力 | 现状 | 本轮 |
|---|---|---|
| SSE per-run 回测流（engine event_emitter + `POST /run/async` + `GET /run/stream` + 前端 EventSource） | ✅ 已建 | **扩展**：止盈/止损/移动止损事件 + 分钟级 |
| 钉钉 `DingTalkChannel`（aiohttp+加签+errcode+结构化卡片+`fire_and_forget`） | ✅ 已建 | **扩展触发**：JQData 流量耗尽 / 宏观-1 清仓 / 单日回撤超阈 |
| `DataLakeReader` 单例 | ✅ 单湖（日线） | **扩展为多湖缓存**（Macro/Sector/Daily/1Min） |
| `data/resilience.py`（CircuitBreaker/RateLimiter） | ✅ | 复用，挂到 AKShare/JQData client |
| `backtest/engine.py` 事件驱动 | ✅ 日频 run/run_portfolio | **扩展 run_minute**（分钟级 + T+1 底仓冻结） |
| `trading/execution_gateway.py` / `order_state.py` | ✅ 基础 | **扩展**：CreditRegime 否决 + 止损/止盈/ATR移动止损 |
| GLMClient / NewsSentimentFactor | ✅（上轮） | **移除** |
| TushareDataFetcher / sync_data_lake.py | ✅（上轮） | **dormant**（不调用） |
| AKShare 宏观/板块/日线 / JQData 分钟 / Binance / CreditRegime / micro_momentum / 前端 dashboard | ❌ | **新建** |

### 0.2 用户已确认的决策
1. **推进方式**：一份大 spec 统一覆盖 5 Epic + 前端可视化（用户选）。
2. **Tushare**：保留 dormant；**LLM**：移除；**Binance**：保留可选沙盒。
3. **Phase 2 顺延决策**：jqdatasdk `1.9.8`（1.2.9 PyPI 不存在）；配额双机制（手动计数 + `get_query_count` 校准，spare<5万 停+告警）；DataLakeReader 多湖缓存（`lake=` 参数，默认 daily 向后兼容）；JQData 默认 5m。
4. **前端可视化**：所有相关功能都要前端可视化（双路由：`/` 回测终端 + `/dashboard` 宏观驾驶舱）。

### 0.3 外部 API 已逐一对已装版本核实（无幻觉）
- **AKShare 1.18.64**（实测）：`macro_china_shrzgm`(社融) / `macro_china_money_supply`(M1M2) / `repo_rate_hist`+`rate_interbank`(DR007) / `macro_china_shibor_all`(SHIBOR) / `stock_margin_detail_sse`+`stock_margin_detail_szse`(融资融券明细) / `stock_sector_fund_flow_rank`(板块主力) / `stock_individual_fund_flow`(个股主力) / `sw_index_first_info`(申万一级) / `stock_zh_a_hist(adjust='qfq')`(前复权日线)。
- **jqdatasdk 1.9.8**（实测）：`auth(user,pwd)` / `get_price(security,start,end,frequency='1m'/'5m',fields=[...],fq='pre')` / `get_query_count()` / `logout()`。
- **Binance Vision**（web 核实）：`https://data.binance.vision/data/spot/daily/klines/{symbol}/1m/{symbol}-1m-{YYYY-MM-DD}.zip`，CSV **12 列无表头**：`open_time(ms),open,high,low,close,volume,close_time(ms),quote_asset_volume,number_of_trades,taker_buy_base,taker_buy_quote,ignore`。

---

## 1. 横切关注点（地基）

### 1.1 依赖增量（`requirements.txt`）
```
akshare==1.18.64          # 宏观/板块/日线（替代 Tushare 数据流）
jqdatasdk==1.9.8          # 分钟级（spec 原 1.2.9 PyPI 不存在，改 1.9.8）
thriftpy2==0.4.20         # jqdatasdk 依赖
fastparquet               # parquet 备用引擎（与 pyarrow 并存）
```
**移除**：`openai`（LLM 已摒弃）。`aiohttp`/`pyarrow`/`tqdm`/`psutil` 上轮已有。AKShare 已随装 lxml/html5lib/jsonpath/openpyxl/xlrd 等。

### 1.2 环境变量（`.env.example` 增补）
```dotenv
# JQData（分钟级，Epic 1）
JQDATA_USERNAME=
JQDATA_PASSWORD=
# AKShare 免凭证（公开接口），无需 key
```
**移除** `.env.example` 与 `.env` 的 `ZHIPU_API_KEY`/`ZHIPU_BASE_URL`/`ZHIPU_MODEL`。

### 1.3 `config.py` 增量
```python
JQDATA_CONFIG = {"freq_default": "5m", "quota_warn_spare": 50_000,
                 "quota_manual_limit": 950_000, "calibrate_every": 10}
AKSHARE_CONFIG = {"qfq": "qfq", "recent_days_sector": 1, "active_pool_size": 50,
                  "top_sectors": 3, "momentum_window": 20}
LAKE_CONFIG["lakes"] = {  # 多湖路径注册
    "macro": "data_lake/macro_credit.parquet",
    "sector": "data_lake/sector.parquet",
    "daily": "data_lake/a_shares_daily.parquet",
    "minute": "data_lake/a_shares_1min.parquet",
    "crypto": "data_lake/crypto_btc_1m.parquet",
}
LAKE_CONFIG["default_lake"] = "daily"
```

### 1.4 LLM 移除清单（一次性）
- 删 `core/llm_client.py`、`factors/alternative_sentiment.py`、`tests/test_llm_client.py`、`tests/test_sentiment_factor.py`。
- `server/main.py` lifespan：删 `GLMClient.get_instance()` 装配与 import。
- `requirements.txt`：删 `openai`。
- `.env` / `.env.example`：删 `ZHIPU_*`。

### 1.5 `server/main.py` lifespan 增量
- 移除 GLMClient（见 1.4）。
- `DataLakeReader.get_instance()` 改为按 `LAKE_CONFIG["lakes"]` **逐个 load 存在的湖**，默认湖 = `LAKE_CONFIG["default_lake"]`。
- `build_default_manager()`（钉钉）保留。

---

## 2. 🏦 Epic 1：自上而下四级数据湖

数据流：**宏观（月频）→ 板块资金（日频）→ 50 只活跃股日线（日频）→ 这 50 只的分钟级（分钟频）**，层层递进。

### 2.1 `scripts/sync_macro_credit.py`（宏观信贷同步器）
**职责**：AKShare 拉宏观信贷三件套，落日频对齐 parquet。
```python
def sync_macro(start, end) -> pd.DataFrame:
    shrzgm = ak.macro_china_shrzgm()          # 社融规模增量（月频）
    money  = ak.macro_china_money_supply()    # M0/M1/M2 + 同比（月频）
    dr007  = ak.repo_rate_hist(...)           # DR007 回购利率（日频）
    shibor = ak.macro_china_shibor_all()      # SHIBOR（日频）
    # 合并 → reindex 到日历日 → 仅向前 ffill（月频→日频，防前视）→ data_lake/macro_credit.parquet
```
- **前视红线**：月频宏观只能向前 ffill 到当日（用过去值解释现在），绝不用未来月度值回填过去。
- 复用 `tushare_breaker` 范式为 AKShare 建独立 `akshare_breaker`/`akshare_limiter`（手动 API，失败返空 DF 不抛）。

### 2.2 `scripts/sync_sector_daily.py`（板块两融 + 活跃股初筛）
**职责**：盘后拉融资融券 + 主力资金，选 top-3 信贷扩张板块 + 50 只活跃股 + 其日线。
```python
def select_active_pool(date) -> list[str]:
    # 1) 融资融券明细：stock_margin_detail_sse + stock_margin_detail_szse → 按标的合并融资余额
    # 2) 申万一级归属：sw_index_first_info + 个股行业映射
    # 3) groupby 行业 → 算【融资余额环比增速】→ 取前 top_sectors(3) 板块
    # 4) top-3 板块内个股：stock_individual_fund_flow 取主力净流入
    #    + 过去 momentum_window(20) 日换手率/动量 → 排序取 active_pool_size(50)
    pool = top50_by_turnover_and_momentum(top3_sectors)
    return pool

def sync_sector_daily(pool) -> None:
    # 落板块资金流 → data_lake/sector.parquet（含 top-3 + 全板块排名 + 活跃池）
    # 拉这 50 只前复权日线：ak.stock_zh_a_hist(symbol, adjust='qfq')
    # → data_lake/a_shares_daily.parquet（MultiIndex date,symbol；仅这 50 只，非全市场）
```
- 活跃股池**每日动态**（依赖当日板块信贷）。
- `stock_zh_a_hist` 限频 → 复用 `akshare_limiter`。

### 2.3 `scripts/sync_jqdata_1min.py`（JQData 高频精准狙击）
**职责**：对**当日活跃股池（50 只）**拉近 3 月 1m/5m，落分钟湖。
```python
def sync_jqdata_1min(pool, months=3, freq="5m"):
    client = JQDataClient.get_instance()
    for symbol in tqdm(pool):
        shard = f"data_lake/jq_shards/{symbol}_{freq}.parquet"
        if exists(shard): continue            # 断点续传
        df = client.fetch_minute_bars(symbol, start, end, frequency=freq)  # 配额双机制
        if df.empty: continue
        df.to_parquet(shard)
    build_multiindex("data_lake/jq_shards", f"data_lake/a_shares_{freq}.parquet")
```
- `QuotaExceeded` → 优雅停（"今日额度将尽，明日重跑续传"），不崩。

### 2.4 `data/clients/jqdata_client.py`（聚宽安全客户端，Phase 2 顺延）
**单例 + `threading.Lock`（聚宽单连接禁并发）+ 配额双机制**：
```python
class JQDataClient:
    def __init__(self):
        self._lock = threading.Lock()
        self._enabled = bool(JQDATA_USERNAME)  # 缺凭证降级
        if self._enabled: jqdatasdk.auth(user, pwd); self._last_calibrate = 0
        self._today_count = 0; self._today = date.today()
    def fetch_minute_bars(self, symbol, start, end, frequency="5m") -> pd.DataFrame:
        with self._lock:                                   # 单连接串行
            self._reset_if_new_day()
            if self._near_limit():                         # 手动计数或校准达阈值
                fire_and_forget(notify_risk_event("JQData 日额度将尽，已停拉取", "WARN"))
                raise QuotaExceeded("JQData spare < 5万")
            if self._last_calibrate % calibrate_every == 0:
                self._today_count = total - jqdatasdk.get_query_count()["spare"]  # 权威校准
            df = jqdatasdk.get_price(symbol, start, end, frequency=frequency,
                                     fields=["open","high","low","close","volume","money"],
                                     fq="pre", skip_paused=False)
            self._today_count += len(df); self._last_calibrate += 1
            return self._cleanse(df)                       # money→amount, tz Asia/Shanghai
```
- **红线**：绝不超日限额；锁防并发；`spare<5万` 或 `_today_count≥95万` → 抛 + 钉钉告警。

### 2.5 `data/lake_reader.py`（多湖缓存扩展，向后兼容）
```python
class DataLakeReader:
    self._lakes: dict[str, pd.DataFrame]; self._ffills: dict[str, pd.DataFrame]
    self._default_key: str
    def load(self, path=None, *, key=None) -> None      # key 缺省=path；缓存 {key:(df,ffill)}
    def get_cross_section(self, date, *, lake=None)     # lake 缺省=默认湖
    def get_timeseries(self, symbol, start, end, *, lake=None)
    @property loaded -> bool                            # 任一湖已载即 True
    def lakes(self) -> list[str]
```
- **向后兼容**：现有调用方（因子沙盒 `get_timeseries(symbol,start,end)` 不传 lake）→ 走默认湖，零回归。
- **ffill 红线不变**：仅价格 `open/high/low/close` 沿时间 ffill；`volume/amount` 绝不 ffill。
- 多湖各自维护 `_ffill`（groupby level=symbol 不跨标的）。

### 2.6 `scripts/sync_binance_vision.py`（可选加密沙盒，低优先）
- `aiohttp` + `semaphore(8)` 下载过去 30 天 `BTCUSDT-1m-{date}.zip`；**404 跳过**。
- stdlib `zipfile` 解压 → CSV 赋 12 列名 → `open_time(ms)→UTC datetime` 索引 → 映射 `open/high/low/close/volume/amount(=quote_asset_volume)`。
- concat → MultiIndex(date,symbol) → 增量写 `data_lake/crypto_btc_1m.parquet`；清理临时 `.zip/.csv`。

### 2.7 数据格式统一（审查标准）
四湖 + 加密湖统一 `MultiIndex(date, symbol)` + `open/high/low/close/volume/amount`；`DataLakeReader` ffill 逻辑通用。`amount` 语义统一：A 股 `money`=CNY 成交额、加密 `quote_asset_volume`=USDT 成交额。

---

## 3. 🔬 Epic 2：因子沙盒

### 3.1 `factors/macro_regime.py`（宏观状态评估机）
**日频 `CreditRegime` 信号**：融合社融增速 + M1M2 剪刀差 + DR007 趋势。**单例 `get_default()`**（启动期装配，lifespan 内 `CreditRegime.get_default()`，网关/前端共用同一实例）。
```python
class CreditRegime:
    @classmethod
    def get_default(cls) -> "CreditRegime": ...   # 双重检查锁单例
    def compute(self, date) -> int:   # +1 扩张 / 0 中性 / -1 收缩
        shrzgm_yoy = macro["社融增速"].loc[:date]   # 已 ffill 到日频，无前视
        m1m2_gap   = macro["M1同比"] - macro["M2同比"]
        dr007_trend = dr007.rolling(20).mean().diff()
        # 规则：社融↑ + 剪刀差扩张 + DR007下行 → +1（放大仓位）
        #       反向 → -1（严格防守）；否则 0
        ...
```
- **无前视**：`.loc[:date]` 严格只用当日及之前；月频已向前 ffill。

### 3.2 `factors/micro_momentum.py`（动量爆发 + ATR 波动率）
基于 1m K 线：
- **微观爆发因子**：均线密集发散（短长期 MA 聚拢后突破）、突破策略信号。
- **ATR 波动率因子**：`ATR = mean((high-low).rolling(window))`；**Risk Parity 头寸**：`目标头寸 ∝ 1/ATR`，波动越大头寸越小，控单笔回撤。
- 纯 Pandas 向量化，复用上轮 `factors/exploratory_momentum.py` 的赫斯顿/横截面范式。

### 3.3 现有因子处置
`factors/technical.py`/`macro.py`/`fusion.py`/`hmm_macro.py`/`mytt.py`/`exploratory_momentum.py`/`analyzer.py` **保留不删**（非 LLM、非 Tushare）；新 CTA 策略用新的 `macro_regime` + `micro_momentum`，旧因子作备用。

---

## 4. ⚔️ Epic 3：执行网关 + 订单状态机

### 4.1 `trading/execution_gateway.py`（融合型执行网关，扩展）
```python
class MacroAwareGateway(BaseExecutionGateway):
    async def submit_order(self, order):
        regime = CreditRegime.get_default().compute(today)
        if regime == -1 and order.side == BUY:
            # 宏观一票否决：收缩期 + 买入突破 → 拦截 或 强制仓位减半
            if self.strict_veto: raise VetoedError("宏观收缩期，否决买入")
            order.quantity //= 2                    # 强制减半
        await self._route(order)
```
- 否决/减半策略可配（`strict_veto` 默认减半，避免完全停摆）。

### 4.2 `trading/order_state.py`（订单状态机，扩展纯逻辑）
新增三道出场逻辑（纯函数，可单测）：
- **固定止损 StopLoss**：`price ≤ entry*(1-sl_pct)` → 平仓。
- **固定止盈 TakeProfit**：`price ≥ entry*(1+tp_pct)` → 平仓。
- **ATR 移动止损 TrailingStop**：`stop = max(stop, high - atr*k)`，触发即平，锁浮盈。

### 4.3 `backtest/engine.py`（分钟级 + T+1 底仓冻结）
新增 `run_minute(df_1m, signal, regime, ...) `：
- 逐分钟遍历；每分钟检查止损/止盈/移动止损触发。
- **T+1 底仓冻结感知**：维护 `position_t1`（昨日及更早建仓 = 底仓，可卖）与 `position_frozen`（今日新仓，冻结至次日）。变相 T+0：底仓可日内卖、新仓次日解冻。
- 复用 `event_emitter`：触发止盈/止损时 yield `{"type":"risk","level":"WARN","reason":"触及止损","date":...}`。

---

## 5. 🌟 Epic 4：SSE 实时流（已建，扩展）

- 引擎 `event_emitter` 已有 trade/progress/risk。**扩展**：`run_minute` 触发止盈/止损/移动止损 → yield `risk` 事件（`[WARN-STOPLOSS]` 语义）。
- 后端 per-run SSE 流（`POST /run/async` + `GET /run/stream`，`_RunBridge` call_soon_threadsafe + `jsonable_encoder`）已建，分钟级直接复用。
- 前端 EventSource 已建；`useTerminalState.toLogEntry` 补 `risk` reason=触及止损/止盈 → `lv-warn`/`lv-error` 高亮。
- **协程安全红线**：复用已验证的 call_soon_threadsafe + QueueFull 丢弃 + finally 清理。

---

## 6. 🌍 Epic 5：钉钉预警（已建，扩展触发）

`DingTalkChannel` + `fire_and_forget` 已就绪。**新增触发场景**：
1. **JQData 流量耗尽**：`JQDataClient.QuotaExceeded` → `fire_and_forget(notify_risk_event(...,"WARN"))`。
2. **外部 API 熔断**：AKShare/JQData `CircuitBreaker.on_open`（复用现有范式）。
3. **宏观 -1 触发清仓**：`CreditRegime == -1` 且网关否决/减半时告警。
4. **单日回撤超阈**：回测/实盘日回撤 > 阈值 → 告警。
均经 `NotificationManager` 多通道软降级，固定安全词 `【Quanter】`。

---

## 7. 前端可视化（全链路）

### 7.1 配套后端只读端点（读内存湖，零写入）
- `GET /api/v1/macro/regime` → 当前 CreditRegime + 近 N 日历史色带 + 触发理由。
- `GET /api/v1/macro/credit` → 社融增速 / M1M2剪刀差 / DR007 三联时序。
- `GET /api/v1/sector/flow` → 申万板块融资余额增速排名 + top-3 + 当日活跃股池（换手率/动量）。
- `GET /api/v1/factors/{symbol}` → ATR / 微观动量值。
- 回测响应扩展：分钟级 ohlcv + 止损/止盈/移动止损点位（供 ProChart 标注）。

### 7.2 前端布局（恢复 vue-router 双路由）
- **`/` 回测终端**（现有，扩展）：
  - ProChart 支持**分钟级 K 线** + **止损/止盈/移动止损水平线 + 触发标注**。
  - 新增**因子面板**（ATR / micro-momentum sparkline）。
  - SSE 终端补 `[WARN-STOPLOSS]` 级别高亮。
- **`/dashboard` 宏观·板块驾驶舱**（新增）：
  - CreditRegime 大号状态卡 + 历史色带 timeline。
  - 社融 / M1M2剪刀差 / DR007 三联折线（ECharts）。
  - 板块热力图（融资余额环比增速，top-3 高亮）。
  - 活跃股池表（50 只，换手率/动量列；点击跳回测终端预填标的）。

### 7.3 技术栈全复用
Vue3 + ECharts(`vue-echarts`) + Element Plus（皆已装）；axios 取快照、EventSource 跑回测流。状态沿用模块级 reactive 单例（不引 Pinia）。

---

## 8. 风控红线自查（对齐架构师红线）
- **数据无前视**：宏观(月)→板块(日)→量价(分钟) 拼接一律**仅向前 ffill**；`.loc[:date]` 严格时间门控。
- **外部 I/O 不阻断**：AKShare/JQData/Binance 全部限流+熔断+返空降级，绝不抛到核心；钉钉异步告警。
- **JQData 不超限**：双机制（手动 + `get_query_count` 校准）+ buffer + 锁防并发 + 临限告警。
- **协程安全**：SSE 复用 call_soon_threadsafe + QueueFull 丢弃 + finally 清理。
- **零回归**：DataLakeReader 多湖 `lake=` 默认值保旧调用方不破；`event_emitter` 默认 None。

## 9. 测试策略（pytest，对齐现有范式）
- `test_jqdata_client.py`：mock jqdatasdk，测配额阈值/告警/锁/登录失败降级/洗净。
- `test_sync_macro_credit.py` / `test_sync_sector_daily.py` / `test_sync_jqdata_1min.py`：mock AKShare/JQData，测 ffill 无前视、活跃池筛选、断点续传、优雅停。
- `test_lake_reader_multilake.py`：多湖 load + `lake=` + 向后兼容 + 价格ffill不ffill量。
- `test_macro_regime.py` / `test_micro_momentum.py`：规则方向性 + ATR Risk Parity 头寸。
- `test_execution_gateway_veto.py` / `test_order_state_stops.py`：宏观否决 + 止损/止盈/移动止损纯逻辑 + T+1 冻结。
- `test_binance_vision.py`（可选）：mock aiohttp 200+zip / 404 跳过 / 解析 / 清理。
- 前端：`vue-tsc --noEmit && npm run build` 通过（无单测范式）。
- 现有套件基线 35 预存失败不新增；LLM 移除后相关 4 测试随之删除（基线下调）。

## 10. 决策点记录（用户已确认）
1. Tushare 保留 dormant；LLM 移除；Binance 保留可选沙盒。
2. 一份大 spec 统一覆盖 5 Epic + 前端可视化。
3. Phase 2 顺延：jqdatasdk 1.9.8 / 配额双机制 / 多湖 DataLakeReader / JQData 5m 默认。
4. 前端双路由（`/` 终端 + `/dashboard` 驾驶舱）。
5. AKShare 函数名按已装 1.18.64 实测（spec 原文 2 处笔误已修正）。

## 11. 建议实现顺序（供 writing-plans 参考）
1. **横切地基**：依赖/env/config + **LLM 移除**（先清，避免后续依赖混乱）。
2. **Epic 1 数据骨架**：AKShare 宏观 → 板块+活跃池+日线 → DataLakeReader 多湖 → JQData 分钟。（Binance 可选最后）
3. **Epic 2 因子**：CreditRegime → micro_momentum+ATR。
4. **Epic 3 网关+订单**：宏观否决 → 止损/止盈/移动止损 → run_minute + T+1。
5. **前端可视化**：只读端点 → `/dashboard` 驾驶舱 → 终端扩展（分钟+止盈止损标注）。
6. **Epic 4/5 扩展**：SSE 止损事件高亮 + 钉钉新触发。
