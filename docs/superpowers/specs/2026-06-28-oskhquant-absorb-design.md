# 设计文档：吸收 OSkhQuant 实战架构到 quanter 后端

- **日期**：2026-06-28
- **状态**：设计已获批，待实现
- **范围**：FastAPI 后端 + 核心量化库的企业级扩展（严禁引入任何 PyQt/GUI 代码）
- **推进策略**：统一 spec，按模块 ①→⑤ 顺序实现

---

## 1. 背景与目标

参考传统桌面端开源项目 `OSkhQuant` 的 A 股实盘经验，将其最核心的实战业务架构吸收到现有 `quanter`（FastAPI + Vue3）Web 架构中，完成 5 个模块的后端企业级重构：

1. 动态策略插件系统（`strategies/`）
2. 本土化指标库 MyTT（`factors/mytt.py`）
3. 独立事前风控模块（`trading/risk_manager.py` + `trading/broker.py`）
4. 后台定时调度引擎（`server/core/scheduler.py`）
5. QMT 本地文件解析器（`data/qmt_parser.py`）

**核心架构理念**：以"单资产 = 单标的组合的退化"为统一抽象，把信号生成与订单执行彻底解耦，让回测与实盘走完全同构的执行链，仅在最末端的 Broker 实现上分叉。

---

## 2. 关键决策记录（brainstorming 阶段已对齐）

| 决策项 | 选定方案 | 理由 |
|---|---|---|
| 推进策略 | 统一 spec，顺序实现 | 5 模块互相依赖，先一次对齐契约再按序落地 |
| BaseStrategy 信号语义 | 统一为 `TargetWeightSignal` 权重信号 | 单资产视为单标的组合退化，引擎只走一条路径 |
| 单资产 Series 路径 | **下线删除** | 不保留 deprecated，统一走 `run_portfolio` |
| 风控拦截点 | 引入轻量 Broker 抽象，RiskManager 前置 | 风控与引擎解耦，实盘可复用同一条链 |
| QMT 文件格式 | 本地 SQLite/DB | 用 sqlite3 + pandas.read_sql 读取 |
| MyTT 引入方式 | 自实现纯向量化函数（零依赖） | 守 CLAUDE.md 反黑盒、第一性原理底线 |
| 交易日历 | 接口抽象 + Tushare trade_cal 缓存 | 复用已有数据缓存基础设施，节假日自动更新，不硬编码 |
| 调度任务执行 | 走 `run_in_threadpool` | 与回测同事件循环保护红线 |

---

## 3. 架构总览：五层执行链

```
┌─────────────────────────────────────────────────────────────────────┐
│  数据层  DataFetcher（双轨）                                         │
│    慢车道: FRED/Tushare（宏观+基本面，带 Parquet 缓存，保持现状）    │
│    快车道: QMTDataParser（本地 SQLite 极速读取）── 模块⑤新增         │
└──────────────────────────┬──────────────────────────────────────────┘
                           │ OHLCV: Dict[str, DataFrame]
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│  策略层  BaseStrategy.generate_target_weights() ── 模块①            │
│    ├ HMMMacroStrategy（改写自现 portfolio_service）                  │
│    ├ MaCrossStrategy / BollStrategy（MyTT 指标驱动）── 模块②        │
│    └ StrategyLoader(importlib 扫描) → /api/v1/strategies             │
└──────────────────────────┬──────────────────────────────────────────┘
                           │ List[TargetWeightSignal]
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│  引擎层  BacktestEngine.run_portfolio() → process_target_weight...   │
│    产出 List[Order]（A 股整手取整 + 碎股过滤 + 现金约束，保持现状）  │
└──────────────────────────┬──────────────────────────────────────────┘
                           │ List[Order]
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│  风控层  RiskManager.check(...) ── 模块③  [事前拦截]                 │
│    拒绝→记录原因丢弃 / 通过↓                                        │
└──────────────────────────┬──────────────────────────────────────────┘
                           │ List[Order]（放行）
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│  执行层  Broker.execute(order)                                       │
│    回测: BacktestBroker（内存撮合，移植现 _execute_portfolio_order） │
│    实盘: QmtBroker（适配器，本期仅留接口）                           │
└─────────────────────────────────────────────────────────────────────┘
        ▲
        │ 交易日 09:25 触发信号 / 15:30 拉数据
┌───────┴───────────────────────────────────────────────────────────┐
│  调度层  APScheduler（lifespan 挂载/销毁）── 模块④                  │
└───────────────────────────────────────────────────────────────────┘
```

