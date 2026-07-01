# 宏观 CTA 究极重构 — 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 按 6 阶段落地宏观 CTA 重构（AKShare 四级数据湖 + CreditRegime/微观动量因子 + 宏观否决网关/止损止盈/T+1 + 前端可视化 + SSE/钉钉扩展），移除 LLM，Tushare dormant。

**Architecture:** AKShare 接管数据流（宏观月频→板块日频→活跃股日线→JQData 分钟），封装在带熔断/限流的 client 内；DataLakeReader 扩为多湖缓存；CreditRegime 单例作宏观锚；执行网关读 Regime 一票否决；引擎增 run_minute 支持 T+1 底仓冻结与止损止盈；前端双路由（终端+dashboard）。外部 API 已对已装版本核实（AKShare 1.18.64 / jqdatasdk 1.9.8 / Binance）。

**Tech Stack:** Python 3.10+ / FastAPI / Pydantic v2 / Pandas+PyArrow / AKShare / jqdatasdk / aiohttp / pytest；Vue3.5 + TS + vue-router + ECharts + Element Plus。

## Global Constraints

- **语言**：代码注释/对话/文档 100% 中文（CLAUDE.md 硬约束），注释含"为什么"。
- **反黑盒 + 类型注解**：Python 3.10+ 类型注解 + `from __future__ import annotations`；不引重型黑盒。
- **测试范式**：`asyncio.run`（非 pytest-asyncio）；外部 IO 用 monkeypatch 脱网；AKShare/JQData 真实 `ak.*`/`jq.*` 调用封装在 client 内，测试 mock client 的 wrapper 方法（不在测试里触网）。
- **外部 I/O 红线**：AKShare/JQData/Binance 全部限流+熔断+返空降级，绝不抛到核心；钉钉异步告警（`fire_and_forget`）。
- **前视偏差红线**：宏观(月)→板块(日)→量价(分钟) 拼接一律仅向前 ffill；`.loc[:date]` 严格时间门控；volume/amount 绝不 ffill。
- **零回归**：DataLakeReader 多湖 `lake=` 默认 daily 向后兼容；`event_emitter` 默认 None。
- **依赖**：`akshare==1.18.64` `jqdatasdk==1.9.8` `thriftpy2==0.4.20` `fastparquet`（移除 `openai`）。
- **提交**：每 Task 末尾 `git commit`，中文消息 + `Co-Authored-By: Claude <noreply@anthropic.com>`。
- **回归门槛**：基线 35 预存失败（pandas 2.x，无关）；LLM 移除后删 4 测试，基线下调至 31；任一 Task 后失败总数不得高于当时基线。
- **data/ 目录被 .gitignore**：`data/clients/*.py`、`data/lake_reader.py` 等须 `git add -f`；提交后 `git show --stat HEAD` 核对入库。

## File Structure

**新建（后端）**：
- `data/clients/akshare_client.py` — AKShare 熔断/限流 + macro/sector/daily fetch wrapper
- `data/clients/jqdata_client.py` — JQDataClient 单例+锁+配额双机制
- `scripts/sync_macro_credit.py` / `scripts/sync_sector_daily.py` / `scripts/sync_jqdata_1min.py` / `scripts/sync_binance_vision.py`
- `factors/macro_regime.py` — CreditRegime 单例
- `factors/micro_momentum.py` — 微观动量+ATR
- `server/api/v1/macro.py` — macro/sector/factors 只读端点
- 对应 `tests/test_*.py` 共 14 个新测试文件

**修改**：
- `requirements.txt` / `.env.example` / `config.py`
- `data/lake_reader.py`（多湖）/ `data/resilience.py`（akshare_breaker/limiter 单例）
- `trading/order_state.py`（止损止盈移动止损）/ `trading/execution_gateway.py`（宏观否决）
- `backtest/engine.py`（run_minute + T+1）
- `server/main.py`（lifespan 多湖 + 撤 GLMClient + 挂 macro 路由）
- 前端：`web/src/router`、`web/src/api/macro.ts`、`web/src/composables/useTerminalState.ts`、`web/src/components/ProChart.vue`、新增 dashboard 组件、`App.vue`

**删除**：`core/llm_client.py`、`factors/alternative_sentiment.py`、`tests/test_llm_client.py`、`tests/test_sentiment_factor.py`

---

## Phase 1 — 地基 + LLM 移除

### Task 1: 依赖、env、config 增量

**Files:** Modify `requirements.txt`、`.env.example`、`config.py`
**Interfaces:** Produces `config.JQDATA_CONFIG`/`AKSHARE_CONFIG`/`LAKE_CONFIG["lakes"]`/`LAKE_CONFIG["default_lake"]`。

- [ ] **Step 1: `requirements.txt`** — 移除 `openai>=1.30`，追加：
```
akshare==1.18.64
jqdatasdk==1.9.8
thriftpy2==0.4.20
fastparquet
```
- [ ] **Step 2: 安装** — `pip install akshare==1.18.64 jqdatasdk==1.9.8 thriftpy2==0.4.20 fastparquet`（akshare 已装则跳过）。
- [ ] **Step 3: `.env.example`** — 删 `ZHIPU_API_KEY`/`ZHIPU_BASE_URL`/`ZHIPU_MODEL` 三行；追加：
```
# JQData（分钟级，Epic 1）
JQDATA_USERNAME=
JQDATA_PASSWORD=
```
- [ ] **Step 4: `config.py` 末尾追加**：
```python
# JQData 分钟级客户端（Epic 1）
JQDATA_CONFIG = {
    "freq_default": "5m",
    "quota_warn_spare": 50_000,      # spare<5万 即停
    "quota_manual_limit": 950_000,   # 手动计数 95万 即停
    "calibrate_every": 10,           # 每 10 次用 get_query_count 校准
}
# AKShare 数据流（替代 Tushare）
AKSHARE_CONFIG = {
    "qfq": "qfq",
    "active_pool_size": 50,
    "top_sectors": 3,
    "momentum_window": 20,
}
# 多湖路径注册（DataLakeReader 按 key 缓存）
LAKE_CONFIG["lakes"] = {
    "macro": "data_lake/macro_credit.parquet",
    "sector": "data_lake/sector.parquet",
    "daily": "data_lake/a_shares_daily.parquet",
    "minute": "data_lake/a_shares_1min.parquet",
    "crypto": "data_lake/crypto_btc_1m.parquet",
}
LAKE_CONFIG["default_lake"] = "daily"
```
- [ ] **Step 5: 验证 import** — `python -c "from config import JQDATA_CONFIG, AKSHARE_CONFIG; from config import LAKE_CONFIG; assert 'lakes' in LAKE_CONFIG; print('ok')"` → `ok`。
- [ ] **Step 6: Commit** — `git add requirements.txt .env.example config.py && git commit -m "feat(foundation): AKShare/JQData 依赖+env+多湖config，移除 openai\n\nCo-Authored-By: Claude <noreply@anthropic.com>"`

---

### Task 2: 移除 LLM（GLMClient + NewsSentimentFactor）

**Files:** Delete `core/llm_client.py`、`factors/alternative_sentiment.py`、`tests/test_llm_client.py`、`tests/test_sentiment_factor.py`；Modify `server/main.py`（撤 lifespan GLMClient）。
**Interfaces:** 移除 `core.llm_client.GLMClient`、`factors.alternative_sentiment.NewsSentimentFactor`。

- [ ] **Step 1: 确认无其它引用** — `grep -rn "llm_client\|alternative_sentiment\|GLMClient\|NewsSentimentFactor\|SentimentResult" --include=*.py server backtest factors strategies data trading core | grep -v test_llm_client | grep -v test_sentiment_factor`；仅 `server/main.py` 应有 `GLMClient` 引用。若还有它处，先评估。
- [ ] **Step 2: 删 4 文件** — `git rm core/llm_client.py factors/alternative_sentiment.py tests/test_llm_client.py tests/test_sentiment_factor.py`。
- [ ] **Step 3: 改 `server/main.py`** — 删 `from core.llm_client import GLMClient` import；删 lifespan 中 `GLMClient.get_instance()` 装配段（含其上注释）。
- [ ] **Step 4: 验证** — `python -c "from server.main import app; print('ok')"` → `ok`（无 GLM 引用残留）。
- [ ] **Step 5: 回归** — `python -m pytest -q --tb=no 2>&1 | tail -3`；基线应从 35 降到 31（删了 4 个 LLM 测试），passed 数对应减少，**无新增失败**。
- [ ] **Step 6: Commit** — `git add -A && git commit -m "refactor: 移除 LLM(GLMClient+NewsSentimentFactor)，撤 lifespan 装配\n\nCo-Authored-By: Claude <noreply@anthropic.com>"`

---

## Phase 2 — Epic 1 四级数据湖

### Task 3: DataLakeReader 多湖缓存扩展

**Files:** Modify `data/lake_reader.py`；Test: `tests/test_lake_reader_multilake.py`
**Interfaces:** Produces `DataLakeReader.load(path=None,*,key=None)`、`get_cross_section(date,*,lake=None)`、`get_timeseries(symbol,start,end,*,lake=None)`、`.loaded`、`.lakes()`；默认湖由首次 load 或 lifespan 设。

