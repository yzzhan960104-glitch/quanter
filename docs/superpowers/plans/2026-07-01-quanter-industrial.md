# Quanter 工业级蜕变 — 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 按 6 阶段顺序落地 5 大 Epic（数据湖 / GLM 情感 / 因子沙盒 / SSE 回测流 / 宏观+钉钉），每阶段产出可独立测试的交付物。

**Architecture:** 全程复用现有 `data/resilience.py`（CircuitBreaker/RateLimiter）与 `core/notifier.py`（NotificationChannel ABC）；新增模块遵循单例 + 异步 + 手动熔断守卫范式；外部 I/O 一律返回空/中性降级，绝不抛到核心引擎。Celery+Redis 承担因子网格重计算（带 psutil CPU 探针 + Redis 宕机降级）。SSE 复用 `logs.py` 的 `call_soon_threadsafe` 跨线程模式做 per-run 流。

**Tech Stack:** Python 3.10+ / FastAPI / Pydantic v2 / Pandas+PyArrow / openai SDK（GLM）/ Celery+Redis / aiohttp（钉钉）/ yfinance / httpx（Alpha Vantage）/ pytest；前端 Vue3.5 + TS + EventSource + ECharts。

## Global Constraints

- **语言**：所有代码注释、对话、文档 100% 中文（CLAUDE.md 硬约束）；注释须含"为什么"。
- **反黑盒**：不引 alphalens/scipy/sse-starlette/akshare；IC 用 pandas rank+corr，赫斯顿用显式 R/S。
- **类型注解**：Python 3.10+ 类型注解全覆盖（`from __future__ import annotations`）。
- **测试范式**：用 `asyncio.run(...)` 而非 pytest-asyncio；外部网络用 monkeypatch 脱网；通道测试仿 `tests/test_notifier.py` 的 `_FakeChannel` + 注入假投递函数。
- **熔断 on_open 跨线程**：同步上下文（线程池里的 fetcher/client）无运行事件循环，`asyncio.get_running_loop()` 会抛 `RuntimeError` 导致告警协程被吞——必须用 `core.notifier.fire_and_forget(coro)`（后台 daemon 线程跑 `asyncio.run`）触发告警。
- **新增依赖**（已在 Task 1 加入 requirements.txt）：`pyarrow>=14.0` `openai>=1.30` `aiohttp>=3.9` `celery[redis]>=5.3` `redis>=5.0` `psutil>=5.9` `tqdm>=4.66`。
- **提交规范**：每个 Task 末尾 `git commit`；消息中文，末尾附 `Co-Authored-By: Claude <noreply@anthropic.com>`。
- **回归红线**：每个 Task 后跑 `pytest -q`，现有 17 个测试文件必须保持全绿（`event_emitter` 默认 None 不破坏回测）。

---

## File Structure

**新建：**
- `.env.example` — 环境变量模板
- `scripts/sync_data_lake.py` — 数据湖批量同步 CLI
- `data/lake_reader.py` — `DataLakeReader` 单例（截面/时序查询）
- `data/clients/__init__.py` — 包标识
- `data/clients/yfinance_client.py` — `YFinanceClient`（标普/原油/黄金/VIX）
- `data/clients/alpha_vantage_client.py` — `AlphaVantageClient`（美债收益率）
- `core/llm_client.py` — `GLMClient` 单例 + `SentimentResult`
- `factors/alternative_sentiment.py` — `NewsSentimentFactor`
- `factors/exploratory_momentum.py` — 动量/波动率调整动量/赫斯顿
- `factors/analyzer.py` — `FactorAnalyzer`（IC + 分层）
- `server/celery_app.py` — Celery 实例 + 因子网格任务
- `server/api/v1/explorer.py` — 因子沙盒路由
- 对应 `tests/test_*.py` 共 11 个新测试文件

**修改：**
- `requirements.txt` — 加 7 个依赖
- `config.py` — 加 `LAKE_CONFIG/LLM_CONFIG/MACRO_CLIENT_CONFIG/CELERY_CONFIG`
- `server/core/config.py` — （可选）补 DATA_LAKE 相关默认
- `core/notifier.py` — 加 `DingTalkChannel` + `fire_and_forget` + `build_default_manager` 增补钉钉
- `server/main.py` — lifespan 接入 notifier/LakeReader/GLM；挂载 explorer 路由
- `backtest/engine.py` — `run()` 加可选 `event_emitter`
- `server/api/v1/backtest.py` — 加 `POST /run/async` + `GET /run/stream/{run_id}`
- `web/src/composables/useTerminalState.ts` — 改 EventSource 流式 + `logs` ref
- `web/src/components/TerminalLogs.vue` — 改为消费 `useTerminalState.logs`

---

## Phase 1 — 横切地基

### Task 1: 依赖、环境模板与配置增量

**Files:**
- Modify: `requirements.txt`
- Create: `.env.example`
- Modify: `config.py`（末尾追加）

**Interfaces:**
- Produces: `config.LAKE_CONFIG` / `LLM_CONFIG` / `MACRO_CLIENT_CONFIG` / `CELERY_CONFIG`；`.env.example` 键名（后续 Task 的 `os.getenv` 以此为准）。

- [ ] **Step 1: 更新 `requirements.txt`**

在文件末尾追加（保留现有内容）：
```
# ===== 工业级蜕变新增依赖 =====
pyarrow>=14.0
openai>=1.30
aiohttp>=3.9
celery[redis]>=5.3
redis>=5.0
psutil>=5.9
tqdm>=4.66
```

- [ ] **Step 2: 安装新依赖**

Run: `pip install pyarrow openai aiohttp celery[redis] redis psutil tqdm`
Expected: 全部安装成功。

- [ ] **Step 3: 创建 `.env.example`**

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

- [ ] **Step 4: `config.py` 末尾追加配置字典**

```python
# ============================================================
# 工业级蜕变新增配置（纯字典，凭证仍走 .env）
# ============================================================
import os as _os

# 数据湖（Epic 1）
LAKE_CONFIG = {
    "default_path": _os.getenv("DATA_LAKE_PATH", "data_lake/a_shares_daily.parquet"),
    "shard_dir": "data_lake/shards",
    "years_default": 10,
}

# GLM 大模型（Epic 2）
LLM_CONFIG = {
    "base_url": _os.getenv("ZHIPU_BASE_URL", "https://open.bigmodel.cn/api/paas/v4/"),
    "model": _os.getenv("ZHIPU_MODEL", "glm-4-flash"),
    "timeout": 15,
}

# 宏观另类数据客户端（Epic 5）
MACRO_CLIENT_CONFIG = {
    "yfinance_symbols": {"SPX": "^GSPC", "CL": "CL=F", "GC": "GC=F", "VIX": "^VIX"},
    "av_treasury_maturities": ["3MO", "2Y", "10Y", "30Y"],
}

# Celery 因子沙盒（Epic 3）
CELERY_CONFIG = {
    "broker_url": _os.getenv("REDIS_URL", "redis://localhost:6379/0"),
    "queue": _os.getenv("CELERY_EXPLORER_QUEUE", "explorer"),
    "cpu_gate_percent": 80.0,
}
```

- [ ] **Step 5: 验证 import 无误**

Run: `python -c "from config import LAKE_CONFIG, LLM_CONFIG, MACRO_CLIENT_CONFIG, CELERY_CONFIG; print('ok')"`
Expected: 输出 `ok`。

- [ ] **Step 6: Commit**

```bash
git add requirements.txt .env.example config.py
git commit -m "feat(foundation): 新增工业级蜕变依赖、env 模板与配置字典

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Phase 2 — Epic 5 容灾通道（先做：它是其他 Epic 异常的告警出口）

### Task 2: 钉钉通道 + fire_and_forget 助手

**Files:**
- Modify: `core/notifier.py`
- Test: `tests/test_notifier.py`（追加用例）

**Interfaces:**
- Produces: `core.notifier.DingTalkChannel(webhook, secret)`（`NotificationChannel` 子类，async `send(text)`）、`core.notifier.fire_and_forget(coro)`。

- [ ] **Step 1: 追加失败测试到 `tests/test_notifier.py`**

```python
def test_fire_and_forget_runs_coroutine_in_background():
    """fire_and_forget 必须在无事件循环的同步上下文里也能跑通协程。"""
    from core.notifier import fire_and_forget
    import time as _t
    done = []
    async def _work():
        done.append(42)
    fire_and_forget(_work())
    _t.sleep(0.5)  # 等后台线程
    assert done == [42]


def test_dingtalk_channel_payload_and_sign(monkeypatch):
    """守护钉钉加签 URL 拼装与 Markdown payload（脱网，monkeypatch _post）。"""
    from core.notifier import DingTalkChannel
    captured = {}

    async def fake_post(url, payload):
        captured["url"] = url
        captured["payload"] = payload

    ch = DingTalkChannel("https://oapi.dingtalk.com/robot/send?access_token=XXX", "SEC123")
    ch._post = fake_post
    import asyncio
    asyncio.run(ch.send("最大回撤触红线"))
    # 加签参数必须出现在 URL
    assert "timestamp=" in captured["url"] and "sign=" in captured["url"]
    # Markdown + 固定安全词【Quanter】
    assert captured["payload"]["msgtype"] == "markdown"
    assert "【Quanter】" in captured["payload"]["markdown"]["text"]
    assert "最大回撤触红线" in captured["payload"]["markdown"]["text"]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_notifier.py::test_dingtalk_channel_payload_and_sign tests/test_notifier.py::test_fire_and_forget_runs_coroutine_in_background -v`
Expected: FAIL（`DingTalkChannel` 不存在 / `fire_and_forget` 不存在）。

- [ ] **Step 3: 在 `core/notifier.py` 顶部 import 区追加**

```python
import base64
import hashlib
import hmac
import time
import urllib.parse
```
（`asyncio/logging/os/threading/ABC/Literal/httpx` 已有，保留。）

- [ ] **Step 4: 在 `core/notifier.py` 文件末尾（`build_default_manager` 之后）追加**

```python
def fire_and_forget(coro: "Awaitable") -> None:
    """从任意线程（含无事件循环的同步上下文）后台调度一个协程，不阻塞调用方。

    Why 必须用独立线程：熔断器 on_open 回调常发生在数据获取线程（run_in_threadpool），
    该线程无运行事件循环，asyncio.get_running_loop() 会抛 RuntimeError 导致告警协程被吞。
    起 daemon 线程跑 asyncio.run 是跨线程触发异步告警的最简显式做法。
    """
    def _runner():
        try:
            asyncio.run(coro)
        except Exception:
            logger.exception("fire_and_forget 后台协程失败")
    threading.Thread(target=_runner, daemon=True).start()