**贯穿原则**：
- **极简反黑盒**：MyTT 自实现、交易日历不硬编码、Broker 是薄抽象而非框架。
- **回测/实盘同构**：策略→引擎→风控→Broker 这条链在回测和实盘完全一致，仅 Broker 实现不同。这是模块③引入 Broker 抽象的真正价值。
- **统一信号语义**：所有策略只产出 `List[TargetWeightSignal]`，引擎只保留 `run_portfolio`。

---

## 4. 项目结构变更

```
quanter/
├── strategies/                    【新增·模块①】策略插件包
│   ├── __init__.py
│   ├── base.py                    # BaseStrategy 抽象基类 + StrategyContext
│   ├── loader.py                  # StrategyLoader（importlib 动态扫描）
│   ├── hmm_macro_strategy.py      # HMM 策略（改写自 portfolio_service）
│   └── ma_cross_strategy.py       # 示例：双均线策略（含 MyTT 用法）
├── factors/
│   ├── technical.py               【改·模块②】引入 mytt 调用
│   ├── mytt.py                    【新增·模块②】自实现通达信指标库（零依赖）
│   ├── fusion.py                  【保持】signal_fusion 供策略内部使用
│   └── hmm_macro.py               【保持】MacroRegimeHMM 供 HMM 策略使用
├── trading/                       【新增·模块③】交易执行与风控
│   ├── __init__.py
│   ├── broker.py                  # Broker 抽象 + BacktestBroker + QmtBroker(接口)
│   └── risk_manager.py            # RiskManager（3 维度事前拦截）
├── backtest/
│   ├── engine.py                  【改】删除 run()；process_target_weight_signal 接 Broker/RiskManager
│   └── cost_model.py              【保持】
├── data/
│   ├── fetcher.py                 【保持】
│   └── qmt_parser.py              【新增·模块⑤】QMTDataParser（SQLite）
├── server/
│   ├── core/
│   │   ├── config.py              【改】调度参数 + 风控参数 + QMT 路径
│   │   └── scheduler.py           【新增·模块④】APScheduler 装配
│   ├── api/v1/
│   │   ├── strategies.py          【新增·模块①】GET /api/v1/strategies
│   │   ├── backtest.py            【改】run_single_backtest 走策略+run_portfolio
│   │   └── portfolio.py           【保持】
│   ├── services/
│   │   ├── backtest_service.py    【改】下线 Series 路径，改走策略
│   │   └── portfolio_service.py   【改】HMM 逻辑迁入 HMMMacroStrategy 后瘦身
│   ├── schemas/
│   │   └── backtest.py            【改】BacktestRequest 增加可选 strategy_name
│   └── main.py                    【改】lifespan 挂载调度器 + 扫描策略
├── trading_calendar.py            【新增·模块④支撑】A 股交易日历（接口+缓存实现）
└── docs/superpowers/specs/        【本文件所在】
```

---

## 5. 模块详细设计

### 5.1 模块① 策略插件系统

#### 5.1.1 BaseStrategy 契约（`strategies/base.py`）

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import ClassVar, Dict, List, Optional, Any
import pandas as pd
from factors.fusion import TargetWeightSignal

@dataclass
class StrategyContext:
    """策略运行时只读快照（防策略误改账户状态）"""
    timestamp: pd.Timestamp
    current_weights: Dict[str, float]      # 引擎算出的当前实际权重
    cash: float
    aum: float
    params: Dict[str, Any] = field(default_factory=dict)