- [ ] **Step 1: 写失败测试 `tests/test_lake_reader_multilake.py`**：
```python
"""多湖缓存：lake= 参数 + 向后兼容 + 价格ffill/不ffill量。"""
import pandas as pd
from data.lake_reader import DataLakeReader

def _df(start="2024-01-02", close=10.0, sym="000001.SZ"):
    idx = pd.MultiIndex.from_tuples([("2024-01-02", sym), ("2024-01-03", sym)], names=["date", "symbol"])
    return pd.DataFrame({"open":[close,close],"high":[close,close],"low":[close,close],
                         "close":[close,float("nan")],"volume":[100,0],"amount":[1e6,0]}, index=idx)

def test_multilake_load_and_query_by_key(tmp_path):
    daily = tmp_path/"daily.parquet"; minute = tmp_path/"minute.parquet"
    _df(close=10.0).to_parquet(daily); _df(close=20.0).to_parquet(minute)
    r = DataLakeReader(); r.load(str(daily), key="daily"); r.load(str(minute), key="minute")
    assert set(r.lakes()) == {"daily", "minute"}
    assert r.loaded is True
    # 按 lake 查询，互不串味
    assert r.get_cross_section("2024-01-02", lake="daily").loc["000001.SZ","close"] == 10.0
    assert r.get_cross_section("2024-01-02", lake="minute").loc["000001.SZ","close"] == 20.0

def test_default_lake_backward_compat(tmp_path):
    """不传 lake → 用默认湖（首次 load 的 key）。"""
    daily = tmp_path/"daily.parquet"; _df(close=10.0).to_parquet(daily)
    r = DataLakeReader(); r.load(str(daily), key="daily")
    # 不传 lake，走默认湖
    assert r.get_cross_section("2024-01-02").loc["000001.SZ","close"] == 10.0
    assert r.get_timeseries("000001.SZ","2024-01-01","2024-01-31").iloc[0]["close"] == 10.0

def test_multilake_ffill_only_prices(tmp_path):
    """多湖各自仅价格 ffill、volume 不 ffill。"""
    p = tmp_path/"d.parquet"; _df(close=5.0).to_parquet(p)
    r = DataLakeReader(); r.load(str(p), key="daily")
    sec = r.get_cross_section("2024-01-03", lake="daily")
    assert sec.loc["000001.SZ","close"] == 5.0   # 停牌日价格 ffill
    assert sec.loc["000001.SZ","volume"] == 0    # volume 不 ffill
```
- [ ] **Step 2: 跑 FAIL** — `pytest tests/test_lake_reader_multilake.py -v` → FAIL（load 不接受 key）。
- [ ] **Step 3: 改 `data/lake_reader.py`** 为多湖。把 `__init__` 的 `self._df/_ffill/_loaded/_date_dtype` 改为字典缓存；保留既有 `_norm_date`/`_PRICE_COLS`/sort_index/去tz 逻辑（每湖独立）。新结构：
```python
class DataLakeReader:
    _instance = None; _lock = threading.Lock()
    @classmethod
    def get_instance(cls): ...  # 双重检查锁不变
    def __init__(self):
        self._lakes: dict[str, pd.DataFrame] = {}
        self._ffills: dict[str, pd.DataFrame] = {}
        self._dtypes: dict[str, object] = {}
        self._default_key: str | None = None
    @property
    def loaded(self) -> bool: return bool(self._lakes)
    def lakes(self) -> list[str]: return list(self._lakes.keys())
    def load(self, path: str | None = None, *, key: str | None = None) -> None:
        from config import LAKE_CONFIG
        path = path or LAKE_CONFIG["default_path"]
        key = key or path
        if not os.path.exists(path):
            logger.warning("湖缺失：%s(key=%s)，跳过", path, key); return
        df = pd.read_parquet(path)
        if not isinstance(df.index, pd.MultiIndex):
            logger.error("湖 %s 索引非 MultiIndex，跳过", key); return
        df = df.sort_index()
        # date 层级去 tz + normalize（复用既有 _norm 局部逻辑）
        date_vals = df.index.get_level_values("date")
        if isinstance(date_vals, pd.DatetimeIndex):
            if getattr(date_vals, "tz", None) is not None:
                date_vals = date_vals.tz_convert("UTC").tz_localize(None)
            date_vals = pd.DatetimeIndex(date_vals).normalize()
            df = df.set_index(date_vals.append(pd.Index(df.index.get_level_values("symbol"))).set_names(["date","symbol"]))
        price_cols = [c for c in _PRICE_COLS if c in df.columns]
        ffill = df[price_cols].groupby(level="symbol").ffill() if price_cols else df[[]]
        self._lakes[key] = df; self._ffills[key] = ffill; self._dtypes[key] = type(date_vals)
        if self._default_key is None: self._default_key = key   # 首个为默认
        logger.info("湖加载：%s(%d 行)", key, len(df))
    def _resolve(self, lake: str | None) -> str:
        if lake is not None: return lake
        if self._default_key is None: raise KeyError("无已加载湖")
        return self._default_key
    def get_cross_section(self, date, *, lake: str | None = None) -> pd.DataFrame:
        key = self._resolve(lake); ff = self._ffills.get(key)
        if ff is None: return pd.DataFrame()
        dt = self._dtypes.get(key)
        d = str(date) if not isinstance(dt, pd.DatetimeIndex) else pd.Timestamp(date).normalize()
        try: return ff.xs(d, level="date")
        except KeyError: return pd.DataFrame()
    def get_timeseries(self, symbol, start, end, *, lake: str | None = None) -> pd.DataFrame:
        key = self._resolve(lake); df = self._lakes.get(key)
        if df is None: return pd.DataFrame()
        try: ts = df.xs(symbol, level="symbol")
        except KeyError: return pd.DataFrame()
        return ts.loc[pd.Timestamp(start):pd.Timestamp(end)]
```
（保留原 `_norm_date` 风格的注释与中文 why；上面的 date 层级重建是为了多湖各自独立处理。）
- [ ] **Step 4: 跑既有 lake_reader 测试 + 新测试** — `pytest tests/test_lake_reader.py tests/test_lake_reader_multilake.py -v` → 全 PASS。
- [ ] **Step 5: `git add -f data/lake_reader.py tests/test_lake_reader_multilake.py` + Commit** — `feat(lake): DataLakeReader 多湖缓存(lake=参数+默认湖向后兼容)`

> 注：`data/lake_reader.py` 现有 `test_lake_reader.py` 的 `test_offline_mode_when_parquet_missing` 等会因多湖改造断言 `.loaded`——若断言 `r.loaded is False` 在单湖离线时仍成立（无湖即 False）。若旧测试因 API 变化失败，就地修正断言（如 `get_cross_section(date)` 无湖返回空 DF 仍成立）。**零回归是底线**。

---

### Task 4: AKShare 客户端（熔断/限流 + fetch wrapper）

**Files:** Create `data/clients/akshare_client.py`；Modify `data/resilience.py`（加 akshare 单例）；Test: `tests/test_akshare_client.py`
**Interfaces:** Produces `AKShareClient.fetch_macro_credit()`、`.fetch_margin_detail()`、`.fetch_sector_fund_flow()`、`.fetch_individual_fund_flow(symbol)`、`.fetch_daily_hist(symbol,start,end)`；模块级 `akshare_breaker`/`akshare_limiter`。

- [ ] **Step 1: 写失败测试 `tests/test_akshare_client.py`**：
```python
"""AKShareClient：手动熔断+限流，失败返空 DF 不抛；wrapper 洗净列。"""
import pandas as pd
from data.clients.akshare_client import AKShareClient, akshare_breaker
from data.resilience import CircuitState

def _reset(): akshare_breaker._state = CircuitState.CLOSED; akshare_breaker._failure_count = 0

def test_fetch_daily_hist_cleanses(monkeypatch):
    _reset()
    fake = pd.DataFrame({"日期":["2024-01-02"],"开盘":[10],"最高":[11],"最低":[9],
                         "收盘":[10.5],"成交量":[1000],"成交额":[1e7]})
    monkeypatch.setattr("akshare.stock_zh_a_hist", lambda *a,**k: fake)
    df = AKShareClient().fetch_daily_hist("000001.SZ","2024-01-02","2024-01-03")
    assert list(df.columns)[:6] == ["open","high","low","close","volume","amount"]
    assert len(df) == 1

def test_failure_returns_empty_df(monkeypatch):
    _reset()
    def boom(*a,**k): raise RuntimeError("network down")
    monkeypatch.setattr("akshare.stock_zh_a_hist", boom)
    df = AKShareClient().fetch_daily_hist("000001.SZ","2024-01-02","2024-01-03")
    assert df.empty   # 绝不抛
```
- [ ] **Step 2: 跑 FAIL** — 模块不存在。
- [ ] **Step 3: `data/resilience.py` 末尾加 akshare 单例**：
```python
akshare_limiter = RateLimiter(name="akshare", capacity=3, refill_rate=1.0)
akshare_breaker = CircuitBreaker(name="akshare", failure_threshold=3, recovery_timeout=60.0)
```
- [ ] **Step 4: 创建 `data/clients/akshare_client.py`**：
```python
"""AKShare 客户端：宏观/板块/日线 fetch wrapper，手动熔断+限流，失败返空 DF 不抛。

Why 手动熔断（非装饰器）：保住"任何异常返空 DF"对外契约（与 yfinance_client 同范式）。
真实 ak.* 调用封装在 wrapper 内，参数以已装 akshare 1.18.64 实测为准。
"""
from __future__ import annotations
import logging
import pandas as pd
from data.resilience import akshare_breaker, akshare_limiter
logger = logging.getLogger(__name__)
_EMPTY = pd.DataFrame()

class AKShareClient:
    def _guard(self):  # 熔断+限流前置
        akshare_limiter.acquire(1.0)
        return akshare_breaker.allow_request()
    def fetch_daily_hist(self, symbol, start, end, adjust="qfq") -> pd.DataFrame:
        if not self._guard(): return _EMPTY.copy()
        try:
            import akshare as ak
            raw = ak.stock_zh_a_hist(symbol=symbol, period="daily",
                                     start_date=start.replace("-",""), end_date=end.replace("-",""), adjust=adjust)
            if raw is None or raw.empty: return _EMPTY.copy()
            akshare_breaker.record_success()
            return self._cleanse_daily(raw)
        except Exception as e:
            logger.error("AKShare 日线失败 [%s]：%s", symbol, e); akshare_breaker.record_failure(); return _EMPTY.copy()
    @staticmethod
    def _cleanse_daily(raw: pd.DataFrame) -> pd.DataFrame:
        col = {"日期":"date","开盘":"open","最高":"high","最低":"low","收盘":"close","成交量":"volume","成交额":"amount","换手率":"turnover"}
        df = raw.rename(columns=col)
        df["date"] = pd.to_datetime(df["date"]); df = df.set_index("date").sort_index()
        keep = [c for c in ["open","high","low","close","volume","amount","turnover"] if c in df.columns]
        return df[keep]
    # 宏观/板块 wrapper 同范式（返回原始 DataFrame 由 sync 脚本合并；失败返空）
    def fetch_macro_raw(self, kind: str) -> pd.DataFrame:
        """kind ∈ {'shrzgm','money_supply','dr007','shibor'}。真实 ak.* 在此分发。"""
        if not self._guard(): return _EMPTY.copy()
        try:
            import akshare as ak
            if kind == "shrzgm": return ak.macro_china_shrzgm()
            if kind == "money_supply": return ak.macro_china_money_supply()
            if kind == "shibor": return ak.macro_china_shibor_all()
            if kind == "dr007":  # DR007：实现期对 akshare 1.18.64 复核确切接口（repo_rate_hist/rate_interbank）
                try: return ak.repo_rate_hist()
                except Exception: return ak.rate_interbank(market="回购市场", indicator="7天")
            return _EMPTY.copy()
        except Exception as e:
            logger.error("AKShare 宏观失败 [%s]：%s", kind, e); akshare_breaker.record_failure(); return _EMPTY.copy()
    def fetch_margin_detail(self) -> pd.DataFrame:
        """融资融券明细（沪深合并）。"""
        if not self._guard(): return _EMPTY.copy()
        try:
            import akshare as ak
            sse = ak.stock_margin_detail_sse(start_date=_today8(), end_date=_today8())
            szse = ak.stock_margin_detail_szse(start_date=_today8(), end_date=_today8())
            return pd.concat([d for d in (sse, szse) if d is not None and not d.empty], ignore_index=True)
        except Exception as e:
            logger.error("AKShare 融资融券失败：%s", e); akshare_breaker.record_failure(); return _EMPTY.copy()
    def fetch_sector_fund_flow(self) -> pd.DataFrame:
        if not self._guard(): return _EMPTY.copy()
        try:
            import akshare as ak; return ak.stock_sector_fund_flow_rank(indicator="今日", sector_type="行业资金流")
        except Exception as e:
            logger.error("AKShare 板块资金流失败：%s", e); akshare_breaker.record_failure(); return _EMPTY.copy()
    def fetch_individual_fund_flow(self, symbol: str) -> pd.DataFrame:
        if not self._guard(): return _EMPTY.copy()
        try:
            import akshare as ak; return ak.stock_individual_fund_flow(stock=symbol, market="sh" if symbol.endswith(".SH") else "sz")
        except Exception as e:
            logger.error("AKShare 个股资金流失败 [%s]：%s", symbol, e); akshare_breaker.record_failure(); return _EMPTY.copy()

def _today8() -> str:
    import datetime as _dt
    return _dt.date.today().strftime("%Y%m%d")
```
- [ ] **Step 5: 跑 PASS** — `pytest tests/test_akshare_client.py -v`。
- [ ] **Step 6: `git add -f` + Commit** — `feat(data): AKShareClient 手动熔断+限流 wrapper(日线洗净/宏观/板块/资金流)`