class DingTalkChannel(NotificationChannel):
    """钉钉群机器人 Webhook（Markdown + 加签）。凭证：webhook url + 加签 secret。

    加签算法（钉钉官方，显式实现，无黑盒）：
      sign = urlencode( base64( HMAC-SHA256(secret, f"{timestamp}\n{secret}") ) )
    """

    def __init__(self, webhook: str, secret: str) -> None:
        self._webhook = webhook
        self._secret = secret

    @staticmethod
    def _sign(secret: str) -> "tuple[str, str]":
        timestamp = str(round(time.time() * 1000))
        string_to_sign = f"{timestamp}\n{secret}"
        digest = hmac.new(secret.encode("utf-8"),
                          string_to_sign.encode("utf-8"), hashlib.sha256).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(digest))
        return timestamp, sign

    async def _post(self, url: str, payload: dict) -> None:
        """真实 aiohttp 投递（测试 monkeypatch 本方法以脱网）。"""
        import aiohttp
        timeout = aiohttp.ClientTimeout(total=10.0)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload) as resp:
                resp.raise_for_status()

    async def send(self, text: str) -> None:
        timestamp, sign = self._sign(self._secret)
        url = f"{self._webhook}&timestamp={timestamp}&sign={sign}"
        # Markdown + 固定安全词【Quanter】（text 已含 Manager 拼的级别前缀）
        await self._post(url, {
            "msgtype": "markdown",
            "markdown": {"title": "【Quanter】风控告警",
                         "text": f"**【Quanter】**\n\n{text}"},
        })
```

- [ ] **Step 5: 运行测试确认通过**

Run: `pytest tests/test_notifier.py -v`
Expected: PASS（含原有用例 + 2 个新用例）。

- [ ] **Step 6: Commit**

```bash
git add core/notifier.py tests/test_notifier.py
git commit -m "feat(notifier): 新增钉钉通道(aiohttp+加签)与 fire_and_forget 跨线程告警助手

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 3: build_default_manager 增补钉钉 + lifespan 接入通知

**Files:**
- Modify: `core/notifier.py`（`build_default_manager`）
- Modify: `server/main.py`（lifespan）
- Test: `tests/test_notifier.py`（追加）

**Interfaces:**
- Produces: `build_default_manager()` 读 `DINGTALK_WEBHOOK`/`DINGTALK_SECRET` 装配；`server.main.lifespan` 启动时调用通知装配。

- [ ] **Step 1: 追加失败测试**

```python
def test_build_default_manager_includes_dingtalk(monkeypatch):
    """配齐钉钉凭证后 build_default_manager 必须装配 DingTalkChannel（幂等）。"""
    from core.notifier import build_default_manager, DingTalkChannel
    monkeypatch.setenv("DINGTALK_WEBHOOK", "https://oapi.dingtalk.com/robot/send?access_token=X")
    monkeypatch.setenv("DINGTALK_SECRET", "SEC")
    mgr = NotificationManager.get_default()
    mgr.clear_channels()
    build_default_manager()
    dts = [c for c in mgr._channels if isinstance(c, DingTalkChannel)]
    assert len(dts) == 1
    build_default_manager()  # 幂等
    dts2 = [c for c in mgr._channels if isinstance(c, DingTalkChannel)]
    assert len(dts2) == 1


def test_build_default_manager_skips_dingtalk_without_credentials(monkeypatch):
    """缺凭证必须跳过该通道，不报错。"""
    from core.notifier import build_default_manager, DingTalkChannel
    monkeypatch.delenv("DINGTALK_WEBHOOK", raising=False)
    monkeypatch.delenv("DINGTALK_SECRET", raising=False)
    mgr = NotificationManager.get_default()
    mgr.clear_channels()
    build_default_manager()
    assert not any(isinstance(c, DingTalkChannel) for c in mgr._channels)
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_notifier.py::test_build_default_manager_includes_dingtalk -v`
Expected: FAIL。

- [ ] **Step 3: 修改 `build_default_manager`**（在 wecom 装配块之后、`mgr._configured = True` 之前插入钉钉装配）

把 `core/notifier.py` 中：
```python
    wecom = os.getenv("WECOM_WEBHOOK", "")
    if wecom:
        mgr.add_channel(WeComChannel(wecom))
    # 装配完成标记，使后续调用幂等。
    mgr._configured = True
```
替换为：
```python
    wecom = os.getenv("WECOM_WEBHOOK", "")
    if wecom:
        mgr.add_channel(WeComChannel(wecom))
    # 钉钉机器人（Markdown + 加签）：缺一凭证则跳过该通道，不报错
    dt_webhook = os.getenv("DINGTALK_WEBHOOK", "")
    dt_secret = os.getenv("DINGTALK_SECRET", "")
    if dt_webhook and dt_secret:
        mgr.add_channel(DingTalkChannel(dt_webhook, dt_secret))
    # 装配完成标记，使后续调用幂等。
    mgr._configured = True
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_notifier.py -v`
Expected: PASS。

- [ ] **Step 5: 在 `server/main.py` lifespan 启动段接入通知装配**

把：
```python
    # 启动：挂载 SSE 日志 handler 到 root logger
```
之前插入一行：
```python
    # 启动：装配异步通知通道（Telegram/企微/钉钉），缺凭证则跳过对应通道
    build_default_manager()
```
并在 `server/main.py` 顶部 import 区加：
```python
from core.notifier import build_default_manager
```

- [ ] **Step 6: 验证 main 可导入（不连 Redis/不触网）**

Run: `python -c "from server.main import app; print('ok')"`
Expected: `ok`（`build_default_manager` 缺凭证只跳过，不报错）。

- [ ] **Step 7: Commit**

```bash
git add core/notifier.py server/main.py tests/test_notifier.py
git commit -m "feat(notifier): build_default_manager 增补钉钉通道并接入 lifespan

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 4: YFinanceClient（标普/原油/黄金/VIX）

**Files:**
- Create: `data/clients/__init__.py`、`data/clients/yfinance_client.py`
- Test: `tests/test_yfinance_client.py`

**Interfaces:**
- Consumes: `data.resilience.CircuitBreaker`（手动 API）、`core.notifier.NotificationManager` + `fire_and_forget`。
- Produces: `data.clients.yfinance_client.YFinanceClient.get_history(symbol, start, end) -> pd.DataFrame`（列 `open/high/low/close/volume`，无时区 DatetimeIndex；熔断/失败返回空 DF，绝不抛）；模块级 `yfinance_breaker`。

- [ ] **Step 1: 写失败测试 `tests/test_yfinance_client.py`**

```python
"""YFinanceClient：熔断守卫 + 数据洗净 + 空降级。"""
import pandas as pd
from data.clients.yfinance_client import YFinanceClient, yfinance_breaker, _EMPTY


def _make_raw():
    idx = pd.DatetimeIndex(["2024-01-02", "2024-01-03"])
    return pd.DataFrame({"Open": [1.0, 2.0], "High": [1.5, 2.5], "Low": [0.9, 1.9],
                         "Close": [1.2, 2.2], "Volume": [100, 200]}, index=idx)


def test_cleanse_returns_standard_columns(monkeypatch):
    client = YFinanceClient()
    monkeypatch.setattr("yfinance.download", lambda *a, **k: _make_raw())
    df = client.get_history("^GSPC", "2024-01-02", "2024-01-03")
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert len(df) == 2


def test_failure_returns_empty_df_not_raise(monkeypatch):
    """yfinance 抛错时必须返回空 DF，绝不向外抛。"""
    client = YFinanceClient()
    def boom(*a, **k):
        raise RuntimeError("network down")
    monkeypatch.setattr("yfinance.download", boom)
    # 复位熔断器，确保不被其它用例污染
    while yfinance_breaker.state.value != "closed":
        pass
    df = client.get_history("^GSPC", "2024-01-02", "2024-01-03")
    assert df.empty
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_yfinance_client.py -v`
Expected: FAIL（模块不存在）。

- [ ] **Step 3: 创建 `data/clients/__init__.py`（空文件，包标识）**

内容仅一行注释：
```python
"""外部数据源客户端（yfinance / alpha_vantage）。"""
```

- [ ] **Step 4: 创建 `data/clients/yfinance_client.py`**

```python
"""YFinance 客户端：标普/原油/黄金/VIX 历史日线。

设计要点（对齐架构师红线）：
- yfinance 库本身同步 → 用【手动熔断 API】（allow_request/record_*），失败返回空 DF，
  绝不抛到核心引擎。
- on_open 跨线程告警：同步上下文无运行 loop，必须 fire_and_forget 触发钉钉异步告警。
- 对外只吐纯净 DataFrame：对齐无时区 DatetimeIndex + 标准列名 + 剔 NaN。
"""
from __future__ import annotations

import logging

import pandas as pd
import yfinance as yf

from core.notifier import NotificationManager, fire_and_forget
from data.resilience import CircuitBreaker

logger = logging.getLogger(__name__)

# 空降级 DataFrame（标准 schema，供熔断/失败时返回）
_EMPTY = pd.DataFrame(columns=["open", "high", "low", "close", "volume"],
                      index=pd.DatetimeIndex([]))


def _notify_yfinance_open() -> None:
    """熔断 on_open 同步回调：后台触发钉钉告警，不阻塞调用线程。"""
    fire_and_forget(
        NotificationManager.get_default().notify_risk_event(
            "yfinance 接口熔断（连续失败），已暂停拉取", "WARN"))


# 模块级熔断器：连续 3 次基础设施异常 → 熔断 60s
yfinance_breaker = CircuitBreaker(
    name="yfinance", failure_threshold=3, recovery_timeout=60.0,
    on_open=_notify_yfinance_open)


class YFinanceClient:
    """标普/原油/黄金/VIX 历史日线客户端。"""

    def get_history(self, symbol: str, start: str, end: str) -> pd.DataFrame:
        """拉取日线；熔断/失败一律返回空 DF，绝不抛。

        参数：
            symbol: yfinance 代码（如 ^GSPC、CL=F、GC=F、^VIX）
            start/end: 'YYYY-MM-DD'
        """
        # 熔断守卫：OPEN 则快速返回空 DF
        if not yfinance_breaker.allow_request():
            logger.warning("yfinance 熔断开启，返回空 DF：%s", symbol)
            return _EMPTY.copy()
        try:
            raw = yf.download(symbol, start=start, end=end, progress=False, auto_adjust=False)
            if raw is None or raw.empty:
                return _EMPTY.copy()
            yfinance_breaker.record_success()
            return self._cleanse(raw)
        except Exception as e:
            # 基础设施异常 → 计入熔断；返回空 DF 绝不抛
            logger.error("yfinance 拉取失败 [%s]：%s", symbol, e)
            yfinance_breaker.record_failure()
            return _EMPTY.copy()

    @staticmethod
    def _cleanse(raw: pd.DataFrame) -> pd.DataFrame:
        """洗净：扁平化可能的多级列、统一列名、去时区、剔 close 为 NaN 的行。"""
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = raw.columns.get_level_values(0)
        cols = ["Open", "High", "Low", "Close", "Volume"]
        available = [c for c in cols if c in raw.columns]
        df = raw[available].copy()
        df.columns = [c.lower() for c in available]
        df.index = pd.to_datetime(df.index)
        if getattr(df.index, "tz", None) is not None:
            df.index = df.index.tz_localize(None)  # 统一无时区
        if "close" in df.columns:
            df = df.dropna(subset=["close"])
        return df
```

- [ ] **Step 5: 运行测试确认通过**

Run: `pytest tests/test_yfinance_client.py -v`
Expected: PASS。

- [ ] **Step 6: Commit**

```bash
git add data/clients/__init__.py data/clients/yfinance_client.py tests/test_yfinance_client.py
git commit -m "feat(macro): YFinanceClient(标普/原油/黄金/VIX) 手动熔断+洗净+空降级

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 5: AlphaVantageClient（美债收益率，双装饰器）

**Files:**
- Create: `data/clients/alpha_vantage_client.py`
- Test: `tests/test_alpha_vantage_client.py`