class BaseStrategy(ABC):
    name: ClassVar[str]                    # 策略唯一标识（前端下拉框 key，必填）
    universe: ClassVar[List[str]]          # 标的池

    @abstractmethod
    def fit(self, price_data: Dict[str, pd.DataFrame],
            macro_data: Optional[pd.DataFrame] = None) -> None:
        """训练阶段（如 HMM 训练）。无状态策略实现为 pass"""

    @abstractmethod
    def generate_target_weights(
        self, price_data: Dict[str, pd.DataFrame], ctx: StrategyContext,
    ) -> List[TargetWeightSignal]:
        """产出每日目标权重信号（复用现有 TargetWeightSignal，不新造类型）"""
```

**约束**：
- `name` 为 ClassVar 且必填，作为 StrategyLoader 注册 key 与去重依据。
- 策略约定为"fit 后只读"，并发回测时由 service 层每请求 new 一个实例（与 engine 同生命周期），杜绝跨请求状态污染。
- 不复用 `name`/`universe` 作为实例属性，避免实例化时重复传参。

#### 5.1.2 StrategyLoader（`strategies/loader.py`）

```python
class StrategyLoader:
    def __init__(self, package_name: str = "strategies"):
        self._package = package_name
        self._registry: Dict[str, type[BaseStrategy]] = {}

    def scan(self) -> None:
        """启动时扫描 strategies/ 白名单目录下所有模块，收集带 name 的 BaseStrategy 子类"""
        import importlib, pkgutil, inspect
        pkg = importlib.import_module(self._package)
        for _, modname, _ in pkgutil.iter_modules(pkg.__path__):
            mod = importlib.import_module(f"{self._package}.{modname}")
            for _, cls in inspect.getmembers(mod, inspect.isclass):
                if (issubclass(cls, BaseStrategy) and cls is not BaseStrategy
                        and getattr(cls, "name", None)):
                    self._registry[cls.name] = cls

    def get(self, name: str) -> type[BaseStrategy]:
        if name not in self._registry:
            raise KeyError(f"未注册的策略: {name}")
        return self._registry[name]

    def list(self) -> List[Dict[str, Any]]:
        """供 /api/v1/strategies 返回"""
        return [{"name": n, "universe": c.universe} for n, c in self._registry.items()]
```

**安全红线**：只扫描 `strategies/` 白名单目录（非任意路径），且要求类显式声明 `name` 才注册，杜绝隐式/恶意加载。

#### 5.1.3 API（`server/api/v1/strategies.py`）

```python
@router.get("", summary="列出已注册策略")
async def list_strategies(loader: StrategyLoader = Depends(get_loader)) -> List[Dict]:
    return loader.list()
```

`get_loader` 返回应用启动时 scan 过的单例（存于 `app.state`）。

#### 5.1.4 下线 Series 路径的牵连（已确认）

- **删除** `BacktestEngine.run()`（单资产 Series 路径）。
- **`schemas/backtest.py`**：`BacktestRequest` 新增 `strategy_name: Optional[str] = None`。缺省时 service 层用内置 `MaCrossFusionStrategy`（封装现有 tech+macro 融合逻辑）。
- **`backtest_service.run_single_backtest`** 改造为：取数 → 实例化策略（按 `strategy_name` 或默认）→ `strategy.fit()` → 构造单标的 `price_data={symbol: df}` → `strategy.generate_target_weights` → `engine.run_portfolio`。
- **`portfolio_service`**：步骤 2-5（对齐→HMM 训练→predict→mapper→signals）迁入 `HMMMacroStrategy`，service 瘦身为"取数→实例化 HMM 策略→run_portfolio"。
- **路由契约不变**：`/api/v1/backtest`、`/portfolio` 入参出参结构不变，前端无需改。

### 5.2 模块② MyTT 指标（`factors/mytt.py`）

**反黑盒立场**：不 `pip install mytt`，自实现通达信公式的 numpy/pandas 翻译。所有函数输入输出均为 `pd.Series`，索引即时间轴，天然对齐。

```python
import pandas as pd