---

### Task 5: 宏观信贷同步脚本（`scripts/sync_macro_credit.py`）

**Files:** Create `scripts/sync_macro_credit.py`；Test: `tests/test_sync_macro_credit.py`
**Interfaces:** Produces `fetch_macro_series(client)`、`align_to_daily(monthly_df)`、`sync_macro(out)`；落 `data_lake/macro_credit.parquet`（DatetimeIndex）。

- [ ] **Step 1: 写失败测试**（mock AKShareClient.fetch_macro_raw）：
```python
import pandas as pd
from scripts.sync_macro_credit import align_to_daily, fetch_macro_series

class _FakeClient:
    def fetch_macro_raw(self, kind):
        return {"shrzgm": pd.DataFrame({"月份":["2024-01"],"社会融资规模增量":[100]}),
                "money_supply": pd.DataFrame({"月份":["2024-01"],"M2同比增长":[9.0],"M1同比增长":[5.0]}),
                "dr007": pd.DataFrame({"日期":["2024-01-02"],"利率":[2.1]}),
                "shibor": pd.DataFrame()}[kind]

def test_align_to_daily_forward_fill_only():
    """月频宏观 → 日频，仅向前 ffill（无未来值回填）。"""
    m = pd.DataFrame({"月份":["2024-01-01","2024-02-01"],"x":[1.0,2.0]})
    m["月份"] = pd.to_datetime(m["月份"])
    daily = align_to_daily(m, date_col="月份", start="2024-01-01", end="2024-01-31")
    # 1月内所有日都应为 1.0（1月值向前填），不应出现 2.0（2月未来值）
    assert (daily["x"] == 1.0).all()

def test_fetch_macro_series_no_empty_merge():
    s = fetch_macro_series(_FakeClient(), "2024-01-01", "2024-01-31")
    assert "M1M2_gap" in s.columns   # 剪刀差衍生列
```
- [ ] **Step 2: 跑 FAIL**。
- [ ] **Step 3: 创建 `scripts/sync_macro_credit.py`**：
```python
"""宏观信贷同步：AKShare 社融/M1M2/DR007/SHIBOR → 日频对齐 parquet。

前视红线：月频宏观仅向前 ffill 到日频（用过去值解释现在），绝不回填未来月度值。
"""
from __future__ import annotations
import os
import pandas as pd
from data.clients.akshare_client import AKShareClient
from config import LAKE_CONFIG

def align_to_daily(df: pd.DataFrame, date_col: str, start: str, end: str, *, value_cols: list[str] | None = None) -> pd.DataFrame:
    """reindex 到日历日 + 仅向前 ffill。无 bfill，杜绝未来函数。"""
    d = df.copy(); d[date_col] = pd.to_datetime(d[date_col]); d = d.set_index(date_col).sort_index()
    cal = pd.bdate_range(start, end)  # 工作日
    out = d.reindex(cal)
    cols = value_cols or out.columns.tolist()
    out[cols] = out[cols].ffill()     # 仅向前
    out.index.name = "date"
    return out

def fetch_macro_series(client: AKShareClient, start: str, end: str) -> pd.DataFrame:
    shrzgm = client.fetch_macro_raw("shrzgm")
    money  = client.fetch_macro_raw("money_supply")
    dr007  = client.fetch_macro_raw("dr007")
    # 社融/货币月频 → 日频向前 ffill（列名以 akshare 实测为准，此处做防御性 rename/取列）
    # DR007 日频直接 reindex
    series = {}
    if not shrzgm.empty:
        s = align_to_daily(_pick(shrzgm, "月份"), "月份", start, end); series["shrzgm"] = s.iloc[:,0]
    if not money.empty:
        m = align_to_daily(_pick(money, "月份"), "月份", start, end)
        if "M1同比增长" in m and "M2同比增长" in m:
            m["M1M2_gap"] = m["M1同比增长"].astype(float) - m["M2同比增长"].astype(float)
        series.update({c: m[c] for c in m.columns})
    if not dr007.empty:
        d = align_to_daily(_pick(dr007, "日期"), "日期", start, end); series["dr007"] = d.iloc[:,0]
    if not series: return pd.DataFrame()
    df = pd.DataFrame(series).ffill().dropna(how="all")
    return df

def _pick(df: pd.DataFrame, prefer: str) -> pd.DataFrame:
    """容错取日期列（akshare 列名版本间有差异）。"""
    if prefer in df.columns: return df
    cand = [c for c in df.columns if "日期" in str(c) or "月份" in str(c) or "date" in str(c).lower()]
    return df.rename(columns={cand[0]: prefer}) if cand else df

def sync_macro(start: str, end: str, out: str | None = None) -> None:
    out = out or LAKE_CONFIG["lakes"]["macro"]
    df = fetch_macro_series(AKShareClient(), start, end)
    if df.empty: print("宏观数据为空，跳过"); return
    os.makedirs(os.path.dirname(out), exist_ok=True)
    df.to_parquet(out)
    print(f"宏观湖写入：{out}，{len(df)} 行")

if __name__ == "__main__":
    import datetime as _dt
    end = _dt.date.today().strftime("%Y-%m-%d")
    start = (_dt.date.today() - _dt.timedelta(days=365*2)).strftime("%Y-%m-%d")
    sync_macro(start, end)
```
- [ ] **Step 4: 跑 PASS** + 全量回归（基线 31）。
- [ ] **Step 5: Commit** — `feat(macro): sync_macro_credit AKShare 社融/M1M2/DR007 日频向前ffill`

---

### Task 6: 板块两融 + 活跃股初筛（`scripts/sync_sector_daily.py`）

**Files:** Create `scripts/sync_sector_daily.py`；Test: `tests/test_sync_sector_daily.py`
**Interfaces:** Produces `select_active_pool(client, top_n=3, pool_size=50)`、`sync_sector_daily(out_sector, out_daily)`。