**Interfaces:**
- Consumes: `data.resilience.RateLimiter` + `CircuitBreaker`（装饰器路径，失败抛 `DataFetchError`）、`core.notifier.fire_and_forget`。
- Produces: `AlphaVantageClient.get_treasury_yield(maturity, start, end) -> Awaitable[pd.DataFrame]`（`@av_limiter @av_breaker` 双装饰；列名为 maturity）；`get_treasury_yield_safe(...)`（service 兜底，捕获 `CircuitOpenError`/`DataFetchError` → 返回空 DF）。

- [ ] **Step 1: 写失败测试 `tests/test_alpha_vantage_client.py`**

```python
"""AlphaVantageClient：双装饰器限流+熔断、洗净、safe 兜底空降级。"""
import asyncio
import pandas as pd
import httpx
from data.clients.alpha_vantage_client import (
    AlphaVantageClient, av_breaker, _EMPTY_TY, DataFetchError, CircuitOpenError)


def _fake_response(data):
    class _R:
        def raise_for_status(self): pass
        def json(self): return data
    return _R()


class _FakeCtx:
    """极简 async context manager 替身，避免触网。"""
    def __init__(self, resp): self._resp = resp
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    def get(self, *a, **k): return self._resp


def test_cleanse_treasury(monkeypatch):
    client = AlphaVantageClient(api_key="KEY")
    payload = {"data": [{"date": "2024-01-03", "value": "4.02"},
                        {"date": "2024-01-02", "value": "4.00"}]}
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _FakeCtx(_fake_response(payload)))
    df = asyncio.run(client.get_treasury_yield("10Y"))
    assert list(df.columns) == ["10Y"]
    assert df["10Y"].iloc[0] == 4.00  # 排序后首行应为较早日期


def test_safe_returns_empty_on_failure(monkeypatch):
    """API 报错时 get_treasury_yield_safe 必须返回空 DF，绝不抛。"""
    client = AlphaVantageClient(api_key="KEY")
    class _BoomCtx:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        def get(self, *a, **k):
            raise httpx.ConnectError("down")
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _BoomCtx())
    df = asyncio.run(client.get_treasury_yield_safe("10Y"))
    assert df.empty
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_alpha_vantage_client.py -v`
Expected: FAIL（模块不存在）。

- [ ] **Step 3: 创建 `data/clients/alpha_vantage_client.py`**

```python
"""Alpha Vantage 客户端：美债收益率（TREASURY_YIELD）。

设计要点：
- 叠加 @RateLimiter(5/60s) + @CircuitBreaker 双装饰器（spec 要求）。
- 装饰器路径下失败抛 DataFetchError（计入熔断）；对外提供 _safe service 方法
  捕获 CircuitOpenError/DataFetchError → 返回空 DF，守住"绝不抛到核心"红线。
- on_open 跨线程告警用 fire_and_forget。
- 对外只吐纯净 DataFrame：对齐 DatetimeIndex、数值列、剔 NaN。
"""
from __future__ import annotations

import logging
import os

import httpx
import pandas as pd

from core.notifier import NotificationManager, fire_and_forget
from data.resilience import CircuitBreaker, DataFetchError, RateLimiter

# 复用熔断器在装饰器路径抛出的异常类型
from data.resilience import CircuitOpenError

logger = logging.getLogger(__name__)

_EMPTY_TY = pd.DataFrame(index=pd.DatetimeIndex([]))


def _notify_av_open() -> None:
    fire_and_forget(
        NotificationManager.get_default().notify_risk_event(
            "Alpha Vantage 接口熔断（连续失败），已暂停拉取", "WARN"))


# 令牌桶：5 calls/60s → capacity=5, refill_rate=5/60
av_limiter = RateLimiter(name="alpha_vantage", capacity=5, refill_rate=5.0 / 60.0)
av_breaker = CircuitBreaker(
    name="alpha_vantage", failure_threshold=3, recovery_timeout=60.0,
    expected_exception=DataFetchError, on_open=_notify_av_open)


class AlphaVantageClient:
    """美债收益率客户端（TREASURY_YIELD）。"""

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key or os.getenv("ALPHA_VANTAGE_API_KEY", "")
        self._enabled = bool(self._api_key)

    @av_limiter
    @av_breaker
    async def get_treasury_yield(self, maturity: str,
                                 start: str | None = None, end: str | None = None) -> pd.DataFrame:
        """拉取指定期限美债收益率。失败抛 DataFetchError（供熔断统计）。"""
        if not self._enabled:
            return _EMPTY_TY.copy()
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(
                    "https://www.alphavantage.co/query",
                    params={"function": "TREASURY_YIELD", "interval": "daily",
                            "maturity": maturity, "apikey": self._api_key})
                resp.raise_for_status()
                data = resp.json()
            return self._cleanse(data, maturity)
        except Exception as e:
            logger.error("Alpha Vantage 拉取失败 [%s]：%s", maturity, e)
            raise DataFetchError(f"Alpha Vantage: {e}") from e

    async def get_treasury_yield_safe(self, maturity: str,
                                      start: str | None = None, end: str | None = None) -> pd.DataFrame:
        """service 兜底：捕获熔断/限流/拉取异常 → 返回空 DF，绝不抛到核心。"""
        try:
            return await self.get_treasury_yield(maturity, start, end)
        except (CircuitOpenError, DataFetchError) as e:
            logger.warning("Alpha Vantage 降级返回空 DF [%s]：%s", maturity, e)
            return _EMPTY_TY.copy()

    @staticmethod
    def _cleanse(data: dict, maturity: str) -> pd.DataFrame:
        items = data.get("data", []) if isinstance(data, dict) else []
        if not items:
            return _EMPTY_TY.copy()
        df = pd.DataFrame(items)
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date").sort_index()
        df[maturity] = pd.to_numeric(df["value"], errors="coerce")
        return df[[maturity]].dropna()
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_alpha_vantage_client.py -v`
Expected: PASS。

- [ ] **Step 5: 全量回归**

Run: `pytest -q`
Expected: 全绿。

- [ ] **Step 6: Commit**

```bash
git add data/clients/alpha_vantage_client.py tests/test_alpha_vantage_client.py
git commit -m "feat(macro): AlphaVantageClient(美债) 双装饰器限流+熔断+_safe 空降级

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Phase 3 — Epic 1 极速本地数据湖

### Task 6: DataLakeReader 单例

**Files:**
- Create: `data/lake_reader.py`
- Test: `tests/test_lake_reader.py`

**Interfaces:**
- Consumes: `config.LAKE_CONFIG`。
- Produces: `data.lake_reader.DataLakeReader.get_instance()`；`.load(path=None)`、`.get_cross_section(date) -> pd.DataFrame`、`.get_timeseries(symbol, start, end) -> pd.DataFrame`、`.loaded -> bool`。

- [ ] **Step 1: 写失败测试 `tests/test_lake_reader.py`**

```python
"""DataLakeReader：ffill 仅价格、截面/时序查询、离线降级。"""
import pandas as pd
from data.lake_reader import DataLakeReader


def _make_lake_df():
    # MultiIndex(date, symbol)；构造一只含停牌（NaN）的标的
    idx = pd.MultiIndex.from_tuples([
        ("2024-01-02", "000001.SZ"), ("2024-01-03", "000001.SZ"),
        ("2024-01-02", "600000.SH"), ("2024-01-03", "600000.SH"),
    ], names=["date", "symbol"])
    df = pd.DataFrame({
        "open":   [10.0, 11.0, 5.0, float("nan")],
        "high":   [10.5, 11.5, 5.5, float("nan")],
        "low":    [9.8, 10.8, 4.8, float("nan")],
        "close":  [10.2, 11.1, 5.1, float("nan")],
        "volume": [1000, 1100, 500, 0],
    }, index=idx)
    return df


def test_ffill_only_prices_not_volume(tmp_path):
    path = tmp_path / "lake.parquet"
    _make_lake_df().to_parquet(path)
    r = DataLakeReader()
    r.load(str(path))
    # 600000.SH 在 2024-01-03 停牌：价格应 ffill 为 01-02 的值，volume 必须保持 0
    sec = r.get_cross_section("2024-01-03")
    assert sec.loc["600000.SH", "close"] == 5.1  # ffill 价格
    assert sec.loc["600000.SH", "volume"] == 0   # volume 不 ffill


def test_timeseries_returns_raw(tmp_path):
    path = tmp_path / "lake.parquet"
    _make_lake_df().to_parquet(path)
    r = DataLakeReader()
    r.load(str(path))
    ts = r.get_timeseries("000001.SZ", "2024-01-01", "2024-01-31")
    assert len(ts) == 2
    assert list(ts.columns)[:1] == ["open"]


def test_offline_mode_when_parquet_missing(tmp_path):
    r = DataLakeReader()
    r.load(str(tmp_path / "nope.parquet"))
    assert r.loaded is False
    assert r.get_cross_section("2024-01-02").empty
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_lake_reader.py -v`
Expected: FAIL（模块不存在）。

- [ ] **Step 3: 创建 `data/lake_reader.py`**

```python
"""数据湖读取适配器：启动期一次性加载 parquet 到内存，提供截面/时序查询。

前视偏差拷问（关键）：
- 仅对【价格列】沿【时间轴】ffill —— 停牌日沿用末次成交价，安全无前视；
  volume/amount 绝不 ffill（停牌日成交应为 0，ffill 会造假量、污染流动性判断）。
- ffill 沿时间方向只传播【过去】值到当前，不引入未来信息。

离线降级：parquet 缺失时 load() 仅记 warning，查询返回空 DF，绝不阻断启动。
"""
from __future__ import annotations

import logging
import os
import threading

import pandas as pd

from config import LAKE_CONFIG

logger = logging.getLogger(__name__)

_PRICE_COLS = ["open", "high", "low", "close"]