def EMA(s: pd.Series, n: int) -> pd.Series:
    """指数移动平均（通达信递归式，adjust=False）"""
    return s.ewm(span=n, adjust=False).mean()

def MA(s: pd.Series, n: int) -> pd.Series:
    """简单移动平均"""
    return s.rolling(n).mean()

def MACD(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    """MACD：返回 (DIF, DEA, HIST)。HIST 按通达信约定 ×2"""
    dif = EMA(close, fast) - EMA(close, slow)
    dea = EMA(dif, signal)          # 关键：对 DIF 再求 EMA，非对 close
    hist = (dif - dea) * 2
    return dif, dea, hist

def BOLL(close: pd.Series, n: int = 20, p: int = 2):
    """布林带：返回 (UPPER, MID, LOWER)。用总体标准差 ddof=0"""
    mid = MA(close, n)
    std = close.rolling(n).std(ddof=0)
    return mid + p * std, mid, mid - p * std
```

**technical.py 引入示例**（替代手写 MACD）：
```python
from factors.mytt import MACD, BOLL
def macd_signal(df, fast=12, slow=26, signal=9):
    dif, dea, hist = MACD(df["close"], fast, slow, signal)
    # 金叉/死叉判定复用现有 shift(1) 防前视偏差逻辑
def boll_bands(df, n=20, p=2):
    return BOLL(df["close"], n, p)
```

### 5.3 模块③ 风控 + Broker（`trading/`）

#### 5.3.1 Broker 抽象（`trading/broker.py`）

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass
from backtest.engine import Order

@dataclass
class FillResult:
    filled: bool
    filled_shares: int
    avg_price: float
    cost: float
    reason: str = ""

class Broker(ABC):
    @abstractmethod
    def execute(self, order: Order, account: "BacktestEngine") -> FillResult:
        """执行订单，更新 account 的 cash/持仓，返回成交结果"""

class BacktestBroker(Broker):
    """内存撮合——整体移植现 _execute_portfolio_order 的成本模型
    （佣金万三/最低5元、卖出印花税千五、沪市过户费十万分之一）"""

class QmtBroker(Broker):
    def execute(self, order, account):
        raise NotImplementedError("实盘 QMT 适配器本期仅留接口，待后续接入 xtdata")
```

#### 5.3.2 RiskManager（`trading/risk_manager.py`）

```python
@dataclass
class RiskDecision:
    approved: bool
    reason: str = ""

@dataclass
class MarketState:
    """单标的当日可交易状态（回测用 OHLC 近似，实盘从行情接口取）"""
    is_tradable: bool          # 非停牌
    is_limit_up_locked: bool   # 一字涨停（无法买入）
    is_limit_down_locked: bool # 一字跌停（无法卖出）

class RiskManager:
    def __init__(self,
                 max_single_position_ratio: float = 0.30,  # 单股占总权益上限
                 max_daily_new_positions: int = 5,         # 单日最大开仓次数
                 enable_market_filter: bool = True):
        self._max_single = max_single_position_ratio
        self._max_daily = max_daily_new_positions
        self._enable_filter = enable_market_filter
        self._daily_buy_count = 0           # 当日 BUY 计数，按交易日 reset

    def reset_daily(self) -> None:
        """引擎每日循环开始时调用，重置日内开仓计数"""
        self._daily_buy_count = 0

    def check(self, order: Order, *, account_aum: float,
              current_position_value: float,
              market_state: MarketState) -> RiskDecision:
        """三维度事前拦截。SELL 仅受维度①约束（减仓释放风险应放行）"""
        # ① 停牌/一字涨跌停过滤
        # ② BUY 且 self._daily_buy_count >= self._max_daily → 拒
        # ③ BUY 后 (current_position_value + order金额) / aum > self._max_single → 拒
```

**拦截规则**：
- 维度①停牌/涨跌停：BUY 遇一字涨停拒、SELL 遇一字跌停拒、停牌全拒。
- 维度②单日开仓次数：仅约束 BUY，SELL 放行。
- 维度③个股占比上限：仅约束 BUY，按"下单后该股市值 / AUM"判定。
- 通过后若为 BUY，`_daily_buy_count += 1`。

#### 5.3.3 engine 接入改造（`backtest/engine.py`）

`BacktestEngine.__init__` 新增 `broker: Broker` 与 `risk_manager: RiskManager` 参数（均有合理默认值，缺省为 `BacktestBroker()` 与 `RiskManager()`，保证旧调用方可无感升级）。

**接口扩展（消除歧义）**：风控判定涨跌停需当日完整 OHLC，而现有 `process_target_weight_signal(signal, prices)` 只接收开盘价。因此扩展签名为：

```python
def process_target_weight_signal(
    self,
    signal: TargetWeightSignal,
    prices: Dict[str, float],                 # 执行价（开盘价），保持原义
    day_bars: Optional[Dict[str, pd.Series]] = None,  # 新增：当日完整 OHLC，供风控判定涨跌停
) -> List[Order]:
```

`run_portfolio` 每日循环里已持有 `price_data[symbol].loc[date]`，构造 `day_bars` 传入即可。`day_bars=None` 时（如单测）退化为不启用涨跌停过滤。

**风控接入（步骤 5/6 改造）**：
```python
for order in sell_orders + buy_orders:
    market_state = self._build_market_state(order.symbol, day_bars)   # 回测：OHLC 近似一字板
    decision = self.risk_manager.check(
        order, account_aum=self.calculate_aum(),
        current_position_value=self._position_value_of(order.symbol), # = 持仓股数 × 最新收盘价
        market_state=market_state,
    )
    if not decision.approved:
        self._record_failed_trade(date=order.timestamp,
                                  reason=f"风控拒绝: {decision.reason}", ...)
        continue
    fill = self.broker.execute(order, self)      # 回测=BacktestBroker
    # FillResult.filled=False（如资金不足部分成交）→ 记录失败
```

**新增辅助方法**：
- `_build_market_state(symbol, day_bars) -> MarketState`：`day_bars` 为空 → 全可交易；否则用 `open==high==close` 近似一字涨停、`open==low==close` 近似一字跌停。
- `_position_value_of(symbol) -> float`：`持仓股数 × latest_prices[symbol]`，复用 `calculate_aum` 的价格防御逻辑。

`run_portfolio` 每日循环开始时调 `self.risk_manager.reset_daily()`。原 `_execute_portfolio_order` 的成本计算与状态变更逻辑整体移入 `BacktestBroker.execute`（`BacktestBroker` 直接操作传入的 engine 的 `cash`/`positions_dict`——这是有意的薄耦合，broker 作为执行层本就需要修改账户状态）。

**注**：`BacktestBroker.execute` 参数 `account` 类型即 `BacktestEngine`，二者耦合是务实选择，不引入额外的 Account 抽象（YAGNI）。

### 5.4 模块④ 调度引擎

#### 5.4.1 交易日历（`trading_calendar.py`）

不硬编码节假日（避免幻觉与过期）。提供接口 + Tushare trade_cal 缓存实现：

```python
from datetime import date
from typing import Optional
import pandas as pd

def is_trading_day(d: date) -> bool:
    """A 股是否交易日。优先查本地缓存（Tushare trade_cal），缓存缺失时退化为周末判断 + 日志告警"""
    # 1. 周末直接 False
    # 2. 查 trade_cal 缓存（复用 data/fetcher 的 Parquet 缓存模式）
    # 3. 缓存缺失 → 记录 warning，返回 d.weekday() < 5（保守可能误判节假日）
```

实现细节：缓存键 `trade_cal_{year}`，用 `TushareDataFetcher` 拉 `trade_cal` 接口（exchange=SSE），落 Parquet。无 Tushare token 时退化为周末判断并告警。

#### 5.4.2 Scheduler（`server/core/scheduler.py`）

```python
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from starlette.concurrency import run_in_threadpool

class SchedulerService:
    def __init__(self):
        self._sched = AsyncIOScheduler(timezone="Asia/Shanghai")

    def start(self):
        # 交易日 15:30 增量拉取收盘数据
        self._sched.add_job(self._pull_eod_data,
                            CronTrigger(day_of_week="mon-fri", hour=15, minute=30),
                            id="pull_eod", misfire_grace_time=600, coalesce=True)
        # 交易日 09:25 生成当日目标仓位信号
        self._sched.add_job(self._generate_signals,
                            CronTrigger(day_of_week="mon-fri", hour=9, minute=25),
                            id="gen_signals", misfire_grace_time=300, coalesce=True)
        self._sched.start()

    async def _pull_eod_data(self):
        """伪代码：调 DataFetcher 增量拉 K 线 + 宏观数据"""
        from trading_calendar import is_trading_day
        if not is_trading_day(date.today()):
            return
        await run_in_threadpool(_do_pull_eod)     # IO 密集，卸载线程池

    async def _generate_signals(self):
        """伪代码：实例化策略 → generate_target_weights → 落库当日目标仓位"""
        from trading_calendar import is_trading_day
        if not is_trading_day(date.today()):
            return
        await run_in_threadpool(_do_generate_signals)

    def shutdown(self, wait: bool = True):
        self._sched.shutdown(wait=wait)
```

**要点**：`AsyncIOScheduler` 与 FastAPI 共用事件循环；cron 仅约束 `mon-fri`，节假日判断在任务体内早退；`misfire_grace_time` 防服务重启后补跑过期任务失控；任务体走 `run_in_threadpool`（与回测同事件循环保护红线）。

#### 5.4.3 main.py lifespan 挂载/销毁

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动：扫描策略 + 启动调度器
    loader = StrategyLoader(); loader.scan()
    app.state.strategy_loader = loader
    scheduler = SchedulerService(); scheduler.start()
    app.state.scheduler = scheduler
    yield
    # 销毁：等待任务退出
    scheduler.shutdown(wait=False)

app = FastAPI(lifespan=lifespan, title="Quanter 量化平台", version="2.0.0")
```

### 5.5 模块⑤ QMT 解析器（`data/qmt_parser.py`）

```python
import sqlite3
import pandas as pd
from datetime import datetime

class QMTDataParser:
    """读取 miniQMT 本地 SQLite 行情库，对齐为系统标准 OHLCV。

    表结构为假设（需按实际 miniQMT schema 调整 SCHEMA 常量）。
    假设表 stockbar(code, time, open, high, low, close, volume, amount)。
    """

    TABLE = "stockbar"
    CODE_COL = "code"
    TIME_COL = "time"

    def __init__(self, db_path: str):
        self._conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)  # 只读连接
        self._conn.execute("PRAGMA query_only = ON")                       # 防误写

    def fetch_ohlcv(self, symbol: str, start: datetime, end: datetime,
                    freq: str = "1d") -> pd.DataFrame:
        sql = (f"SELECT {self.TIME_COL}, open, high, low, close, volume, amount "
               f"FROM {self.TABLE} WHERE {self.CODE_COL}=? "
               f"AND {self.TIME_COL} BETWEEN ? AND ? ORDER BY {self.TIME_COL}")
        df = pd.read_sql(sql, self._conn, params=(symbol, start, end))
        return self._normalize(df)

    def _normalize(self, df: pd.DataFrame) -> pd.DataFrame:
        # time 字段单位适配（秒/毫秒 epoch 或字符串），try-except 兼容
        df[self.TIME_COL] = pd.to_datetime(df[self.TIME_COL], errors="coerce")
        df = df.dropna(subset=[self.TIME_COL]).set_index(self.TIME_COL)
        if df.index.tz is None:
            df.index = df.index.tz_localize("Asia/Shanghai")
        # 过滤非法价（≤0 / NaN / Inf）
        for col in ("open", "high", "low", "close"):
            df = df[df[col] > 0]
        return df[["open", "high", "low", "close", "volume", "amount"]]

    def close(self):
        self._conn.close()