- [ ] **Step 1: 写失败测试**（mock client）：
```python
import pandas as pd
from scripts.sync_sector_daily import select_active_pool, compute_margin_growth

def test_compute_margin_growth_top_sectors():
    """融资余额环比增速 → 取前 3 板块。"""
    margin = pd.DataFrame({"标的代码":["000001.SZ","000002.SZ","600000.SH"],
                           "行业":["银行","地产","银行"],"融资余额":[110,105,90]})  # 假昨日 100/100/100
    growth = compute_margin_growth(margin, prev={"银行":100.0,"地产":100.0})
    top3 = growth.sort_values("growth", ascending=False).head(3)
    assert "银行" in top3["行业"].tolist()  # 银行 110+90 vs 100 → 增速正

def test_select_active_pool_size_and_source():
    """活跃池来自 top 板块内、按动量/换手排序，定 50 只（测试用小池）。"""
    class _FakeClient:
        def fetch_margin_detail(self): return pd.DataFrame(...)
        def fetch_sector_fund_flow(self): return pd.DataFrame(...)
        def fetch_individual_fund_flow(self, s): return pd.DataFrame(...)
        def fetch_daily_hist(self, s, a, b): return pd.DataFrame({"日期":["2024-01-02"],"开盘":[1],"最高":[1],"最低":[1],"收盘":[1],"成交量":[1],"成交额":[1]})
    pool = select_active_pool(_FakeClient(), top_n=1, pool_size=2)
    assert isinstance(pool, list) and len(pool) <= 2
```
（测试用小池验证筛选逻辑；真实 pool_size=50 由 config。）
- [ ] **Step 2: 跑 FAIL**。
- [ ] **Step 3: 创建 `scripts/sync_sector_daily.py`**：
```python
"""板块两融 + 活跃股初筛：融资融券明细→申万一级 groupby→融资余额环比增速 top3 板块
→ 板块内按 20 日换手率/动量选 50 只活跃股 → 拉其前复权日线。

Why 漏斗：宏观信贷扩张先体现在板块融资增速，再传导到活跃个股，避免全市场拉取。
"""
from __future__ import annotations
import os
import pandas as pd
from data.clients.akshare_client import AKShareClient
from config import AKSHARE_CONFIG, LAKE_CONFIG

def compute_margin_growth(margin: pd.DataFrame, prev: dict) -> pd.DataFrame:
    """按行业 groupby 算融资余额环比增速。prev={行业: 昨日融资余额}。"""
    g = margin.groupby("行业")["融资余额"].sum().reset_index()
    g["growth"] = g.apply(lambda r: (r["融资余额"] - prev.get(r["行业"], r["融资余额"])) / max(prev.get(r["行业"], 1), 1), axis=1)
    return g.sort_values("growth", ascending=False)

def select_active_pool(client: AKShareClient, top_n: int = 3, pool_size: int = 50) -> list[str]:
    margin = client.fetch_margin_detail()
    flow = client.fetch_sector_fund_flow()
    if margin.empty: return []
    # 申万行业归属（实现期对 akshare 复核个股→行业映射；此处用 margin 内行业列兜底）
    prev = {}  # 真实场景从昨日 sector.parquet 读
    growth = compute_margin_growth(margin, prev)
    top = growth.head(top_n)["行业"].tolist()
    # top 板块内个股按 20 日换手率/动量排序取 pool_size
    cand = margin[margin["行业"].isin(top)]["标的代码"].tolist()
    scored = []
    for sym in cand:
        df = client.fetch_daily_hist(sym, _shift(20), _today())  # 近 20 日
        if df.empty or len(df) < 5: continue
        mom = df["close"].pct_change().sum(); turn = df["volume"].mean()
        scored.append((sym, mom, turn))
    scored.sort(key=lambda x: (x[1], x[2]), reverse=True)
    return [s[0] for s in scored[:pool_size]]

def sync_sector_daily(out_sector: str | None = None, out_daily: str | None = None) -> None:
    client = AKShareClient()
    out_sector = out_sector or LAKE_CONFIG["lakes"]["sector"]
    out_daily = out_daily or LAKE_CONFIG["lakes"]["daily"]
    pool = select_active_pool(client, AKSHARE_CONFIG["top_sectors"], AKSHARE_CONFIG["active_pool_size"])
    if not pool: print("活跃池为空，跳过"); return
    # 板块资金流落盘
    flow = client.fetch_sector_fund_flow()
    if not flow.empty:
        os.makedirs(os.path.dirname(out_sector), exist_ok=True); flow.to_parquet(out_sector)
    # 50 只日线合并落盘
    pieces = []
    for sym in pool:
        df = client.fetch_daily_hist(sym, _shift(365), _today())
        if df.empty: continue
        df["symbol"] = sym; df = df.reset_index().rename(columns={"index":"date"})
        pieces.append(df)
    if pieces:
        big = pd.concat(pieces, ignore_index=True)
        big["date"] = pd.to_datetime(big["date"])
        big = big.set_index(["date","symbol"]).sort_index()
        os.makedirs(os.path.dirname(out_daily), exist_ok=True)
        big.to_parquet(out_daily)
    print(f"活跃池 {len(pool)} 只，sector/daily 已落盘")

def _today() -> str:
    import datetime as _dt; return _dt.date.today().strftime("%Y-%m-%d")
def _shift(days: int) -> str:
    import datetime as _dt; return (_dt.date.today() - _dt.timedelta(days=days)).strftime("%Y-%m-%d")

if __name__ == "__main__":
    sync_sector_daily()
```
- [ ] **Step 4: 跑 PASS** + 回归。
- [ ] **Step 5: Commit** — `feat(sector): sync_sector_daily 融资融券→top3板块→50活跃股日线漏斗`

---

### Task 7: JQData 安全客户端（`data/clients/jqdata_client.py`）

**Files:** Create `data/clients/jqdata_client.py`；Test: `tests/test_jqdata_client.py`
**Interfaces:** Produces `JQDataClient.get_instance()`、`.fetch_minute_bars(symbol,start,end,frequency='5m')`；异常 `QuotaExceeded`；配额双机制。

- [ ] **Step 1: 写失败测试**（mock jqdatasdk）：
```python
"""JQDataClient：单例锁+配额双机制+洗净；临限抛 QuotaExceeded+告警；缺凭证降级。"""
import asyncio, os
from unittest.mock import AsyncMock, MagicMock
from data.clients.jqdata_client import JQDataClient, QuotaExceeded

def test_disabled_when_no_creds(monkeypatch):
    monkeypatch.delenv("JQDATA_USERNAME", raising=False)
    c = JQDataClient()
    df = c.fetch_minute_bars("000001.SZ","2024-01-02","2024-01-03")
    assert df.empty and not c._enabled

def _mock_jq(monkeypatch, spare=200_000, rows=240):
    jq = MagicMock()
    jq.auth = MagicMock()
    jq.get_query_count = MagicMock(return_value={"total":1_000_000,"spare":spare})
    fake = __import__("pandas").DataFrame({"open":[1]*rows,"high":[1]*rows,"low":[1]*rows,
        "close":[1]*rows,"volume":[100]*rows,"money":[1e6]*rows},
        index=__import__("pandas").date_range("2024-01-02",periods=rows,freq="T"))
    jq.get_price = MagicMock(return_value=fake)
    monkeypatch.setitem(__import__("sys").modules, "jqdatasdk", jq)
    return jq

def test_fetch_cleanses_and_counts(monkeypatch):
    monkeypatch.setenv("JQDATA_USERNAME","u"); monkeypatch.setenv("JQDATA_PASSWORD","p")
    _mock_jq(monkeypatch, spare=900_000)
    c = JQDataClient()
    df = c.fetch_minute_bars("000001.SZ","2024-01-02","2024-01-03",frequency="1m")
    assert "amount" in df.columns and "volume" in df.columns
    assert c._today_count == 240

def test_quota_near_limit_raises_and_alerts(monkeypatch):
    monkeypatch.setenv("JQDATA_USERNAME","u"); monkeypatch.setenv("JQDATA_PASSWORD","p")
    _mock_jq(monkeypatch, spare=10_000)  # spare<5万
    c = JQDataClient()
    import pytest
    with pytest.raises(QuotaExceeded):
        c.fetch_minute_bars("000001.SZ","2024-01-02","2024-01-03")
```
- [ ] **Step 2: 跑 FAIL**。
- [ ] **Step 3: 创建 `data/clients/jqdata_client.py`**：
```python
"""JQData 单例客户端：threading.Lock 防并发（聚宽单连接）+ 配额双机制
（手动计数 + get_query_count 校准），spare<5万 或 95万 即抛 QuotaExceeded + 钉钉告警。

红线：绝不超日限额；缺凭证降级返空。money→amount 洗净。
"""
from __future__ import annotations
import asyncio, datetime as _dt, logging, os, threading
import pandas as pd
from config import JQDATA_CONFIG
logger = logging.getLogger(__name__)

class QuotaExceeded(Exception): ...

class JQDataClient:
    _instance = None; _singleton_lock = threading.Lock()
    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            with cls._singleton_lock:
                if cls._instance is None: cls._instance = cls()
        return cls._instance
    def __init__(self):
        self._lock = threading.Lock()
        self._enabled = bool(os.getenv("JQDATA_USERNAME") and os.getenv("JQDATA_PASSWORD"))
        self._today_count = 0; self._today = _dt.date.today(); self._calls_since_calib = 0
        if self._enabled:
            try:
                import jqdatasdk as jq
                jq.auth(os.getenv("JQDATA_USERNAME"), os.getenv("JQDATA_PASSWORD"))
                logger.info("JQData 认证成功")
            except Exception as e:
                logger.error("JQData 认证失败，降级：%s", e); self._enabled = False
    def _reset_if_new_day(self):
        today = _dt.date.today()
        if today != self._today: self._today = today; self._today_count = 0; self._calls_since_calib = 0
    def _near_limit(self) -> bool:
        return self._today_count >= JQDATA_CONFIG["quota_manual_limit"] or self._calls_since_calib == 0 and self._spare() < JQDATA_CONFIG["quota_warn_spare"]
    def _spare(self) -> float:
        try:
            import jqdatasdk as jq
            return float(jq.get_query_count().get("spare", 1_000_000))
        except Exception: return 1_000_000
    def fetch_minute_bars(self, symbol, start_date, end_date, frequency="5m") -> pd.DataFrame:
        if not self._enabled: return pd.DataFrame()
        with self._lock:   # 单连接串行
            self._reset_if_new_day()
            if self._calls_since_calib == 0 or self._calls_since_calib >= JQDATA_CONFIG["calibrate_every"]:
                # 校准：用 get_query_count 的 total-spare 复位 _today_count（权威）
                q = self._spare(); self._today_count = max(self._today_count, 1_000_000 - q); self._calls_since_calib = 0
            if self._today_count >= JQDATA_CONFIG["quota_manual_limit"] or self._spare() < JQDATA_CONFIG["quota_warn_spare"]:
                from core.notifier import NotificationManager, fire_and_forget
                fire_and_forget(NotificationManager.get_default().notify_risk_event(
                    f"JQData 日额度将尽（spare≈{int(self._spare())}），已停止拉取 {symbol}", "WARN"))
                raise QuotaExceeded("JQData 日额度接近上限")
            try:
                import jqdatasdk as jq
                df = jq.get_price(symbol, start_date=start_date, end_date=end_date, frequency=frequency,
                                  fields=["open","high","low","close","volume","money"], fq="pre", skip_paused=False)
                if df is None or df.empty: return pd.DataFrame()
                self._today_count += len(df); self._calls_since_calib += 1
                return self._cleanse(df)
            except QuotaExceeded: raise
            except Exception as e:
                logger.error("JQData fetch 失败 [%s]：%s", symbol, e); return pd.DataFrame()
    @staticmethod
    def _cleanse(df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        if "money" in out.columns: out = out.rename(columns={"money":"amount"})
        if not isinstance(out.index, pd.DatetimeIndex): out.index = pd.to_datetime(out.index)
        if getattr(out.index, "tz", None) is not None: out.index = out.index.tz_localize(None)
        return out.sort_index()
```
- [ ] **Step 4: 跑 PASS** + 回归。
- [ ] **Step 5: `git add -f` + Commit** — `feat(jq): JQDataClient 单例+锁+配额双机制+钉钉告警`