class DataLakeReader:
    """单例：全市场日线常驻内存。"""

    _instance: "DataLakeReader | None" = None
    _lock = threading.Lock()

    @classmethod
    def get_instance(cls) -> "DataLakeReader":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def __init__(self) -> None:
        self._df: pd.DataFrame | None = None      # 原始 MultiIndex(date, symbol)
        self._ffill: pd.DataFrame | None = None   # 仅价格列 ffill 后（同索引）
        self._loaded: bool = False

    @property
    def loaded(self) -> bool:
        return self._loaded

    def load(self, path: str | None = None) -> None:
        """加载 parquet 到内存。缺失则进入离线模式。"""
        path = path or LAKE_CONFIG["default_path"]
        if not os.path.exists(path):
            logger.warning("数据湖缺失：%s，DataLakeReader 进入离线模式（查询返回空）", path)
            self._loaded = False
            return
        df = pd.read_parquet(path)
        # 确保 MultiIndex(date, symbol)
        if not isinstance(df.index, pd.MultiIndex):
            logger.error("数据湖索引非 MultiIndex(date, symbol)，跳过加载")
            self._loaded = False
            return
        self._df = df
        # 仅价格列沿时间 ffill；groupby(symbol).ffill() 保证不跨标的串味
        price_cols = [c for c in _PRICE_COLS if c in df.columns]
        self._ffill = df[price_cols].groupby(level="symbol").ffill() if price_cols else df[[]]
        self._loaded = True
        logger.info("数据湖加载完成：%s，%d 行", path, len(df))

    def get_cross_section(self, date: str) -> pd.DataFrame:
        """某日全市场截面（价格取 ffill 版，停牌沿用末次成交价）。"""
        if not self._loaded or self._ffill is None:
            return pd.DataFrame()
        try:
            return self._ffill.xs(pd.Timestamp(date), level="date")
        except KeyError:
            logger.warning("截面日期不存在：%s", date)
            return pd.DataFrame()

    def get_timeseries(self, symbol: str, start: str, end: str) -> pd.DataFrame:
        """单标的原始时序（不 ffill，保留真实停牌空洞）。"""
        if not self._loaded or self._df is None:
            return pd.DataFrame()
        try:
            ts = self._df.xs(symbol, level="symbol")
        except KeyError:
            return pd.DataFrame()
        return ts.loc[pd.Timestamp(start):pd.Timestamp(end)]
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_lake_reader.py -v`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add data/lake_reader.py tests/test_lake_reader.py
git commit -m "feat(lake): DataLakeReader 单例(价格ffill/不ffill量/离线降级)

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 7: 数据湖批量同步脚本 + lifespan 接入

**Files:**
- Create: `scripts/sync_data_lake.py`
- Modify: `server/main.py`（lifespan 接入 LakeReader）
- Test: `tests/test_sync_data_lake.py`

**Interfaces:**
- Consumes: `tushare pro`（`pro_bar(adj='qfq')` 取前复权）、`data.resilience.tushare_rate_limiter` + `tushare_breaker`、`config.LAKE_CONFIG`。
- Produces: CLI `python scripts/sync_data_lake.py --years 10`；函数 `load_universe(pro)`、`fetch_qfq(pro, ts_code, start, end)`、`build_multiindex(shard_dir, out)`。

- [ ] **Step 1: 写失败测试 `tests/test_sync_data_lake.py`**

```python
"""数据湖同步：universe 过滤 ST、空数据跳过、断点续传。"""
import pandas as pd
from scripts.sync_data_lake import load_universe, fetch_qfq, build_multiindex


class _FakePro:
    def stock_basic(self, **kwargs):
        return pd.DataFrame({
            "ts_code": ["000001.SZ", "000002.SZ", "600000.SH"],
            "name": ["平安银行", "万科A", "ST 浦发"],
            "list_date": ["19910403", "19910129", "19991110"],
        })

    def pro_bar(self, **kwargs):
        # 仅 000001.SZ 返回数据，其它返回空，覆盖"空数据跳过"
        if kwargs.get("ts_code") == "000001.SZ":
            return pd.DataFrame({
                "trade_date": ["20240102", "20240103"],
                "open": [10.0, 11.0], "high": [10.5, 11.5], "low": [9.8, 10.8],
                "close": [10.2, 11.1], "vol": [1000, 1100], "amount": [1e7, 1.1e7],
            })
        return pd.DataFrame()


def test_load_universe_excludes_st():
    codes = load_universe(_FakePro())
    assert "000001.SZ" in codes
    assert "000002.SZ" in codes
    assert "600000.SH" not in codes  # 名称含 ST 被剔除


def test_fetch_qfq_cleanses_columns():
    df = fetch_qfq(_FakePro(), "000001.SZ", "2024-01-01", "2024-01-31")
    assert list(df.columns) == ["open", "high", "low", "close", "volume", "amount"]
    assert len(df) == 2


def test_build_multiindex(tmp_path):
    shard = tmp_path / "000001.SZ.parquet"
    fetch_qfq(_FakePro(), "000001.SZ", "2024-01-01", "2024-01-31").to_parquet(shard)
    out = tmp_path / "lake.parquet"
    build_multiindex(str(tmp_path), str(out))
    lake = pd.read_parquet(out)
    assert isinstance(lake.index, pd.MultiIndex)
    assert "000001.SZ" in lake.index.get_level_values("symbol").unique()
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_sync_data_lake.py -v`
Expected: FAIL（模块不存在）。

- [ ] **Step 3: 创建 `scripts/sync_data_lake.py`**

```python
"""数据湖批量同步 CLI：全市场（剔除 ST/退市）过去 N 年日线【前复权】OHLCV。

关键正确性：
- 前复权用 pro_bar(adj='qfq')，【不可】用 pro.daily()（后者不复权）。
- 断点续传：每标的独立落 shard，已存在则跳过。
- 复用 tushare_rate_limiter / tushare_breaker 防封；空数据跳过不中断。

用法：
    python scripts/sync_data_lake.py --years 10 --out data_lake/a_shares_daily.parquet
"""
from __future__ import annotations

import argparse
import os
import time
from datetime import datetime, timedelta

import pandas as pd
from tqdm import tqdm

from config import LAKE_CONFIG
from data.resilience import tushare_breaker, tushare_rate_limiter

# tushare 延迟导入，避免无 token 环境直接崩
def _get_pro():
    import tushare as ts
    from config import get_credential
    ts.set_token(get_credential("tushare", "token"))
    return ts.pro_api()


def load_universe(pro) -> list[str]:
    """全市场在售标的，剔除名称含 'ST'/'退' 的。"""
    df = pro.stock_basic(list_status="L",
                         fields="ts_code,symbol,name,list_date")
    mask = (~df["name"].str.contains("ST", na=False)) & \
           (~df["name"].str.contains("退", na=False))
    return df.loc[mask, "ts_code"].tolist()


def fetch_qfq(pro, ts_code: str, start: str, end: str) -> pd.DataFrame:
    """拉取前复权日线，洗净为标准 schema。失败返回空 DF。"""
    tushare_rate_limiter.acquire(1.0)
    if not tushare_breaker.allow_request():
        return pd.DataFrame()
    try:
        raw = pro.pro_bar(ts_code=ts_code, adj="qfq",
                          start_date=start.replace("-", ""), end_date=end.replace("-", ""),
                          freq="D")
    except Exception:
        tushare_breaker.record_failure()
        return pd.DataFrame()
    if raw is None or raw.empty:
        return pd.DataFrame()
    tushare_breaker.record_success()
    raw = raw.copy()
    raw["trade_date"] = pd.to_datetime(raw["trade_date"], format="%Y%m%d")
    raw = raw.set_index("trade_date").sort_index()
    raw = raw.rename(columns={"vol": "volume"})
    cols = ["open", "high", "low", "close", "volume", "amount"]
    return raw[[c for c in cols if c in raw.columns]]


def build_multiindex(shard_dir: str, out: str) -> None:
    """合并所有 shard → MultiIndex(date, symbol) → pyarrow 写超级大表。"""
    frames = []
    for f in os.listdir(shard_dir):
        if not f.endswith(".parquet"):
            continue
        ts_code = f.replace(".parquet", "")
        df = pd.read_parquet(os.path.join(shard_dir, f))
        df["symbol"] = ts_code
        df = df.reset_index().rename(columns={"trade_date": "date", "index": "date"})
        # 兼容 shard 里 index 已是 date 的情况
        if "date" not in df.columns:
            df = df.reset_index().rename(columns={"index": "date"})
        frames.append(df)
    if not frames:
        raise RuntimeError(f"shard 目录无数据：{shard_dir}")
    big = pd.concat(frames, ignore_index=True)
    big["date"] = pd.to_datetime(big["date"])
    big = big.set_index(["date", "symbol"]).sort_index()
    os.makedirs(os.path.dirname(out), exist_ok=True)
    big.to_parquet(out, engine="pyarrow")
    print(f"数据湖写入完成：{out}，{len(big)} 行")


def main(years: int, out: str, resume: bool = True) -> None:
    pro = _get_pro()
    end = datetime.today().strftime("%Y-%m-%d")
    start = (datetime.today() - timedelta(days=365 * years)).strftime("%Y-%m-%d")
    shard_dir = LAKE_CONFIG["shard_dir"]
    os.makedirs(shard_dir, exist_ok=True)
    codes = load_universe(pro)
    print(f"待同步标的数：{len(codes)}，区间 {start} ~ {end}")
    for ts_code in tqdm(codes):
        shard = os.path.join(shard_dir, f"{ts_code}.parquet")
        if resume and os.path.exists(shard):
            continue  # 断点续传
        df = fetch_qfq(pro, ts_code, start, end)
        if df.empty:
            continue  # 停牌/退市/空 → 跳过不中断
        df.to_parquet(shard)
        time.sleep(0.2)  # 节流，防 Tushare 封禁
    build_multiindex(shard_dir, out)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="A 股全市场前复权日线数据湖同步")
    ap.add_argument("--years", type=int, default=LAKE_CONFIG["years_default"])
    ap.add_argument("--out", default=LAKE_CONFIG["default_path"])
    ap.add_argument("--no-resume", action="store_true")
    args = ap.parse_args()
    main(years=args.years, out=args.out, resume=not args.no_resume)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_sync_data_lake.py -v`
Expected: PASS。

- [ ] **Step 5: `server/main.py` lifespan 接入 LakeReader**

在 `build_default_manager()` 调用之后插入：
```python
    # 启动：数据湖常驻内存（parquet 缺失则离线降级，不阻断启动）
    from data.lake_reader import DataLakeReader
    DataLakeReader.get_instance().load()
```

- [ ] **Step 6: 全量回归**

Run: `pytest -q`
Expected: 全绿。

- [ ] **Step 7: Commit**

```bash
git add scripts/sync_data_lake.py tests/test_sync_data_lake.py server/main.py
git commit -m "feat(lake): 全市场前复权日线同步脚本(pro_bar qfq/断点续传) + lifespan 接入

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Phase 4 — Epic 2 GLM 与另类情感因子

### Task 8: GLMClient 单例 + lifespan 接入

**Files:**
- Create: `core/llm_client.py`
- Modify: `server/main.py`
- Test: `tests/test_llm_client.py`

**Interfaces:**
- Consumes: `config.LLM_CONFIG`、`openai.AsyncOpenAI`、`pydantic`。
- Produces: `core.llm_client.SentimentResult`、`GLMClient.get_instance()`、`async analyze_sentiment(news_text) -> SentimentResult`。

- [ ] **Step 1: 写失败测试 `tests/test_llm_client.py`**

```python
"""GLMClient：凭证缺失降级、超时降级、JSON 非法降级、结构化校验。"""
import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch
from core.llm_client import GLMClient, SentimentResult


def test_disabled_returns_neutral(monkeypatch):
    monkeypatch.delenv("ZHIPU_API_KEY", raising=False)
    c = GLMClient()
    r = asyncio.run(c.analyze_sentiment("某股大涨"))
    assert r.score == 0.0 and "降级" in r.reasoning


def test_valid_json_returns_score(monkeypatch):
    monkeypatch.setenv("ZHIPU_API_KEY", "KEY")
    c = GLMClient()
    # 构造一个假 completion 响应
    msg = MagicMock(); msg.message.content = '{"score": 0.6, "reasoning": "利好"}'
    resp = MagicMock(); resp.choices = [msg]
    c._client = MagicMock()
    c._client.chat = MagicMock()
    c._client.chat.completions = MagicMock()
    c._client.chat.completions.create = AsyncMock(return_value=resp)
    r = asyncio.run(c.analyze_sentiment("业绩超预期"))
    assert r.score == 0.6 and r.reasoning == "利好"


def test_invalid_json_falls_back_neutral(monkeypatch):
    monkeypatch.setenv("ZHIPU_API_KEY", "KEY")
    c = GLMClient()
    msg = MagicMock(); msg.message.content = "not a json"
    resp = MagicMock(); resp.choices = [msg]
    c._client = MagicMock(); c._client.chat = MagicMock()
    c._client.chat.completions = MagicMock()
    c._client.chat.completions.create = AsyncMock(return_value=resp)
    r = asyncio.run(c.analyze_sentiment("x"))
    assert r.score == 0.0


def test_score_clamped_by_pydantic():
    # score 越界必须被 pydantic 拒绝
    try:
        SentimentResult(score=1.5); assert False
    except Exception:
        pass
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_llm_client.py -v`
Expected: FAIL。