```

**对接**：与 `DataFetcher` 同构，可作为 `CompositeDataFetcher` 的快车道 fetcher。表名/字段名集中在类常量，便于按实际 schema 调整。

---

## 6. 交叉关注点

### 6.1 新增依赖
- **仅 `apscheduler`** 一个 pip 包（调度无可替代）。
- MyTT、交易日历、SQLite 解析全部用标准库 + 自实现，守住反黑盒底线。

### 6.2 错误处理
- **风控拒绝**：记录原因 + 丢弃订单（非异常），不中断回测/实盘。
- **调度任务异常**：任务体内 try-except 全包，记日志，绝不崩调度器（APScheduler 默认也会因异常移除 job，需显式 `max_instances` + 兜底捕获）。
- **策略加载失败**：单个策略 import/扫描异常跳过，不阻塞其他策略与启动。
- **QMT 读取**：只读连接 + `query_only` PRAGMA；time 字段单位 try-except 兼容；非法价过滤。
- **前视偏差**：MyTT 函数本身是纯计算，前视偏差由调用方（策略）用 `shift(1)` 控制，与现有 technical.py 一致。

### 6.3 测试策略
- **BaseStrategy/StrategyLoader**：stub 策略测扫描、去重、name 缺失不注册。
- **RiskManager**：构造 Order + AUM + MarketState，测 3 维度的通过/拒绝边界（含 BUY/SELL 区别）。
- **BacktestBroker**：移植后用 fixture 订单验证成本计算与持仓变更与原 `_execute_portfolio_order` 一致。
- **QMTDataParser**：内存 SQLite fixture（`sqlite3.connect(":memory:")`）测取数与标准化。
- **trading_calendar**：mock trade_cal 缓存测 `is_trading_day`。
- **端到端**：用 MockDataFetcher 跑一遍"策略→run_portfolio→风控→broker"全链，验证净值/交易记录正确。

### 6.4 向后兼容
- `/api/v1/backtest`、`/api/v1/portfolio` 路由入参出参结构不变，前端无需改。
- `signal_fusion`、`MacroRegimeHMM`、`HMMStateMapper`、`TargetWeightSignal`、`CostModel` 全部保留，被策略/service 复用。
- `BacktestRequest.strategy_name` 为可选，缺省行为对齐现状。

### 6.5 实现顺序（依赖驱动）
1. 模块② MyTT（无依赖，先就位供策略用）
2. 模块① 策略系统（base → loader → ma_cross 示例 → HMM 改写 → service 改造 → API）
3. 模块③ broker + risk_manager（engine 接入）
4. 模块⑤ QMT 解析器（独立，可并行）
5. 模块④ 调度（依赖策略 + 数据，最后装配）

---

## 7. 风险与未决项

| 风险 | 处理 |
|---|---|
| 下线 `run()` 影响现有单资产回测语义 | service 改造 + 内置默认策略保证行为对齐，端到端测试校验净值一致 |
| RiskManager 日内计数有状态 | 每日 `reset_daily()`；实盘需加锁（本期回测单实例串行无并发） |
| MarketState 实盘来源 | 本期回测用 OHLC 近似，实盘 MarketState 接口预留 |
| QMT SQLite schema 实际未知 | 表名/字段集中常量，按实际 schema 调整；需用户提供样例确认 |
| 节假日表过期 | 用 Tushare trade_cal 缓存自动更新，无 token 时退化告警 |
| APScheduler job 异常被移除 | 任务体全包 try-except + `coalesce`/`max_instances` 配置 |

---

## 8. 不做（YAGNI）

- 不做 QMT 实盘下单（QmtBroker 仅留接口）。
- 不做策略参数热更新 UI（`/api/v1/strategies` 仅返回 name/universe）。
- 不做策略版本管理/回滚。
- 不做 GUI/PyQt 任何代码（架构红线）。
- 不做多 Broker 并行。