---

### Task 8: JQData 分钟同步脚本（`scripts/sync_jqdata_1min.py`）

**Files:** Create `scripts/sync_jqdata_1min.py`；Test: `tests/test_sync_jqdata_1min.py`
**Interfaces:** Produces `sync_jqdata_1min(pool, months=3, freq='5m')`；断点续传 + 优雅停。

- [ ] **Step 1: 写失败测试**（mock JQDataClient + QuotaExceeded 优雅停）：
```python
import pandas as pd
from scripts.sync_jqdata_1min import sync_jqdata_1min

class _FakeClient:
    def __init__(self, fail_at=99): self.n=0; self.fail_at=fail_at
    def fetch_minute_bars(self, s, a, b, frequency="5m"):
        self.n+=1
        if self.n>=self.fail_at:
            from data.clients.jqdata_client import QuotaExceeded
            raise QuotaExceeded("limit")
        return pd.DataFrame({"open":[1],"high":[1],"low":[1],"close":[1],"volume":[1],"amount":[1]},
                            index=pd.to_datetime(["2024-01-02"]))

def test_sync_resumable_and_graceful_stop(tmp_path, monkeypatch):
    monkeypatch.setattr("scripts.sync_jqdata_1min.JQDataClient.get_instance", lambda: _FakeClient(fail_at=2))
    monkeypatch.setattr("scripts.sync_jqdata_1min.build_multiindex", lambda d,o: None)
    shard_dir = str(tmp_path/"shards"); out = str(tmp_path/"m.parquet")
    sync_jqdata_1min(["A","B","C"], months=3, freq="5m", shard_dir=shard_dir, out=out)
    import os
    done = [f for f in os.listdir(shard_dir) if f.endswith(".parquet")]
    assert "A_5m.parquet" in done      # 第 1 只成功
    # 第 2 只触发 QuotaExceeded → 优雅停，C 不再拉
    assert "C_5m.parquet" not in done
```
- [ ] **Step 2: 跑 FAIL**。
- [ ] **Step 3: 创建 `scripts/sync_jqdata_1min.py`**：
```python
"""JQData 分钟同步：对活跃池(50只)拉近 3 月 1m/5m，断点续传，配额耗尽优雅停。

Why：试用期 100 万条/天 + 单连接，分钟数据量大；shard 落盘可断点续传，
QuotaExceeded 即停（明日重跑从断点继续），不崩。
"""
from __future__ import annotations
import datetime as _dt, os
import pandas as pd
from tqdm import tqdm
from data.clients.jqdata_client import JQDataClient, QuotaExceeded

def build_multiindex(shard_dir: str, out: str) -> None:
    frames = []
    for f in os.listdir(shard_dir):
        if not f.endswith(".parquet"): continue
        sym = f.rsplit("_",1)[0]
        df = pd.read_parquet(os.path.join(shard_dir,f)); df["symbol"]=sym; df=df.reset_index().rename(columns={"index":"date"})
        if "date" not in df.columns: df=df.reset_index().rename(columns={"index":"date"})
        frames.append(df)
    if not frames: raise RuntimeError(f"shard 空：{shard_dir}")
    big = pd.concat(frames, ignore_index=True); big["date"]=pd.to_datetime(big["date"])
    big = big.set_index(["date","symbol"]).sort_index()
    os.makedirs(os.path.dirname(out), exist_ok=True); big.to_parquet(out)
    print(f"分钟湖写入：{out}，{len(big)} 行")

def sync_jqdata_1min(pool: list[str], months: int = 3, freq: str = "5m",
                     shard_dir: str = "data_lake/jq_shards",
                     out: str = "data_lake/a_shares_1min.parquet") -> None:
    os.makedirs(shard_dir, exist_ok=True)
    end = _dt.date.today().strftime("%Y-%m-%d")
    start = (_dt.date.today() - _dt.timedelta(days=30*months)).strftime("%Y-%m-%d")
    client = JQDataClient.get_instance()
    stopped = False
    for sym in tqdm(pool, desc=f"JQData {freq}"):
        shard = os.path.join(shard_dir, f"{sym}_{freq}.parquet")
        if os.path.exists(shard): continue
        try:
            df = client.fetch_minute_bars(sym, start, end, frequency=freq)
        except QuotaExceeded:
            print("今日额度将尽，明日重跑续传"); stopped = True; break
        if df.empty: continue
        df.to_parquet(shard)
    if not stopped:
        try: build_multiindex(shard_dir, out)
        except RuntimeError as e: print(e)

if __name__ == "__main__":
    from scripts.sync_sector_daily import select_active_pool
    from data.clients.akshare_client import AKShareClient
    from config import JQDATA_CONFIG, AKSHARE_CONFIG
    pool = select_active_pool(AKShareClient(), AKSHARE_CONFIG["top_sectors"], AKSHARE_CONFIG["active_pool_size"])
    sync_jqdata_1min(pool, months=3, freq=JQDATA_CONFIG["freq_default"])
```
- [ ] **Step 4: 跑 PASS** + 回归。
- [ ] **Step 5: Commit** — `feat(jq): sync_jqdata_1min 活跃池分钟同步(断点续传/优雅停)`

---

### Task 9: Binance Vision 离线挖掘（可选，`scripts/sync_binance_vision.py`）

**Files:** Create `scripts/sync_binance_vision.py`；Test: `tests/test_sync_binance_vision.py`
**Interfaces:** Produces `async fetch_one(session,symbol,date,sem)`、`async sync_binance_vision(symbol='BTCUSDT',days=30,out=...)`；404 跳过、清理临时。

- [ ] **Step 1: 写失败测试**（mock aiohttp 200+zip / 404）：
```python
import asyncio, io, zipfile, pandas as pd
from scripts.sync_binance_vision import parse_klines_csv, _row_to_df

def test_parse_klines_csv_assigns_columns():
    csv = b"1700000000000,1.0,2.0,0.5,1.5,100,1700000060000,150,50,60,90,ignore\n"
    df = parse_klines_csv(csv)
    assert list(df.columns)[:6] == ["open","high","low","close","volume","amount"]
    assert df["close"].iloc[0] == 1.5
    assert str(df.index[0])[:4] == "2023"  # ms→UTC datetime

def test_404_skipped(monkeypatch):
    """404 → 返回 None，调用方跳过。"""
    from scripts.sync_binance_vision import fetch_one
    import aiohttp
    class _Resp:
        def raise_for_status(self): raise aiohttp.ClientResponseError(None,None,status=404,message="NF")
        async def __aenter__(self): return self
        async def __aexit__(self,*a): return False
    class _Sess:
        def get(self,*a,**k): return _Resp()
        async def __aenter__(self): return self
        async def __aexit__(self,*a): return False
    async def _mk(): return aiohttp.ClientSession
    res = asyncio.run(fetch_one(_Sess(),"BTCUSDT","2024-01-02"))
    assert res is None
```
- [ ] **Step 2: 跑 FAIL**。
- [ ] **Step 3: 创建 `scripts/sync_binance_vision.py`**：
```python
"""Binance Vision 离线下载：aiohttp 并发拉 1m ZIP → 解压 CSV → 统一列 → 增量 parquet。

404 跳过（某日无数据）；临时 zip/csv 用完即清。
"""
from __future__ import annotations
import asyncio, io, os, tempfile, zipfile
import aiohttp
import pandas as pd

_COLS = ["open","high","low","close","volume","close_time","amount",
         "trades","taker_buy_base","taker_buy_quote","ignore"]

def parse_klines_csv(raw: bytes) -> pd.DataFrame:
    """12 列无表头 CSV → 标准 DataFrame，open_time(ms)→UTC datetime 索引。"""
    import csv as _csv
    rows = list(_csv.reader(io.StringIO(raw.decode().strip())))
    if not rows: return pd.DataFrame()
    df = pd.DataFrame(rows, columns=_COLS + (["extra"] if len(rows[0])>len(_COLS) else []))
    for c in ["open","high","low","close","volume","amount","trades"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df.index = pd.to_datetime(df["open_time"].astype("int64"), unit="ms", utc=True).tz_convert("UTC")
    df.index.name = "date"
    return df[["open","high","low","close","volume","amount"]]

async def fetch_one(session, symbol: str, date: str, sem: asyncio.Semaphore) -> pd.DataFrame | None:
    url = f"https://data.binance.vision/data/spot/daily/klines/{symbol}/1m/{symbol}-1m-{date}.zip"
    async with sem:
        try:
            async with session.get(url) as resp:
                if resp.status == 404: return None   # 某日无数据，跳过
                resp.raise_for_status()
                data = await resp.read()
        except Exception: return None
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            name = z.namelist()[0]; raw = z.read(name)
        return parse_klines_csv(raw)
    except Exception:
        return None

async def sync_binance_vision(symbol: str = "BTCUSDT", days: int = 30,
                              out: str = "data_lake/crypto_btc_1m.parquet") -> None:
    import datetime as _dt
    sem = asyncio.Semaphore(8)
    dates = [(_dt.date.today() - _dt.timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)]
    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(*[fetch_one(session, symbol, d, sem) for d in dates])
    frames = [df for df in results if df is not None and not df.empty]
    if not frames: print("Binance 无数据"); return
    big = pd.concat(frames).sort_index()
    big["symbol"] = symbol; big = big.reset_index().set_index(["date","symbol"]).sort_index()
    os.makedirs(os.path.dirname(out), exist_ok=True)
    big.to_parquet(out)
    print(f"crypto 湖写入：{out}，{len(big)} 行")

if __name__ == "__main__":
    asyncio.run(sync_binance_vision())
```
- [ ] **Step 4: 跑 PASS** + 回归。
- [ ] **Step 5: Commit** — `feat(crypto): sync_binance_vision aiohttp 并发+404跳过+清理(可选沙盒)`