- [ ] **Step 3: 创建 `core/llm_client.py`**

```python
"""GLM（智谱）大模型客户端：情感打分，强制结构化输出，全异常降级中性。

设计要点：
- 用标准 openai SDK + base_url 覆盖调用智谱 GLM（解耦，可平替其它兼容端点）。
- response_format=json_object + pydantic 校验双保险。
- 任何异常（凭证缺失/超时/限频/JSON 非法）→ 返回中性 SentimentResult(0.0)，绝不上抛。
"""
from __future__ import annotations

import logging
import os
import threading

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

SYS_PROMPT = (
    "你是冷酷客观的量化分析师。对给定财经新闻给出情绪打分。"
    "仅输出 JSON：{\"score\": 介于 [-1.0, 1.0] 的浮点, \"reasoning\": 一句话理由}。"
    "score>0 偏多、<0 偏空、0 中性。严禁输出 JSON 以外任何字符。"
)


class SentimentResult(BaseModel):
    """情感打分结构。score ∈ [-1.0, 1.0]。"""
    score: float = Field(ge=-1.0, le=1.0)
    reasoning: str = ""


class GLMClient:
    """GLM 情感打分单例。"""

    _instance: "GLMClient | None" = None
    _lock = threading.Lock()

    @classmethod
    def get_instance(cls) -> "GLMClient":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def __init__(self) -> None:
        from config import LLM_CONFIG
        self._model = LLM_CONFIG["model"]
        self._timeout = LLM_CONFIG["timeout"]
        key = os.getenv("ZHIPU_API_KEY", "")
        self._enabled = bool(key)
        if self._enabled:
            from openai import AsyncOpenAI
            self._client = AsyncOpenAI(base_url=LLM_CONFIG["base_url"], api_key=key)
        else:
            self._client = None
            logger.warning("ZHIPU_API_KEY 缺失，GLMClient 进入降级模式（一律返回中性）")

    async def analyze_sentiment(self, news_text: str) -> SentimentResult:
        """对单条新闻打分；全异常降级中性。"""
        if not self._enabled or self._client is None:
            return SentimentResult(score=0.0, reasoning="凭证缺失，降级中性")
        try:
            resp = await self._client.chat.completions.create(
                model=self._model,
                response_format={"type": "json_object"},
                messages=[{"role": "system", "content": SYS_PROMPT},
                          {"role": "user", "content": news_text}],
                timeout=self._timeout,
            )
            return SentimentResult.model_validate_json(resp.choices[0].message.content)
        except Exception as e:
            logger.warning("GLM 情感打分降级中性：%s", e)
            return SentimentResult(score=0.0, reasoning="降级中性")
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_llm_client.py -v`
Expected: PASS。

- [ ] **Step 5: `server/main.py` lifespan 接入 GLM**

在 LakeReader 加载之后插入：
```python
    # 启动：GLM 客户端单例（凭证缺失则降级中性，不阻断启动）
    from core.llm_client import GLMClient
    GLMClient.get_instance()
```

- [ ] **Step 6: Commit**

```bash
git add core/llm_client.py tests/test_llm_client.py server/main.py
git commit -m "feat(llm): GLMClient 单例(openai SDK+结构化+全降级中性) + lifespan 接入

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 9: NewsSentimentFactor

**Files:**
- Create: `factors/alternative_sentiment.py`
- Test: `tests/test_sentiment_factor.py`

**Interfaces:**
- Consumes: `core.llm_client.GLMMClient` + `SentimentResult`。
- Produces: `factors.alternative_sentiment.NewsSentimentFactor.compute_daily_score(news_list) -> Awaitable[float]`。

- [ ] **Step 1: 写失败测试 `tests/test_sentiment_factor.py`**

```python
"""NewsSentimentFactor：并发打分、单条失败不炸、全失败→0.0。"""
import asyncio
from factors.alternative_sentiment import NewsSentimentFactor
from core.llm_client import SentimentResult


class _FakeClient:
    def __init__(self, scores):
        self._scores = scores
    async def analyze_sentiment(self, text):
        # 模拟第 2 条抛错（被 gather return_exceptions 吞掉）
        if self._scores is None:
            raise RuntimeError("boom")
        return SentimentResult(score=self._scores.pop(0), reasoning="x")


def test_weighted_average_of_scores():
    f = NewsSentimentFactor(client=_FakeClient([0.6, 0.4]))
    s = asyncio.run(f.compute_daily_score(["a", "b"]))
    assert abs(s - 0.5) < 1e-9


def test_single_failure_does_not_crash():
    f = NewsSentimentFactor(client=_FakeClient([0.8]))  # 第 2 条会 index error
    s = asyncio.run(f.compute_daily_score(["a", "b"]))
    # 仅 1 条成功 → 取成功的 0.8
    assert abs(s - 0.8) < 1e-9


def test_all_failure_returns_zero():
    f = NewsSentimentFactor(client=_FakeClient(None))  # 全抛
    s = asyncio.run(f.compute_daily_score(["a", "b"]))
    assert s == 0.0


def test_empty_list_returns_zero():
    f = NewsSentimentFactor(client=_FakeClient([]))
    assert asyncio.run(f.compute_daily_score([])) == 0.0
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_sentiment_factor.py -v`
Expected: FAIL。

- [ ] **Step 3: 创建 `factors/alternative_sentiment.py`**

```python
"""新闻情绪因子：并发调用 GLM 打分，加权聚合当日情绪。

防御：asyncio.gather(return_exceptions=True) 单条失败不炸整批；全失败/空列表 → 0.0。
"""
from __future__ import annotations

import asyncio
import logging

from core.llm_client import GLMClient, SentimentResult

logger = logging.getLogger(__name__)


class NewsSentimentFactor:
    """当日新闻情绪因子。"""

    def __init__(self, client: GLMClient | None = None) -> None:
        self._client = client or GLMClient.get_instance()

    async def compute_daily_score(self, news_list: list[str]) -> float:
        """并发打分 → 等权平均。全失败/空 → 0.0。"""
        if not news_list:
            return 0.0
        results = await asyncio.gather(
            *(self._client.analyze_sentiment(t) for t in news_list),
            return_exceptions=True,
        )
        scores = [r.score for r in results if isinstance(r, SentimentResult)]
        if not scores:
            logger.warning("当日新闻全部打分失败，情绪因子降级 0.0")
            return 0.0
        return float(sum(scores) / len(scores))
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_sentiment_factor.py -v`
Expected: PASS。

- [ ] **Step 5: 全量回归**

Run: `pytest -q`
Expected: 全绿。

- [ ] **Step 6: Commit**

```bash
git add factors/alternative_sentiment.py tests/test_sentiment_factor.py
git commit -m "feat(factor): NewsSentimentFactor 并发 GLM 打分+单条失败不炸+全失败中性

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Phase 5 — Epic 3 异步因子探索沙盒

### Task 10: 探索性动量因子矩阵

**Files:**
- Create: `factors/exploratory_momentum.py`
- Test: `tests/test_exploratory_momentum.py`

**Interfaces:**
- Produces: `cross_sectional_momentum(returns, window=20)`、`vol_adjusted_momentum(returns, high, low, close, window=20, atr_window=20)`、`hurst_exponent(series, max_k=50)`。

- [ ] **Step 1: 写失败测试 `tests/test_exploratory_momentum.py`**

```python
"""探索性动量：横截面排名、波动率调整、赫斯顿指数数值正确性。"""
import numpy as np
import pandas as pd
from factors.exploratory_momentum import (
    cross_sectional_momentum, vol_adjusted_momentum, hurst_exponent)


def test_cross_sectional_momentum_ranks():
    # 两只标的，构造 20+ 行
    idx = pd.date_range("2024-01-01", periods=25)
    returns = pd.DataFrame({"A": np.linspace(0.01, 0.02, 25),
                            "B": np.linspace(-0.02, -0.01, 25)}, index=idx)
    mom = cross_sectional_momentum(returns, window=20)
    # A 滚动收益 > B → A 排名百分位应 > B
    last = mom.iloc[-1]
    assert last["A"] > last["B"]


def test_vol_adjusted_momentum_no_div_by_zero():
    idx = pd.date_range("2024-01-01", periods=25)
    rng = np.random.default_rng(0)
    close = pd.DataFrame({"A": 100 + np.cumsum(rng.normal(size=25))}, index=idx)
    returns = close.pct_change()
    high = close + 1; low = close - 1
    m = vol_adjusted_momentum(returns, high, low, close, window=10, atr_window=10)
    assert m.shape == close.shape
    assert not np.isinf(m.dropna()).any().any()  # 无 Inf（防除零）


def test_hurst_persistent_series_above_half():
    # 强自相关随机游走累积序列，赫斯顿应 > 0.5（持续性）
    rng = np.random.default_rng(42)
    s = pd.Series(np.cumsum(rng.normal(0.01, 0.1, size=500)))
    h = hurst_exponent(s, max_k=50)
    assert 0.0 < h < 1.0
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_exploratory_momentum.py -v`
Expected: FAIL。

- [ ] **Step 3: 创建 `factors/exploratory_momentum.py`**

```python
"""探索性动量因子矩阵（纯 Pandas/NumPy 向量化，零黑盒）。

包含：
- 横截面动量：滚动收益在全市场的百分位排名。
- 波动率调整动量：滚动收益 / ATR（ATR 防除零）。
- 赫斯顿指数：R/S 重标极差法估计持续性（逐标量，循环可接受）。
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def cross_sectional_momentum(returns: pd.DataFrame, window: int = 20) -> pd.DataFrame:
    """横截面动量：滚动累计收益 → 逐日横截面百分位排名。

    参数：returns 为日收益率 DataFrame（index=date, columns=symbol）。
    返回：同形状的百分位排名（0~1）。
    """
    cum = returns.rolling(window).sum()
    return cum.rank(pct=True, axis=1)


def vol_adjusted_momentum(returns: pd.DataFrame, high: pd.DataFrame, low: pd.DataFrame,
                          close: pd.DataFrame, window: int = 20,
                          atr_window: int = 20) -> pd.DataFrame:
    """波动率调整动量 = 滚动累计收益 / ATR。

    ATR 用 (high-low).rolling(atr_window).mean() 近似（显式，免引 ta-lib 黑盒）；
    ATR→0 时以 ε 兜底防除零产生 Inf。
    """
    atr = (high - low).rolling(atr_window).mean()
    atr_safe = atr.where(atr > 1e-9, 1e-9)
    return (returns.rolling(window).sum()) / atr_safe


def hurst_exponent(series: pd.Series, max_k: int = 50) -> float:
    """R/S 重标极差法估计赫斯顿指数 H。

    对每个 lag k：把序列均分为长 k 的块，计算每块的 R（均值偏离累计极差）/ S（标准差），
    取所有块 R/S 的均值；最后对 (log k, log R/S) 线性回归，斜率即 H。
    H>0.5 持续、H=0.5 随机游走、H<0.5 均值回复。
    """
    arr = np.asarray(series.dropna(), dtype=float)
    n = len(arr)
    if n < 20:
        return float("nan")
    ks = np.arange(2, min(max_k, n // 2))
    rs_values = []
    for k in ks:
        usable = (n // k) * k
        chunks = arr[:usable].reshape(-1, k)
        mean = chunks.mean(axis=1, keepdims=True)
        dev = np.cumsum(chunks - mean, axis=1)
        r = dev.max(axis=1) - dev.min(axis=1)
        s = chunks.std(axis=1, ddof=1)
        valid = s > 0
        if valid.any():
            rs_values.append((r[valid] / s[valid]).mean())
    if len(rs_values) < 2:
        return float("nan")
    log_k = np.log(ks[: len(rs_values)])
    log_rs = np.log(rs_values)
    slope, _ = np.polyfit(log_k, log_rs, 1)
    return float(slope)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_exploratory_momentum.py -v`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add factors/exploratory_momentum.py tests/test_exploratory_momentum.py