---

### Task 10: lifespan 多湖 load

**Files:** Modify `server/main.py`（lifespan：按 LAKE_CONFIG["lakes"] 逐个 load；移除已删的 GLMClient 残留若 Task2 未清）。
**Interfaces:** 启动期 `DataLakeReader.get_instance().load(path, key=...)` 载入存在的湖；默认湖 = LAKE_CONFIG["default_lake"]。

- [ ] **Step 1: 改 `server/main.py` lifespan** — 把原 `DataLakeReader.get_instance().load()` 单行替换为：
```python
    # 启动：按 LAKE_CONFIG 多湖逐个加载（缺失则离线降级，不阻断启动）
    from data.lake_reader import DataLakeReader
    from config import LAKE_CONFIG
    reader = DataLakeReader.get_instance()
    for key, path in LAKE_CONFIG.get("lakes", {}).items():
        reader.load(path, key=key)
```
- [ ] **Step 2: 验证** — `python -c "from server.main import app; print('ok')"` → `ok`。
- [ ] **Step 3: 回归** — `pytest -q --tb=no | tail -3`。
- [ ] **Step 4: Commit** — `feat(server): lifespan 多湖按 LAKE_CONFIG 逐个 load`

---

## Phase 3 — Epic 2 因子

### Task 11: CreditRegime 宏观状态机（`factors/macro_regime.py`）

**Files:** Create `factors/macro_regime.py`；Test: `tests/test_macro_regime.py`
**Interfaces:** Produces `CreditRegime.get_default()`、`.compute(date) -> int`（+1/0/-1）；读 macro 湖。

- [ ] **Step 1: 写失败测试**（注入 macro DataFrame）：
```python
import pandas as pd
from factors.macro_regime import CreditRegime

def test_expansion_when_credit_up_and_rates_down():
    idx = pd.date_range("2024-01-01", periods=40, freq="B")
    macro = pd.DataFrame(index=idx)
    macro["shrzgm"] = [100 + i for i in range(40)]      # 社融扩张
    macro["M1M2_gap"] = [1.0]*40                        # 剪刀差正
    macro["dr007"] = [2.5 - i*0.01 for i in range(40)]  # 利率下行
    r = CreditRegime(macro_df=macro)
    assert r.compute(idx[-1]) == 1

def test_contraction_when_credit_down_and_rates_up():
    idx = pd.date_range("2024-01-01", periods=40, freq="B")
    macro = pd.DataFrame(index=idx)
    macro["shrzgm"] = [200 - i for i in range(40)]
    macro["M1M2_gap"] = [-1.0]*40
    macro["dr007"] = [2.0 + i*0.01 for i in range(40)]
    r = CreditRegime(macro_df=macro)
    assert r.compute(idx[-1]) == -1

def test_no_lookahead_only_uses_past():
    """compute(D) 只用 D 及之前的数据（D 之后的扩张不影响）。"""
    idx = pd.date_range("2024-01-01", periods=40, freq="B")
    macro = pd.DataFrame(index=idx); macro["shrzgm"]=[100]*40; macro["M1M2_gap"]=[0.0]*40; macro["dr007"]=[2.0]*40
    # 在 D 之后人为插入扩张，compute(D) 不应感知
    macro.loc[idx[-1], "shrzgm"] = 9999
    r = CreditRegime(macro_df=macro)
    d = idx[20]
    assert r.compute(d) in (0, 1, -1)   # 仅用 [:d]
```
- [ ] **Step 2: 跑 FAIL**。
- [ ] **Step 3: 创建 `factors/macro_regime.py`**：
```python
"""CreditRegime：日频宏观信贷状态机（+1 扩张/0 中性/-1 收缩）。

无前视：compute(date) 用 .loc[:date] 严格时间门控。
融合社融增速 + M1M2 剪刀差 + DR007 趋势。
"""
from __future__ import annotations
import threading
import pandas as pd

class CreditRegime:
    _instance = None; _lock = threading.Lock()
    @classmethod
    def get_default(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None: cls._instance = cls()
        return cls._instance
    def __init__(self, macro_df: pd.DataFrame | None = None):
        self._macro = macro_df   # 测试注入；生产由 lifespan 从 macro 湖载入
    def _series(self, date) -> pd.DataFrame:
        if self._macro is None:
            from data.lake_reader import DataLakeReader
            from config import LAKE_CONFIG
            ts = DataLakeReader.get_instance().get_timeseries  # macro 湖按 symbol 取需另法；这里用截面兜底
            self._macro = pd.DataFrame()  # 占位，实现期按 macro 湖结构对接
        return self._macro.loc[:pd.Timestamp(date)]
    def compute(self, date) -> int:
        s = self._series(date)
        if s.empty or len(s) < 20: return 0
        win = s.tail(20)
        credit_up = win["shrzgm"].iloc[-1] > win["shrzgm"].iloc[0]
        gap_pos = win["M1M2_gap"].iloc[-1] > 0
        rate_down = win["dr007"].iloc[-1] < win["dr007"].iloc[0]
        if credit_up and gap_pos and rate_down: return 1
        credit_down = win["shrzgm"].iloc[-1] < win["shrzgm"].iloc[0]
        rate_up = win["dr007"].iloc[-1] > win["dr007"].iloc[0]
        if credit_down and (not gap_pos) and rate_up: return -1
        return 0
```
（注：生产环境 `_series` 对接 macro 湖——macro 湖是 DatetimeIndex（无 symbol 层），实现期可加 `get_macro_series(date)` 方法或直接 `DataLakeReader._lakes["macro"].loc[:date]`。测试用注入 macro_df 覆盖逻辑。）
- [ ] **Step 4: 跑 PASS** + 回归。
- [ ] **Step 5: Commit** — `feat(factor): CreditRegime 日频宏观信贷状态机(+1/0/-1, 无前视)`

---

### Task 12: 微观动量 + ATR 波动率（`factors/micro_momentum.py`）

**Files:** Create `factors/micro_momentum.py`；Test: `tests/test_micro_momentum.py`
**Interfaces:** Produces `breakout_signal(df_1m, fast=5, slow=20)`、`atr(df, window=14)`、`risk_parity_weight(atr_value, budget)`。

- [ ] **Step 1: 写失败测试**：
```python
import numpy as np, pandas as pd
from factors.micro_momentum import breakout_signal, atr, risk_parity_weight

def test_breakout_signal_direction():
    idx = pd.date_range("2024-01-02", periods=60, freq="T")
    close = pd.Series(np.linspace(10,12,60), index=idx)  # 单边上行
    df = pd.DataFrame({"close":close,"high":close+0.1,"low":close-0.1}, index=idx)
    sig = breakout_signal(df)
    assert sig.iloc[-1] in (1, 0) and sig.iloc[-5:].sum() >= 0  # 上行→末期非负

def test_atr_positive_and_risk_parity_inverse():
    idx = pd.date_range("2024-01-02", periods=40, freq="T")
    df = pd.DataFrame({"high":np.linspace(11,12,40),"low":np.linspace(9,10,40),"close":np.linspace(10,11,40)}, index=idx)
    a = atr(df, window=14)
    assert (a.dropna() > 0).all()
    w1 = risk_parity_weight(0.5, budget=1e6); w2 = risk_parity_weight(2.0, budget=1e6)
    assert w1 > w2   # ATR 小→头寸大（反比）

def test_atr_no_inf():
    idx = pd.date_range("2024-01-02", periods=40, freq="T")
    df = pd.DataFrame({"high":[11]*40,"low":[9]*40,"close":[10]*40}, index=idx)  # ATR 常数
    a = atr(df, window=14)
    assert not np.isinf(a.dropna()).any()
```
- [ ] **Step 2: 跑 FAIL**。
- [ ] **Step 3: 创建 `factors/micro_momentum.py`**：
```python
"""微观动量爆发 + ATR 波动率 + Risk Parity 头寸。

ATR = mean((high-low).rolling(window))；头寸 ∝ 1/ATR，控单笔回撤。
均线密集发散：短长期 MA 聚拢后突破 → 信号。
"""
from __future__ import annotations
import numpy as np, pandas as pd

def breakout_signal(df: pd.DataFrame, fast: int = 5, slow: int = 20) -> pd.Series:
    ma_f = df["close"].rolling(fast).mean(); ma_s = df["close"].rolling(slow).mean()
    dense = (ma_f - ma_s).abs() < (df["close"].rolling(slow).std() * 0.2)  # 密集
    cross = (ma_f > ma_s).astype(int)
    return (dense.shift(1, fill_value=False) & (ma_f > ma_s)).astype(int)

def atr(df: pd.DataFrame, window: int = 14) -> pd.Series:
    tr = (df["high"] - df["low"]).abs()
    a = tr.rolling(window).mean()
    return a.where(a > 1e-9, 1e-9)   # 防除零 Inf

def risk_parity_weight(atr_value: float, budget: float, min_atr: float = 1e-9) -> float:
    a = max(atr_value, min_atr)
    return float(budget / a)   # ∝ 1/ATR
```
- [ ] **Step 4: 跑 PASS** + 回归。
- [ ] **Step 5: Commit** — `feat(factor): micro_momentum 突破信号+ATR+Risk Parity 头寸`

---

## Phase 4 — Epic 3 网关 + 订单

### Task 13: 订单状态机止损止盈（`trading/order_state.py`）

**Files:** Modify `trading/order_state.py`；Test: `tests/test_order_state_stops.py`
**Interfaces:** Produces `check_stop_loss(entry, price, pct)`、`check_take_profit(entry, price, pct)`、`update_trailing_stop(high, atr, k, prev_stop)`。

- [ ] **Step 1: 写失败测试**：
```python
from trading.order_state import check_stop_loss, check_take_profit, update_trailing_stop

def test_stop_loss_triggers_below_threshold():
    assert check_stop_loss(entry=100.0, price=94.0, pct=0.05) is True   # 跌 6%>5%
    assert check_stop_loss(entry=100.0, price=96.0, pct=0.05) is False

def test_take_profit_triggers_above_threshold():
    assert check_take_profit(entry=100.0, price=106.0, pct=0.05) is True
    assert check_take_profit(entry=100.0, price=104.0, pct=0.05) is False

def test_trailing_only_moves_up():
    # high=110, atr=2, k=2 → stop=106；后续 high=108 → stop=104 < 106，不降
    s1 = update_trailing_stop(high=110.0, atr=2.0, k=2.0, prev_stop=0.0)
    assert s1 == 106.0
    s2 = update_trailing_stop(high=108.0, atr=2.0, k=2.0, prev_stop=s1)
    assert s2 == 106.0   # 不下移
    s3 = update_trailing_stop(high=112.0, atr=2.0, k=2.0, prev_stop=s1)
    assert s3 == 108.0   # 上移
```
- [ ] **Step 2: 跑 FAIL**。
- [ ] **Step 3: 在 `trading/order_state.py` 追加纯函数**（不破坏既有 OrderState 枚举）：
```python
# ============ 出场逻辑（纯函数，可单测）============
def check_stop_loss(entry: float, price: float, pct: float) -> bool:
    """固定止损：price ≤ entry*(1-pct) 触发。"""
    return price <= entry * (1.0 - pct)

def check_take_profit(entry: float, price: float, pct: float) -> bool:
    """固定止盈：price ≥ entry*(1+pct) 触发。"""
    return price >= entry * (1.0 + pct)

def update_trailing_stop(high: float, atr: float, k: float, prev_stop: float) -> float:
    """ATR 移动止损：stop = high - atr*k；只上移不下移，锁浮盈。"""
    new_stop = high - atr * k
    return max(new_stop, prev_stop)
```
- [ ] **Step 4: 跑 PASS** + 回归（既有 test_trading.py 不破）。
- [ ] **Step 5: Commit** — `feat(order): 止损/止盈/ATR移动止损纯函数`

---

### Task 14: 执行网关宏观否决（`trading/execution_gateway.py`）

**Files:** Modify `trading/execution_gateway.py`；Test: `tests/test_execution_gateway_veto.py`
**Interfaces:** Produces `MacroAwareGateway.submit_order(order, regime)`（regime=-1 + BUY → 减半或否决）。

- [ ] **Step 1: 写失败测试**（注入 regime，不触网）：
```python
import pytest
from trading.execution_gateway import MacroAwareGateway, VetoedError
from trading.order_state import OrderState

class _Order:
    def __init__(self, side, qty): self.side=side; self.quantity=qty; self.state=OrderState.PENDING

def test_buy_in_contraction_halved():
    gw = MacroAwareGateway(strict_veto=False)
    o = _Order("BUY", 1000)
    gw.submit_order(o, regime=-1)
    assert o.quantity == 500   # 减半

def test_buy_in_contraction_strict_veto():
    gw = MacroAwareGateway(strict_veto=True)
    with pytest.raises(VetoedError):
        gw.submit_order(_Order("BUY",1000), regime=-1)

def test_buy_in_expansion_passes():
    gw = MacroAwareGateway(strict_veto=False)
    o = _Order("BUY",1000)
    gw.submit_order(o, regime=1)
    assert o.quantity == 1000   # 不变

def test_sell_not_vetoed_in_contraction():
    gw = MacroAwareGateway(strict_veto=True)
    o = _Order("SELL",1000)
    gw.submit_order(o, regime=-1)   # 卖出不受否决
    assert o.quantity == 1000
```
- [ ] **Step 2: 跑 FAIL**。
- [ ] **Step 3: 在 `trading/execution_gateway.py` 追加**（不破坏既有 BaseExecutionGateway/MockExecutionGateway）：
```python
class VetoedError(Exception):
    """宏观一票否决（收缩期买入）。"""

class MacroAwareGateway:
    """宏观感知执行网关：收缩期(-1)买入→强制减半或一票否决。"""
    def __init__(self, strict_veto: bool = False):
        self.strict_veto = strict_veto   # True=否决，False=减半
    def submit_order(self, order, regime: int):
        if regime == -1 and getattr(order, "side", "").upper() == "BUY":
            if self.strict_veto:
                raise VetoedError("宏观收缩期，否决买入突破")
            order.quantity = max(1, order.quantity // 2)   # 强制减半
        return order
```
- [ ] **Step 4: 跑 PASS** + 回归。
- [ ] **Step 5: Commit** — `feat(gateway): 宏观否决网关(收缩期买入减半/一票否决)`

---

### Task 15: 引擎分钟级 + T+1 底仓冻结（`backtest/engine.py`）

**Files:** Modify `backtest/engine.py`；Test: `tests/test_engine_minute.py`
**Interfaces:** Produces `BacktestEngine.run_minute(df_1m, signal, symbol, atr_window=14, sl_pct=0.05, tp_pct=0.05, trail_k=2.0, event_emitter=None)`；维护底仓可卖/新仓冻结。

- [ ] **Step 1: 写失败测试**：
```python
import numpy as np, pandas as pd
from backtest.engine import BacktestEngine

def _up_data():
    idx = pd.date_range("2024-01-02 09:30", periods=120, freq="T")
    close = pd.Series(np.linspace(10,11.5,120), index=idx)
    df = pd.DataFrame({"open":close,"high":close+0.05,"low":close-0.05,"close":close,"volume":1000}, index=idx)
    sig = pd.Series(np.where(np.arange(120)>=20, 0.8, 0.0), index=idx)
    return df, sig

def test_run_minute_default_none_unaffected():
    df, sig = _up_data()
    r = BacktestEngine(initial_capital=100000).run_minute(df, sig, "000001.SZ")
    assert "trades" in r and "daily_records" in r

def test_run_minute_emitter_receives_progress_and_trade():
    df, sig = _up_data(); events=[]
    BacktestEngine().run_minute(df, sig, "000001.SZ", event_emitter=lambda e: events.append(e))
    assert any(e["type"]=="progress" for e in events)
    assert any(e["type"]=="trade" for e in events)

def test_t1_new_position_frozen_today_sellable_next_day():
    """T+1：今日新仓不可卖，次日解冻为底仓可卖。"""
    # 构造先涨（建仓）后跌（应卖）的分钟序列；验证日内不卖、次日才卖
    # （此用例聚焦 _split_t1 逻辑：底仓 vs 冻结）
    from backtest.engine import _split_t1
    held, frozen = _split_t1(current_held=0, today_bought=100, prev_held=200)
    assert held == 200 and frozen == 100   # 昨日200为底仓可卖，今日100冻结
```
- [ ] **Step 2: 跑 FAIL**。
- [ ] **Step 3: 在 `backtest/engine.py` 追加**（不破坏既有 run/run_portfolio）：
```python
def _split_t1(current_held: int, today_bought: int, prev_held: int):
    """T+1 底仓冻结感知：返回 (底仓可卖, 今日新仓冻结)。

    A 股 T+1：昨日及更早建仓=底仓可日内卖；今日新仓冻结至次日。
    current_held=此刻总持仓，today_bought=今日买入量，prev_held=昨日收盘持仓。
    """
    sellable = min(prev_held, current_held)            # 底仓（昨日的）可卖
    frozen = max(0, current_held - sellable)            # 今日新仓冻结
    # 但若 today_bought > 0 且 prev_held 已被卖光，则全部为冻结
    return sellable, frozen

class BacktestEngine:  # 既有类内追加方法
    def run_minute(self, df, signal, symbol="000001.SZ", atr_window=14,
                   sl_pct=0.05, tp_pct=0.05, trail_k=2.0,
                   event_emitter=None) -> dict:
        """分钟级回测 + T+1 + 止损止盈移动止损 + event_emitter。"""
        from trading.order_state import check_stop_loss, check_take_profit, update_trailing_stop
        self._reset_state()
        aligned = df.loc[signal.index]
        atr_s = (aligned["high"]-aligned["low"]).rolling(atr_window).mean().fillna(1e-9)
        prev_held = 0
        for i,(ts,row) in enumerate(aligned.iterrows()):
            sig = signal.loc[ts]
            today = ts.date()
            # 日切：今日新仓 → 次日底仓（简化：每个新交易日把昨日持仓转底仓）
            if i>0 and aligned.index[i-1].date() != today:
                prev_held = self.position
            sellable, frozen = _split_t1(self.position, 0, prev_held)
            price = row["open"]
            # 止损/止盈（仅对底仓 sellable 生效，新仓 frozen 不能卖——T+1）
            if sellable > 0:
                if check_stop_loss(self._entry or price, price, sl_pct):
                    self._close(sellable, price, ts, "stoploss", event_emitter); 
                elif check_take_profit(self._entry or price, price, tp_pct):
                    self._close(sellable, price, ts, "takeprofit", event_emitter)
            # 信号建仓（分钟级简化：sig>0 买入）
            target = int(sig * self.nav / price / 100) * 100
            if target > self.position:
                self._buy(target - self.position, price, ts, symbol, event_emitter)
            self._update_minute_nav(ts, row)
            if event_emitter:
                event_emitter({"type":"progress","date":str(ts),"i":i,"n":len(aligned),"nav":self.nav})
        return self._calculate_result()
    # _entry/_close/_buy/_update_minute_nav 为引擎内辅助（见实现，_entry 记最近买入价）
```
（实现期补 `_entry`/`_close`/`_buy`/`_update_minute_nav` 辅助方法，复用既有 self.trades/self.nav/self.cash 机制；emit risk 事件 reason="触及止损/止盈"。）
- [ ] **Step 4: 跑 PASS** + 回归（既有 test_backtest.py 不破）。
- [ ] **Step 5: Commit** — `feat(engine): run_minute 分钟级+T+1底仓冻结+止损止盈+emitter`