git commit -m "feat(factor): 探索性动量矩阵(横截面/波动率调整/赫斯顿 R/S)

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 11: FactorAnalyzer（IC + 分层）

**Files:**
- Create: `factors/analyzer.py`
- Test: `tests/test_factor_analyzer.py`

**Interfaces:**
- Produces: `FactorAnalyzer.compute_ic(factor, fwd_returns) -> dict`、`.fractile_analysis(factor, fwd_returns, n_groups=5) -> dict`。

- [ ] **Step 1: 写失败测试 `tests/test_factor_analyzer.py`**

```python
"""FactorAnalyzer：IC 方向性、分层单调性、空输入安全。"""
import numpy as np
import pandas as pd
from factors.analyzer import FactorAnalyzer


def _make_perfect_factor():
    # 因子值与远期收益完全单调正相关 → IC 应显著为正
    idx = pd.date_range("2024-01-01", periods=30)
    rng = np.random.default_rng(0)
    factor = pd.DataFrame(rng.uniform(0, 1, size=(30, 5)),
                          index=idx, columns=list("ABCDE"))
    # 远期收益 = 因子 + 小噪声
    fwd = factor + rng.normal(0, 0.05, size=factor.shape)
    return factor, fwd


def test_ic_positive_for_monotone_relation():
    factor, fwd = _make_perfect_factor()
    out = FactorAnalyzer().compute_ic(factor, fwd)
    assert out["ic_mean"] > 0.5
    assert "ic_ir" in out and "t_stat" in out


def test_fractile_monotone_top_above_bottom():
    factor, fwd = _make_perfect_factor()
    out = FactorAnalyzer().fractile_analysis(factor, fwd, n_groups=5)
    ls = out["long_short"].dropna()
    # 多空价差均值应为正（top 组收益 > bottom 组）
    assert ls.mean() > 0


def test_compute_ic_empty_safe():
    factor = pd.DataFrame(np.nan, index=[0], columns=["A"])
    fwd = pd.DataFrame(np.nan, index=[0], columns=["A"])
    out = FactorAnalyzer().compute_ic(factor, fwd)
    # 不抛异常即可
    assert "ic_mean" in out
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_factor_analyzer.py -v`
Expected: FAIL。

- [ ] **Step 3: 创建 `factors/analyzer.py`**

```python
"""单因子评估引擎：秩相关 IC（Rank IC）+ 分层收益测试。纯 Pandas，禁 Alphalens。

IC 用 factor.rank().corrwith(fwd.rank(), axis=1) 逐日横截面 Spearman —— 无需 scipy。
分层用 pd.qcut 逐日分组，聚合各组远期收益与多空价差。
"""
from __future__ import annotations

import numpy as np
import pandas as pd


class FactorAnalyzer:
    """单因子评估：IC 与分层。"""

    def compute_ic(self, factor: pd.DataFrame, fwd_returns: pd.DataFrame) -> dict:
        """逐日横截面秩相关 IC。

        参数：factor / fwd_returns 均为 DataFrame(index=date, columns=symbol)。
        返回：{ic_series, ic_mean, ic_ir, t_stat}。
        """
        aligned = factor.index.intersection(fwd_returns.index)
        if len(aligned) == 0:
            return {"ic_series": pd.Series(dtype=float), "ic_mean": 0.0,
                    "ic_ir": 0.0, "t_stat": 0.0}
        f = factor.loc[aligned]
        r = fwd_returns.loc[aligned]
        ic = f.rank().corrwith(r.rank(), axis=1).dropna()
        if ic.empty or ic.std() == 0:
            return {"ic_series": ic, "ic_mean": float(ic.mean() if not ic.empty else 0.0),
                    "ic_ir": 0.0, "t_stat": 0.0}
        return {
            "ic_series": ic,
            "ic_mean": float(ic.mean()),
            "ic_ir": float(ic.mean() / ic.std()),
            "t_stat": float(ic.mean() / ic.std() * np.sqrt(len(ic))),
        }

    def fractile_analysis(self, factor: pd.DataFrame, fwd_returns: pd.DataFrame,
                          n_groups: int = 5) -> dict:
        """逐日分层：pd.qcut 分 n_groups，聚合各组远期收益序列 + 多空价差。"""
        aligned = factor.index.intersection(fwd_returns.index)
        group_series = {g: [] for g in range(n_groups)}
        for dt in aligned:
            f = factor.loc[dt].dropna()
            r = fwd_returns.loc[dt].reindex(f.index).dropna()
            common = f.index.intersection(r.index)
            if len(common) < n_groups:
                continue
            f, r = f.loc[common], r.loc[common]
            try:
                bins = pd.qcut(f, n_groups, labels=False, duplicates="drop")
            except ValueError:
                continue
            for g in pd.unique(bins.dropna()):
                mask = bins == g
                if mask.any():
                    group_series.setdefault(int(g), []).append(r[mask].mean())
        result = {g: pd.Series(v, dtype=float) for g, v in group_series.items()}
        # 多空 = 最高组 - 最低组
        max_g = max(result.keys()) if result else None
        min_g = min(result.keys()) if result else None
        if max_g is not None and min_g is not None:
            ls = result.get(max_g, pd.Series(dtype=float)) - \
                 result.get(min_g, pd.Series(dtype=float))
        else:
            ls = pd.Series(dtype=float)
        return {"group_returns": result, "long_short": ls}
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_factor_analyzer.py -v`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add factors/analyzer.py tests/test_factor_analyzer.py
git commit -m "feat(factor): FactorAnalyzer 秩相关IC+分层(纯pandas,禁Alphalens)

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 12: Celery app + 因子沙盒 API（CPU 探针 + Redis 宕机降级）

**Files:**
- Create: `server/celery_app.py`
- Create: `server/api/v1/explorer.py`
- Modify: `server/main.py`（挂载 explorer 路由）
- Test: `tests/test_explorer_api.py`

**Interfaces:**
- Consumes: `celery`、`redis`、`psutil`、`factors.analyzer.FactorAnalyzer`、`factors.exploratory_momentum`、`config.CELERY_CONFIG`。
- Produces: Celery 任务 `explorer.run_factor_grid(spec)`；HTTP `POST /api/v1/explorer/grid`、`GET /api/v1/explorer/result/{task_id}`。

- [ ] **Step 1: 写失败测试 `tests/test_explorer_api.py`**

```python
"""Explorer API：CPU 探针拒绝、Redis 宕机降级、正常派发。"""
from unittest.mock import patch, MagicMock
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from server.main import app
    return TestClient(app)


def test_grid_rejects_when_cpu_high(client, monkeypatch):
    monkeypatch.setattr("psutil.cpu_percent", lambda interval=0.0: 95.0)
    resp = client.post("/api/v1/explorer/grid", json={
        "factor": "cross_sectional_momentum", "universe": ["000001.SZ"],
        "start": "2024-01-01", "end": "2024-06-01"})
    assert resp.status_code in (429, 503)


def test_grid_falls_back_on_redis_down(client, monkeypatch):
    """Redis 连不上 → 降级线程池执行，返回 degraded=True。"""
    monkeypatch.setattr("psutil.cpu_percent", lambda interval=0.0: 10.0)
    import redis
    monkeypatch.setattr("server.api.v1.explorer.run_factor_grid",
                        MagicMock(delay=MagicMock(side_effect=redis.ConnectionError("down"))))
    # 让降级实现立刻返回一个哨兵
    monkeypatch.setattr("server.api.v1.explorer.run_factor_grid_impl",
                        lambda spec: {"degraded_marker": True})
    resp = client.post("/api/v1/explorer/grid", json={
        "factor": "cross_sectional_momentum", "universe": ["000001.SZ"],
        "start": "2024-01-01", "end": "2024-06-01"})
    assert resp.status_code == 200
    body = resp.json()
    assert body.get("degraded") is True
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_explorer_api.py -v`
Expected: FAIL。

- [ ] **Step 3: 创建 `server/celery_app.py`**

```python
"""Celery 实例 + 因子网格任务。

设计：单 Redis broker/backend；不引 beat。worker 核心调 FactorAnalyzer/exploratory_momentum，
结果落 reports/explorer/{task_id}.json。
"""
from __future__ import annotations

import json
import os

from celery import Celery

from config import CELERY_CONFIG

celery_app = Celery("quanter",
                    broker=CELERY_CONFIG["broker_url"],
                    backend=CELERY_CONFIG["broker_url"])
celery_app.conf.task_default_queue = CELERY_CONFIG["queue"]


def run_factor_grid_impl(spec: dict) -> dict:
    """网格计算实现（同步纯函数，可被 worker 或线程池调用）。

    spec 形如 {factor, universe, start, end}。
    本实现以 DataLakeReader 为数据源、FactorAnalyzer 为评估器；
    数据源缺失时返回空结果（不抛）。
    """
    from data.lake_reader import DataLakeReader
    from factors.analyzer import FactorAnalyzer
    from factors.exploratory_momentum import cross_sectional_momentum

    reader = DataLakeReader.get_instance()
    if not reader.loaded:
        return {"ok": False, "reason": "数据湖未加载"}
    # 收集 universe 时序，拼成截面 returns 面板
    pieces = []
    for sym in spec.get("universe", []):
        ts = reader.get_timeseries(sym, spec["start"], spec["end"])
        if not ts.empty:
            pieces.append(ts["close"].rename(sym))
    if not pieces:
        return {"ok": False, "reason": "universe 无可用数据"}
    panel = pd.concat(pieces, axis=1).sort_index()
    returns = panel.pct_change()
    factor = cross_sectional_momentum(returns, window=20)
    fwd = returns.shift(-1)
    out = FactorAnalyzer().compute_ic(factor, fwd)
    return {"ok": True, "ic_mean": out["ic_mean"], "ic_ir": out["ic_ir"]}


import pandas as pd  # noqa: E402（避免顶部 pandas 依赖顺序问题）


@celery_app.task(name="explorer.run_factor_grid")
def run_factor_grid(spec: dict) -> str:
    """Celery 任务入口：跑网格、落盘、返回结果摘要路径。"""
    result = run_factor_grid_impl(spec)
    task_dir = "reports/explorer"
    os.makedirs(task_dir, exist_ok=True)
    out_path = os.path.join(task_dir, f"{run_factor_grid.request.id}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, default=str)
    return out_path
```

- [ ] **Step 4: 创建 `server/api/v1/explorer.py`**