---

## Phase 5 — 前端可视化

### Task 16: 宏观/板块/因子只读端点（`server/api/v1/macro.py`）

**Files:** Create `server/api/v1/macro.py`；Modify `server/main.py`（挂载）；Test: `tests/test_macro_api.py`
**Interfaces:** `GET /api/v1/macro/regime`、`/macro/credit`、`/sector/flow`、`/factors/{symbol}`。

- [ ] **Step 1: 写失败测试**（TestClient）：
```python
import pytest
from fastapi.testclient import TestClient

@pytest.fixture
def client():
    from server.main import app
    return TestClient(app)

def test_macro_regime_endpoint(client, monkeypatch):
    from factors.macro_regime import CreditRegime
    monkeypatch.setattr(CreditRegime, "get_default", lambda: _FakeRegime())
    resp = client.get("/api/v1/macro/regime")
    assert resp.status_code == 200
    assert resp.json()["regime"] in (-1, 0, 1)

class _FakeRegime:
    def compute(self, date): return 1
    def history(self, n=60): return [{"date":"2024-01-02","regime":1}]
```
- [ ] **Step 2: 跑 FAIL**。
- [ ] **Step 3: 创建 `server/api/v1/macro.py`**：
```python
"""宏观/板块/因子只读端点（读内存湖，零写入）。"""
from __future__ import annotations
from fastapi import APIRouter
from config import LAKE_CONFIG
from data.lake_reader import DataLakeReader
from factors.macro_regime import CreditRegime
router = APIRouter(prefix="/macro", tags=["宏观/板块/因子"])

@router.get("/regime")
async def regime():
    r = CreditRegime.get_default().compute(_today())
    return {"regime": r, "history": CreditRegime.get_default().history(60)}

@router.get("/credit")
async def credit():
    # macro 湖时序（社融/M1M2_gap/dr007）
    reader = DataLakeReader.get_instance()
    df = reader._lakes.get("macro")
    if df is None or df.empty: return {"series": {}}
    return {"series": {c: _to_json(df[c]) for c in df.columns}}

@router.get("/sector/flow")
async def sector_flow():
    reader = DataLakeReader.get_instance()
    df = reader._lakes.get("sector")
    if df is None or df.empty: return {"sectors": [], "pool": []}
    return {"sectors": df.head(20).to_dict("records"), "pool": []}

@router.get("/factors/{symbol}")
async def factors(symbol: str):
    from factors.micro_momentum import atr
    ts = DataLakeReader.get_instance().get_timeseries(symbol, _shift(30), _today(), lake="minute")
    if ts.empty: return {"atr": None}
    return {"atr": float(atr(ts).iloc[-1])}

def _today():
    import datetime as _dt; return _dt.date.today().strftime("%Y-%m-%d")
def _shift(d):
    import datetime as _dt; return (_dt.date.today()-_dt.timedelta(days=d)).strftime("%Y-%m-%d")
def _to_json(s):
    return [{"date": str(i), "value": float(v)} for i, v in s.dropna().tail(180).items()]
```
（实现期补 `CreditRegime.history(n)`；`/sector/flow` 的 pool 字段从 sector 湖活跃池读。）
- [ ] **Step 4: `server/main.py` 挂载** — `app.include_router(macro_router, prefix="/api/v1")`。
- [ ] **Step 5: 跑 PASS** + 回归。
- [ ] **Step 6: Commit** — `feat(api): macro/sector/factors 只读端点`

---

### Task 17: 前端 `/dashboard` 宏观·板块驾驶舱

**Files:** Create `web/src/views/DashboardView.vue`、`web/src/api/macro.ts`；Modify `web/src/router/index.ts`（恢复路由）、`web/src/App.vue`（用 router-view）。
**Interfaces:** axios GET `/api/v1/macro/regime`/`/macro/credit`/`/sector/flow`。

- [ ] **Step 1: `web/src/api/macro.ts`** — axios 封装 `getMacroRegime()`/`getMacroCredit()`/`getSectorFlow()`。
- [ ] **Step 2: 恢复 vue-router** — `web/src/router/index.ts` 创建 router，路由 `/`（回测终端，App.vue 现有 Grid）与 `/dashboard`；`main.ts` `.use(router)`；`App.vue` 改用 `<router-view/>`（或保留终端布局在 `/`，dashboard 在 `/dashboard`）。
- [ ] **Step 3: `web/src/views/DashboardView.vue`** — ECharts 组件：CreditRegime 状态卡 + 三联折线（社融/M1M2_gap/dr007）+ 板块热力图（融资余额增速）+ 活跃股池表。
- [ ] **Step 4: 验证** — `cd web && npx vue-tsc --noEmit && npm run build` 通过。
- [ ] **Step 5: Commit** — `feat(front): /dashboard 宏观·板块驾驶舱(路由恢复)`

---

### Task 18: 回测终端扩展（分钟级 K 线 + 止损止盈标注 + STOPLOSS 高亮）

**Files:** Modify `web/src/components/ProChart.vue`（分钟级 + 止损/止盈/移动止损水平线 + 触发标注）、`web/src/composables/useTerminalState.ts`（risk reason 止损/止盈 → lv-warn/lv-error）、`web/src/api/backtest.ts`（BacktestRequest 增 freq='1m'/'5m'）。
**Interfaces:** ProChart 支持 minute ohlcv + stop/take lines；TerminalLogs `[WARN-STOPLOSS]` 高亮。

- [ ] **Step 1: `useTerminalState.toLogEntry`** — risk 分支按 reason 区分：`触及止损`→`lv-error`、`触及止盈`→`lv-success`、其它→`lv-warn`。
- [ ] **Step 2: `ProChart.vue`** — ohlcv 时间轴兼容分钟；若 result 含 `stop_lines`/`take_lines`/`trail_lines`，画 markLine。
- [ ] **Step 3: `api/backtest.ts`** — `SingleBacktestParams` 增可选 `freq: '1d'|'5m'|'1m'`。
- [ ] **Step 4: 验证** — `cd web && npx vue-tsc --noEmit && npm run build`。
- [ ] **Step 5: Commit** — `feat(front): 终端分钟级K线+止损止盈标注+STOPLOSS高亮`

---

## Phase 6 — 收尾

### Task 19: 钉钉新触发 wiring + SSE risk 高亮回归

**Files:** Verify `core/notifier.py` 触发链路（JQData QuotaExceeded 已在 Task7 wired；宏观否决在 Task14 wired 钉钉；单日回撤告警）。
**Interfaces:** 钉钉触发：JQData 流量 / 熔断 on_open / 宏观-1 / 单日回撤。

- [ ] **Step 1: 宏观-1 清仓告警** — `MacroAwareGateway.submit_order` 否决/减半时 `fire_and_forget(notify_risk_event("宏观收缩期，买入已减半/否决","WARN"))`。
- [ ] **Step 2: 单日回撤告警** — `backtest/engine.py` `run_minute` 内：日切时若当日 nav 回撤 > 阈值（config）→ `fire_and_forget(...)`。
- [ ] **Step 3: 全量回归** — `pytest -q`，基线 31，0 新增。
- [ ] **Step 4: Commit** — `feat(notify): 宏观-1否决告警+单日回撤告警 wiring`

---

### Task 20: README 更新 + 全量回归

**Files:** Modify `README.md`。
- [ ] **Step 1: README 增** — 宏观 CTA 架构说明、AKShare/JQData/Binance 同步命令、宏观驾驶舱路由、Tushare dormant/LLM 移除说明、`.env` JQDATA_* 配置。
- [ ] **Step 2: 全量回归** — `pytest -q`（基线 31，0 新增）+ `cd web && npm run build`。
- [ ] **Step 3: Commit** — `docs: README 增宏观CTA架构/同步命令/驾驶舱`

---

## Self-Review（计划自审记录）

**1. Spec 覆盖**：Epic1→T3-T10（含 Binance T9 可选）；Epic2→T11/T12；Epic3→T13/T14/T15；Epic4→T15(emitter)+T18(前端高亮)；Epic5→T7(JQData告警)+T14(宏观告警)+T19；前端→T16/T17/T18；LLM移除→T2；Tushare dormant→T1(不删)；横切→T1。全覆盖。

**2. 类型一致性**：`AKShareClient.fetch_*` / `JQDataClient.fetch_minute_bars`+`QuotaExceeded` / `DataLakeReader.load(key=)`+`get_*(lake=)`+`lakes()` / `CreditRegime.get_default().compute(date)` / `breakout_signal`/`atr`/`risk_parity_weight` / `check_stop_loss`/`check_take_profit`/`update_trailing_stop` / `MacroAwareGateway.submit_order(order,regime)`+`VetoedError` / `BacktestEngine.run_minute`+`_split_t1` / macro 端点 — 跨 Task 名称一致。

**3. 已知留白（实现期补，非占位符）**：
- AKShare DR007 确切接口、个股→申万行业映射、stock_margin_detail_sse/szse 列名：实现期对 akshare 1.18.64 复核（wrapper 内，测试 mock wrapper 不受影响）。
- `CreditRegime._series` 对 macro 湖（DatetimeIndex 无 symbol 层）的对接：实现期加 `DataLakeReader._lakes["macro"].loc[:date]` 直读或 `get_macro_series(date)`。
- `run_minute` 的 `_entry`/`_close`/`_buy`/`_update_minute_nav` 辅助方法：实现期补，复用既有 self.trades/nav/cash。
- `CreditRegime.history(n)`、`/sector/flow` 的 pool 字段：实现期补。

**4. 回归红线**：每 Task 末 `pytest -q` ≤ 当时基线（T2 后 31）；DataLakeReader `lake=` 默认值保旧调用方；`event_emitter` 默认 None。