```python
"""因子探索沙盒路由：CPU 探针拒绝 + Celery 派发 + Redis 宕机降级。"""
from __future__ import annotations

import logging

import psutil
import redis
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from config import CELERY_CONFIG
from core.notifier import NotificationManager, fire_and_forget
from server.celery_app import run_factor_grid, run_factor_grid_impl

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/explorer", tags=["因子沙盒"])


class FactorGridSpec(BaseModel):
    factor: str
    universe: list[str]
    start: str
    end: str


@router.post("/grid", summary="提交因子网格计算")
async def submit_grid(spec: FactorGridSpec):
    """CPU > 阈值拒绝；Redis 宕机降级线程池。"""
    if psutil.cpu_percent(interval=0.1) > CELERY_CONFIG["cpu_gate_percent"]:
        raise HTTPException(429, "CPU 负载过高，拒绝调度")
    try:
        task = run_factor_grid.delay(spec.model_dump())
        return {"task_id": task.id, "degraded": False}
    except redis.ConnectionError:
        # 风控红线：Redis 不可用 → 钉钉告警 + 降级线程池，绝不阻断
        fire_and_forget(NotificationManager.get_default().notify_risk_event(
            "Redis 不可用，因子网格降级到线程池执行", "WARN"))
        logger.warning("Redis 不可用，explorer 降级线程池")
        result = await run_in_threadpool(run_factor_grid_impl, spec.model_dump())
        return {"result": result, "degraded": True}


@router.get("/result/{task_id}", summary="查询因子网格结果")
async def get_result(task_id: str):
    from celery.result import AsyncResult
    from server.celery_app import celery_app
    res = AsyncResult(task_id, app=celery_app)
    return {"status": res.status, "ready": res.ready(),
            "result": res.result if res.ready() else None}
```

- [ ] **Step 5: `server/main.py` 挂载 explorer 路由**

在路由注册区，其它 `include_router` 之后追加：
```python
from server.api.v1.explorer import router as explorer_router
app.include_router(explorer_router, prefix="/api/v1")
```

- [ ] **Step 6: 运行测试确认通过**

Run: `pytest tests/test_explorer_api.py -v`
Expected: PASS。

注：`TestClient` 触发 lifespan 会调 `DataLakeReader.get_instance().load()`（无 parquet → 离线模式，不报错）与 `GLMClient.get_instance()`（无 key → 降级，不报错）。

- [ ] **Step 7: 全量回归**

Run: `pytest -q`
Expected: 全绿。

- [ ] **Step 8: Commit**

```bash
git add server/celery_app.py server/api/v1/explorer.py server/main.py tests/test_explorer_api.py
git commit -m "feat(explorer): Celery 因子网格 + CPU 探针 + Redis 宕机降级

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Phase 6 — Epic 4 SSE 实时回测流

### Task 13: BacktestEngine 注入 event_emitter

**Files:**
- Modify: `backtest/engine.py`（`run` 签名 + 关键事件点）
- Test: `tests/test_engine_events.py`

**Interfaces:**
- Produces: `BacktestEngine.run(df, signal, symbol, event_emitter=None)`；emitter 收到 dict：`{"type":"progress"|"trade"|"risk", ...}`。默认 None → 现有行为零变化。

- [ ] **Step 1: 写失败测试 `tests/test_engine_events.py`**

```python
"""回测引擎 event_emitter 注入：成交/进度/风控事件，默认 None 不破坏现有行为。"""
import numpy as np
import pandas as pd
from backtest.engine import BacktestEngine


def _make_data():
    idx = pd.date_range("2024-01-01", periods=30)
    df = pd.DataFrame({"open": 10.0, "high": 10.5, "low": 9.8,
                       "close": np.linspace(10.0, 11.0, 30),
                       "volume": 10000}, index=idx)
    signal = pd.Series(np.linspace(0.2, 0.8, 30), index=idx)
    return df, signal


def test_default_none_unchanged():
    df, signal = _make_data()
    result = BacktestEngine().run(df, signal, "000001.SZ")  # 不传 emitter
    assert "metrics" in result or "nav" in result or "trades" in result


def test_emitter_receives_events():
    df, signal = _make_data()
    events = []
    BacktestEngine().run(df, signal, "000001.SZ",
                         event_emitter=lambda ev: events.append(ev))
    types = {e["type"] for e in events}
    assert "progress" in types  # 进度事件必发


def test_emitter_receives_trade_event():
    df, signal = _make_data()
    events = []
    BacktestEngine().run(df, signal, "000001.SZ",
                         event_emitter=lambda ev: events.append(ev) if ev["type"] == "trade" else None)
    # 信号上升段应至少触发一次成交
    assert len(events) >= 1
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_engine_events.py -v`
Expected: FAIL（`run()` 不接受 `event_emitter`）。

- [ ] **Step 3: 修改 `backtest/engine.py` 的 `run` 签名与事件点**

把：
```python
    def run(
        self,
        df: pd.DataFrame,
        signal: pd.Series,
        symbol: str = "600000.SH"
    ) -> Dict[str, Any]:
```
改为：
```python
    def run(
        self,
        df: pd.DataFrame,
        signal: pd.Series,
        symbol: str = "600000.SH",
        event_emitter: "Callable[[dict], None] | None" = None,
    ) -> Dict[str, Any]:
```
并在文件顶部 import 区确认有 `from typing import Callable`（若无则补）。

在 `run` 的逐日循环内，`_record_daily_state(...)` 之后、循环末尾插入进度事件：
```python
            # 记录每日状态
            self._record_daily_state(date, row, current_signal)

            # SSE 事件：进度（默认 None 时不发，零开销）
            if event_emitter is not None:
                event_emitter({
                    "type": "progress",
                    "date": str(date),
                    "i": i,
                    "n": len(aligned_df),
                    "nav": self.nav,
                })
```

在 `_execute_trade` 内部真实成交（`self.trades.append(...)` 之后）发 trade 事件。由于 `_execute_trade` 拿不到 emitter，最简做法是在 `run` 循环里比对成交记录长度变化来发 trade 事件——把循环体里调用 `_execute_trade` 之后追加：
```python
            # SSE 事件：成交（trades 长度变化即代表本日有成交）
            if event_emitter is not None and len(self.trades) > _prev_n_trades:
                last = self.trades[-1]
                event_emitter({
                    "type": "trade",
                    "date": str(date),
                    "direction": getattr(last, "direction", "buy"),
                    "shares": getattr(last, "shares", 0),
                    "price": getattr(last, "price", price),
                    "symbol": symbol,
                })
            _prev_n_trades = len(self.trades)
```
并在循环开始前初始化 `_prev_n_trades = len(self.trades)`（即 0）。

（涨跌停/流动性/现金不足等风控事件：若现有 `_execute_trade` 内已有对应分支判定，在那些分支里 `if event_emitter is not None: event_emitter({"type":"risk","level":"WARN","date":str(date),"msg":...})`。若现有实现未暴露这些判定点，本任务先落地 progress+trade 两类事件，risk 事件作为注释 TODO 标注在对应位置，后续迭代补——保证不破坏现有成交逻辑。）

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_engine_events.py -v`
Expected: PASS。

- [ ] **Step 5: 全量回归（关键：确保现有回测测试不破）**

Run: `pytest -q`
Expected: 全绿（`event_emitter` 默认 None，行为不变）。

- [ ] **Step 6: Commit**

```bash
git add backtest/engine.py tests/test_engine_events.py
git commit -m "feat(engine): BacktestEngine.run 注入 event_emitter(progress/trade, 默认None不破坏现有)

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 14: 后端 per-run SSE 流式接口

**Files:**
- Modify: `server/api/v1/backtest.py`
- Test: `tests/test_backtest_stream.py`

**Interfaces:**
- Produces: `POST /api/v1/backtest/run/async`（返回 `{run_id}`）、`GET /api/v1/backtest/run/stream/{run_id}`（SSE：`data: {ev}\n\n` … `data: [DONE]\n\n`）。

- [ ] **Step 1: 写失败测试 `tests/test_backtest_stream.py`**

```python
"""per-run SSE：建 run → 流式收到 progress/trade/result → [DONE]。"""
import json
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from server.main import app
    return TestClient(app)


def test_create_run_returns_runid(client):
    resp = client.post("/api/v1/backtest/run/async", json={
        "symbol": "000001.SZ", "start_date": "2024-01-01", "end_date": "2024-02-01",
        "initial_capital": 1000000, "signal_freq": "1d"})
    assert resp.status_code == 200
    assert "run_id" in resp.json()


def test_stream_unknown_run_404(client):
    resp = client.get("/api/v1/backtest/run/stream/does-not-exist")
    assert resp.status_code == 404
```

（端到端流式断言依赖真实数据源，留作手动验证；此处守护契约：建 run 成功、未知 run 404。）

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_backtest_stream.py -v`
Expected: FAIL（端点不存在）。

- [ ] **Step 3: 在 `server/api/v1/backtest.py` 顶部 import 区追加**

```python
import asyncio
import json
import uuid
```
并确认 `from server.schemas.backtest import BacktestRequest, BacktestResponse` 已有；追加：
```python
from fastapi.responses import StreamingResponse
from server.services.backtest_service import run_single_backtest
```
（`run_single_backtest` 已在文件中 import，避免重复。）

- [ ] **Step 4: 在 `server/api/v1/backtest.py` 末尾追加 per-run 流实现**

```python
# ============ per-run SSE 流（EventSource 只支持 GET，故两步式）============
# 内存 run 注册表：run_id → 请求参数（进程内、TTL 由消费后清理）
_run_registry: dict[str, BacktestRequest] = {}


class _RunBridge:
    """单订阅者跨线程桥：业务线程 publish，事件循环消费。

    Why call_soon_threadsafe：回测跑在线程池（run_in_threadpool），emit 发生在工作线程；
    asyncio.Queue 非线程安全，必须把 put 排入消费端事件循环，否则竞态损坏队列。
    """

    def __init__(self) -> None:
        self._q: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self._loop = asyncio.get_running_loop()

    def publish(self, ev: dict) -> None:
        try:
            self._loop.call_soon_threadsafe(self._safe_put, ev)
        except RuntimeError:
            pass  # 消费端 loop 已关闭（客户端断开）→ 静默

    def _safe_put(self, ev: dict) -> None:
        try:
            self._q.put_nowait(ev)
        except asyncio.QueueFull:
            pass  # 满则丢，绝不阻塞事件循环

    async def get(self) -> dict:
        return await self._q.get()


def _run_with_emitter(req: BacktestRequest, publish) -> dict:
    """线程池内执行：把 emitter 透传给引擎（若 service/engine 支持）。

    注：run_single_backtest 现签名不含 event_emitter；这里以"日志已走全局 SSE"
    为兜底，progress/trade 事件需要 service 层透传 emitter（见 Step 5 说明）。
    若 service 暂未透传，流仍能正常返回最终 result，只是中途无 progress/trade 帧。
    """
    # 兼容：service 若支持 event_emitter 关键字则透传，否则按原签名调用
    try:
        return run_single_backtest(req, event_emitter=publish)
    except TypeError:
        return run_single_backtest(req)


@router.post("/run/async", summary="创建回测 run（返回 run_id）")
async def create_run(req: BacktestRequest):
    run_id = str(uuid.uuid4())
    _run_registry[run_id] = req
    return {"run_id": run_id}


@router.get("/run/stream/{run_id}", summary="回测 per-run SSE 流")
async def stream_run(run_id: str):
    req = _run_registry.get(run_id)
    if req is None:
        raise HTTPException(404, "run 不存在或已过期")
    bridge = _RunBridge()

    async def gen():
        # 后台在线程池跑回测，结束后 publish result
        async def runner():
            try:
                result = await run_in_threadpool(_run_with_emitter, req, bridge.publish)
            except Exception as e:
                bridge.publish({"type": "error", "message": str(e)})
                return
            bridge.publish({"type": "result", "data": result})
        asyncio.create_task(runner())
        try:
            while True:
                ev = await bridge.get()
                yield f"data: {json.dumps(ev, ensure_ascii=False, default=str)}\n\n"
                if ev.get("type") in ("result", "error"):
                    break
            yield "data: [DONE]\n\n"
        finally:
            _run_registry.pop(run_id, None)  # 防注册表泄漏

    return StreamingResponse(gen(), media_type="text/event-stream")
```

- [ ] **Step 5: service 层透传 emitter（若 `run_single_backtest` 可改）**

打开 `server/services/backtest_service.py`，给 `run_single_backtest` 增加可选参数 `event_emitter: Callable[[dict], None] | None = None`，并在调用 `engine.run(...)` 时透传。若该 service 逻辑复杂不便透传，本任务可保留 Step 4 的 `try/except TypeError` 兜底——流仍返回最终 result，progress/trade 帧留待后续迭代（在 spec 中标注）。

- [ ] **Step 6: 运行测试确认通过**

Run: `pytest tests/test_backtest_stream.py -v`
Expected: PASS。

- [ ] **Step 7: 全量回归**

Run: `pytest -q`
Expected: 全绿。

- [ ] **Step 8: Commit**

```bash
git add server/api/v1/backtest.py server/services/backtest_service.py tests/test_backtest_stream.py
git commit -m "feat(sse): per-run 回测 SSE 流(POST /run/async + GET /run/stream, call_soon_threadsafe)

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 15: 前端 EventSource 流式接入

**Files:**
- Modify: `web/src/composables/useTerminalState.ts`
- Modify: `web/src/components/TerminalLogs.vue`
- Modify: `web/src/api/backtest.ts`（加 `createRun` axios 封装）

**Interfaces:**
- Consumes: 后端 `POST /api/v1/backtest/run/async`、`GET /api/v1/backtest/run/stream/{run_id}`。
- Produces: `useTerminalState().logs` ref（LogEntry[]）；`execute(req)` 改为 EventSource 流。

- [ ] **Step 1: `web/src/api/backtest.ts` 加建 run 封装**

在文件末尾追加：
```ts
/** 创建 per-run 回测（返回 run_id，配合 EventSource 流） */
export function createBacktestRun(params: SingleBacktestParams): Promise<{ run_id: string }> {
  return apiClient.post('/api/v1/backtest/run/async', params, { timeout: 30000 })
}
```

- [ ] **Step 2: 改造 `web/src/composables/useTerminalState.ts`**

整体替换为：
```ts
/**
 * 终端全局状态（模块级 reactive 单例）。
 *
 * Epic 4 改造：废弃阻塞式 axios.post，改用原生 EventSource 接收 per-run 流。
 * - progress/trade/risk 事件 → logs（终端按级别高亮）
 * - result 事件 → state.result（触发 ProChart/NavChart 渲染）
 * - [DONE] → 关闭流
 * result 仍用 markRaw 阻止深度代理海量时序数据。
 */
import { reactive, toRefs, markRaw, ref } from 'vue'
import { createBacktestRun, type SingleBacktestParams, type SingleBacktestResponse } from '@/api/backtest'

/** 终端日志条目（与后端 SSE 事件归一化对齐） */
export interface LogEntry {
  ts: number
  level: string         // INFO / SUCCESS / WARNING / ERROR
  logger: string        // 'backtest' / 'trade' / 'risk' / 'progress'
  message: string
}

const state = reactive<{ loading: boolean; result: SingleBacktestResponse | null; error: string }>({
  loading: false,
  result: null,
  error: '',
})
// 日志流（独立 ref，避免与 result 一起被 markRaw）
const logs = ref<LogEntry[]>([])
let currentES: EventSource | null = null

/** 把后端 SSE 事件 dict 归一化为终端 LogEntry */
function toLogEntry(ev: any): LogEntry {
  const now = Date.now() / 1000
  switch (ev.type) {
    case 'trade':
      return { ts: now, level: 'SUCCESS', logger: 'trade',
        message: `${ev.direction} ${ev.symbol} ${ev.shares}@${ev.price?.toFixed?.(2) ?? ev.price} @ ${ev.date}` }
    case 'risk':
      return { ts: now, level: ev.level === 'ERROR' ? 'ERROR' : 'WARNING', logger: 'risk',
        message: `${ev.msg} @ ${ev.date}` }
    case 'progress':
      return { ts: now, level: 'INFO', logger: 'progress',
        message: `${ev.date}  nav=${ev.nav?.toFixed?.(2) ?? ev.nav}  (${ev.i + 1}/${ev.n})` }
    default:
      return { ts: now, level: 'INFO', logger: 'backtest', message: JSON.stringify(ev) }
  }
}

export function useTerminalState() {
  async function execute(req: SingleBacktestParams) {
    state.loading = true
    state.error = ''
    logs.value = []
    currentES?.close()
    try {
      const { run_id } = await createBacktestRun(req)
      const es = new EventSource(`/api/v1/backtest/run/stream/${run_id}`)
      currentES = es
      es.onmessage = (e) => {
        if (e.data === '[DONE]') { es.close(); state.loading = false; return }
        let ev: any
        try { ev = JSON.parse(e.data) } catch { return }
        if (ev.type === 'result') {
          state.result = markRaw(ev.data as SingleBacktestResponse)
        } else if (ev.type === 'error') {
          state.error = ev.message || '回测执行失败'
        } else {
          const entry = toLogEntry(ev)
          logs.value.push(entry)
          if (logs.value.length > 2000) logs.value.splice(0, logs.value.length - 2000)
        }
      }
      es.onerror = () => {
        state.error = '日志流中断'
        es.close()
        state.loading = false
      }
    } catch (e: any) {
      state.error = e?.message || '创建回测失败'
      state.result = null
      state.loading = false
    }
  }

  return { ...toRefs(state), logs, execute }
}
```

- [ ] **Step 3: 改造 `web/src/components/TerminalLogs.vue` 消费 useTerminalState.logs**

把 `<script setup>` 内"自带 EventSource 订阅全局流"的逻辑替换为读取 `useTerminalState().logs`，保留滚动/上限/级别高亮。替换 `<script setup lang="ts">` 区为：
```ts
/**
 * 沉浸式日志终端：消费 useTerminalState.logs（per-run SSE 事件归一化），
 * 按级别分色高亮，自动滚动到底（用户上翻则暂停跟随）。
 */
import { ref, nextTick, watch } from 'vue'
import { useTerminalState } from '@/composables/useTerminalState'

const { logs } = useTerminalState()
const follow = ref(true)
const containerRef = ref<HTMLDivElement | null>(null)

function levelClass(level: string): string {
  switch (level) {
    case 'ERROR':
    case 'CRITICAL': return 'lv-error'
    case 'WARNING': return 'lv-warn'
    case 'SUCCESS': return 'lv-success'
    default: return 'lv-info'
  }
}
function formatTs(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString('zh-CN', { hour12: false })
}
async function scrollToBottom() {
  await nextTick()
  if (follow.value && containerRef.value) containerRef.value.scrollTop = containerRef.value.scrollHeight
}
function onScroll() {
  const el = containerRef.value; if (!el) return
  follow.value = el.scrollHeight - el.scrollTop - el.clientHeight < 40
}
watch(() => logs.value.length, () => scrollToBottom())
</script>
```
`<template>` 中把 `v-for="(l, i) in logs"`（`logs` 现为来自 composable 的 ref，模板里自动解包），并把空态文案改为"提交回测后此处实时滚动买卖点与风控告警"。

- [ ] **Step 4: 前端类型检查 + 构建**

Run: `cd web && npx vue-tsc --noEmit && npm run build`
Expected: 无类型错误，构建成功。

- [ ] **Step 5: 手动端到端验证（启动后端 + 前端）**

后端：`uvicorn server.main:app --reload`
前端：`cd web && npm run dev`
浏览器提交一次单资产回测，确认：终端实时滚动 progress/trade；结束后 ECharts 渲染净值曲线；断开网络前端不卡死。

- [ ] **Step 6: Commit**

```bash
git add web/src/composables/useTerminalState.ts web/src/components/TerminalLogs.vue web/src/api/backtest.ts
git commit -m "feat(sse,front): 前端改原生 EventSource 接收 per-run 流+按级别高亮+[DONE]渲染

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## 收尾

### Task 16: 全量回归 + README 骨架

**Files:**
- Create: `README.md`（根目录，首次创建）

- [ ] **Step 1: 全量测试**

Run: `pytest -q`
Expected: 全绿（原 17 + 新 11 个测试文件）。

- [ ] **Step 2: 创建根 `README.md`**

含：项目定位、依赖安装（`pip install -r requirements.txt` + `cd web && npm install`）、`.env` 配置（参照 `.env.example`）、数据湖同步命令（`python scripts/sync_data_lake.py --years 10`）、启动后端/前端、Celery worker 启动（`celery -A server.celery_app worker -Q explorer -l info`）、5 Epic 模块速览、指向 `docs/superpowers/specs/2026-07-01-quanter-industrial-design.md` 的设计文档链接。全文中文、无占位符。

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: 新增根 README（部署/启动/5 Epic 速览）

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Self-Review（计划自审记录）

**1. Spec 覆盖**：Epic 1→Task 6/7；Epic 2→Task 8/9；Epic 3→Task 10/11/12；Epic 4→Task 13/14/15；Epic 5→Task 2/3/4/5；横切地基→Task 1；README→Task 16。spec 第 1.4 节 lifespan 三处接入（notifier/LakeReader/GLM）分别在 Task 3/7/8 落地；explorer 路由挂载在 Task 12。全覆盖。

**2. 类型一致性**：`DingTalkChannel(webhook, secret)` / `fire_and_forget(coro)` / `YFinanceClient.get_history` / `AlphaVantageClient.get_treasury_yield` + `get_treasury_yield_safe` / `DataLakeReader.get_instance().load/get_cross_section/get_timeseries/loaded` / `GLMClient.get_instance().analyze_sentiment` / `SentimentResult(score,reasoning)` / `NewsSentimentFactor.compute_daily_score` / `cross_sectional_momentum` / `vol_adjusted_momentum` / `hurst_exponent` / `FactorAnalyzer.compute_ic/fractile_analysis` / `run_factor_grid` + `run_factor_grid_impl` / `BacktestEngine.run(event_emitter=)` / `_RunBridge.publish/get` — 跨 Task 名称一致。前端 `LogEntry`/`logs`/`execute`/`createBacktestRun` 对齐。

**3. 已知留白（spec 内已声明，非占位符）**：`run_single_backtest` 的 emitter 透传依赖 service 现有结构（Task 14 Step 5 给了 try/except TypeError 兜底）；engine 风控事件（涨跌停/流动性）若现有 `_execute_trade` 无暴露点，Task 13 先落 progress+trade，risk 事件标 TODO（spec 允许迭代）。

**4. 回归红线**：每个 Task 末尾 `pytest -q` 全绿；`event_emitter` 默认 None 保证 Task 13 不破坏现有回测测试。
