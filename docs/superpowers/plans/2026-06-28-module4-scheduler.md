# 模块④ 调度引擎 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 引入 APScheduler 后台调度，在 FastAPI lifespan 中挂载/销毁；交易日 15:30 增量拉取收盘数据、09:25 生成当日目标仓位信号；并提供自维护的 A 股交易日历。

**Architecture:** `AsyncIOScheduler` 与 FastAPI 共用事件循环；cron 仅约束 `mon-fri`，节假日判断在任务体内早退；任务体走 `run_in_threadpool`（与回测同事件循环保护红线）。交易日历接口抽象 + Tushare trade_cal 缓存，无 token 退化为周末判断 + 告警。

**Tech Stack:** Python 3, `apscheduler`（**新增依赖**，调度无可替代）, pandas, pytest。

## Global Constraints

- 严禁 PyQt/GUI；全中文注释（含 Why）；扁平反黑盒
- **新增依赖**：`pip install apscheduler`（scheduler 之前在 requirements.txt 加 `apscheduler>=3.10`）
- 调度任务异常 try-except 全包，绝不崩调度器；`coalesce=True`、`max_instances=1`
- 任务体走 `run_in_threadpool`（IO/CPU 密集，避免阻塞事件循环）
- 交易日历不硬编码节假日（幻觉红线），用 trade_cal 缓存 + 无 token 退化告警
- **依赖模块①**（策略系统，09:25 任务调策略生成信号）

## File Structure

| 文件 | 责任 | 动作 |
|---|---|---|
| `trading_calendar.py` | A 股交易日历（is_trading_day + 可注入节假日） | 新建 |
| `server/core/scheduler.py` | `SchedulerService`（APScheduler 装配 + 两任务） | 新建 |
| `server/main.py` | lifespan 挂载/销毁 scheduler | 修改 |
| `requirements.txt` | 加 apscheduler | 修改 |
| `tests/test_scheduler.py` | 调度与日历测试 | 新建 |

---

## Task 1: `trading_calendar.py`（A 股交易日历）

**Files:**
- Create: `trading_calendar.py`
- Test: `tests/test_scheduler.py`（新建，含 `TestTradingCalendar`）

**Interfaces:**
- Consumes: 无（trade_cal 缓存可选依赖 Tushare）
- Produces: `is_trading_day(d) -> bool`、`_set_holidays(set)`（测试注入）

- [ ] **Step 1: 写失败测试**

新建 `tests/test_scheduler.py`：
```python
"""调度引擎与交易日历单元测试"""
from datetime import date, datetime

import pytest

from trading_calendar import is_trading_day, _set_holidays, _reset_holidays


class TestTradingCalendar:
    """测试 A 股交易日历"""

    def setup_method(self):
        _reset_holidays()   # 每个测试重置缓存

    def test_weekend_not_trading(self):
        """周末非交易日"""
        assert is_trading_day(date(2023, 1, 7)) is False   # 周六
        assert is_trading_day(date(2023, 1, 8)) is False   # 周日

    def test_weekday_trading_without_holidays(self):
        """无节假日缓存时，工作日为交易日（退化模式）"""
        assert is_trading_day(date(2023, 1, 3)) is True    # 周二

    def test_holiday_not_trading(self):
        """节假日非交易日（注入节假日 set）"""
        _set_holidays({date(2023, 1, 2)})   # 假设 1/2 周一为节假日
        assert is_trading_day(date(2023, 1, 2)) is False

    def test_accepts_datetime(self):
        """接受 datetime 入参（自动取 date）"""
        assert is_trading_day(datetime(2023, 1, 7, 10, 0)) is False

    def test_holiday_cache_used(self):
        """节假日缓存生效（同日二次调用一致）"""
        _set_holidays({date(2023, 5, 1)})
        assert is_trading_day(date(2023, 5, 1)) is False
        assert is_trading_day(date(2023, 5, 1)) is False
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_scheduler.py::TestTradingCalendar -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'trading_calendar'`

- [ ] **Step 3: 写最小实现**

新建 `trading_calendar.py`：
```python
"""A 股交易日历

设计（反黑盒、不硬编码节假日）：
- 提供接口 is_trading_day(d)
- 节假日来源：Tushare trade_cal 接口拉取并本地缓存（复用 data/fetcher 缓存模式）
- 无 Tushare token / 拉取失败 → 退化为周末判断 + 告警（保守可能误判节假日）
- 节假日缓存可由测试通过 _set_holidays 注入

为什么不硬编码节假日 set：
- 节假日每年变动（春节/国庆调休），硬编码会过期（幻觉 + 维护负担）
- trade_cal 是权威来源，缓存后毫秒级读取
"""
import logging
from datetime import date, datetime
from typing import Optional, Set

logger = logging.getLogger(__name__)

# 模块级节假日缓存（date set）；None 表示未加载
_HOLIDAYS_CACHE: Optional[Set[date]] = None


def _load_holidays() -> Set[date]:
    """加载节假日 set（带缓存）。无 token 时退化为空 set + 告警"""
    global _HOLIDAYS_CACHE
    if _HOLIDAYS_CACHE is not None:
        return _HOLIDAYS_CACHE

    try:
        # 尝试从 Tushare trade_cal 拉取并缓存（实盘环境）
        # 此处仅做接口预留；真实拉取由 data/fetcher 的 trade_cal 缓存承担
        # 拉取失败时退化
        _HOLIDAYS_CACHE = _fetch_holidays_from_tushare()
    except Exception as e:
        logger.warning(
            f"交易日历 trade_cal 缓存缺失（{e}），退化为周末判断，"
            f"节假日可能误判。请配置 Tushare token 并预热 trade_cal。"
        )
        _HOLIDAYS_CACHE = set()
    return _HOLIDAYS_CACHE


def _fetch_holidays_from_tushare() -> Set[date]:
    """从 Tushare trade_cal 拉取节假日（需 token）。

    本期为接口预留：实际项目可调用 data.fetcher.TushareDataFetcher
    拉 trade_cal(exchange='SSE', is_open='0') 并缓存。
    """
    # 无 token 环境抛异常 → 由 _load_holidays 捕获退化
    from data.fetcher import TushareDataFetcher  # noqa: F401（延迟导入）
    raise RuntimeError("trade_cal 拉取未配置（需 Tushare token）")


def is_trading_day(d) -> bool:
    """A 股是否交易日

    参数：
        d: date 或 datetime

    返回：
        True=交易日，False=周末或节假日
    """
    if isinstance(d, datetime):
        d = d.date()
    if not isinstance(d, date):
        raise TypeError(f"期望 date/datetime，收到 {type(d)}")

    # 周末直接非交易日
    if d.weekday() >= 5:
        return False
    # 节假日判断（缓存）
    return d not in _load_holidays()


def _set_holidays(holidays: Set[date]) -> None:
    """测试注入节假日缓存"""
    global _HOLIDAYS_CACHE
    _HOLIDAYS_CACHE = set(holidays)


def _reset_holidays() -> None:
    """测试重置缓存（强制下次重新加载）"""
    global _HOLIDAYS_CACHE
    _HOLIDAYS_CACHE = None
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_scheduler.py::TestTradingCalendar -v`
Expected: PASS — 5 个测试全绿

- [ ] **Step 5: 提交**

```bash
git add trading_calendar.py tests/test_scheduler.py
git commit -m "feat(calendar): 新增 A 股交易日历（trade_cal 缓存 + 退化告警）"
```

---

## Task 2: `server/core/scheduler.py`（SchedulerService）

**Files:**
- Create: `server/core/scheduler.py`
- Test: `tests/test_scheduler.py`（追加 `TestSchedulerService`）

**Interfaces:**
- Consumes: `trading_calendar.is_trading_day`、策略系统（09:25 任务）、DataFetcher（15:30 任务）
- Produces: `SchedulerService(scheduler=None).start()/shutdown()`

**前置**：`pip install apscheduler`，并在 `requirements.txt` 加 `apscheduler>=3.10`。

- [ ] **Step 1: 写失败测试**

在 `tests/test_scheduler.py` 追加（import 区补）：
```python
from unittest.mock import MagicMock, patch
from server.core.scheduler import SchedulerService
```

```python
class TestSchedulerService:
    """测试调度服务（注入 mock scheduler，避免事件循环依赖）"""

    def test_start_registers_two_jobs(self):
        """start 注册 2 个定时任务（15:30 拉数据、09:25 生成信号）"""
        mock_sched = MagicMock()
        svc = SchedulerService(scheduler=mock_sched)
        svc.start()
        assert mock_sched.add_job.call_count == 2
        mock_sched.start.assert_called_once()

    def test_shutdown_calls_scheduler_shutdown(self):
        """shutdown 调用 scheduler.shutdown"""
        mock_sched = MagicMock()
        svc = SchedulerService(scheduler=mock_sched)
        svc.shutdown()
        mock_sched.shutdown.assert_called_once()

    def test_pull_eod_skips_non_trading_day(self):
        """非交易日 _pull_eod_data 早退（不拉数据）"""
        import asyncio
        svc = SchedulerService(scheduler=MagicMock())
        with patch("server.core.scheduler.is_trading_day", return_value=False):
            with patch("server.core.scheduler.run_in_threadpool") as mock_pool:
                asyncio.run(svc._pull_eod_data())
                mock_pool.assert_not_called()

    def test_generate_signals_skips_non_trading_day(self):
        """非交易日 _generate_signals 早退"""
        import asyncio
        svc = SchedulerService(scheduler=MagicMock())
        with patch("server.core.scheduler.is_trading_day", return_value=False):
            with patch("server.core.scheduler.run_in_threadpool") as mock_pool:
                asyncio.run(svc._generate_signals())
                mock_pool.assert_not_called()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_scheduler.py::TestSchedulerService -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'server.core.scheduler'`

- [ ] **Step 3: 安装依赖并写实现**

先 `pip install "apscheduler>=3.10"`，并在 `requirements.txt` 末尾加一行：
```
apscheduler>=3.10
```

新建 `server/core/scheduler.py`：
```python
# -*- coding: utf-8 -*-
"""后台定时调度引擎（APScheduler）

借鉴 OSkhQuant GUIScheduler.py，走向无人值守自动化。
两个实盘维度任务：
- 交易日 15:30：增量拉取当日收盘 K 线 + 宏观数据
- 交易日 09:25：实例化策略生成当日目标仓位信号

设计：
- AsyncIOScheduler 与 FastAPI 共用事件循环
- cron 仅约束 mon-fri，节假日判断在任务体内早退
- 任务体走 run_in_threadpool（IO/CPU 密集，避免阻塞事件循环）
- 异常 try-except 全包，绝不崩调度器
"""
import asyncio
import logging
from datetime import date
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from starlette.concurrency import run_in_threadpool

from trading_calendar import is_trading_day

logger = logging.getLogger(__name__)


class SchedulerService:
    def __init__(self, scheduler: Optional[AsyncIOScheduler] = None):
        # scheduler 可注入（便于测试 mock）；默认 AsyncIOScheduler
        self._sched = scheduler if scheduler is not None else AsyncIOScheduler(
            timezone="Asia/Shanghai"
        )

    def start(self) -> None:
        """注册任务并启动调度器"""
        self._sched.add_job(
            self._pull_eod_data,
            CronTrigger(day_of_week="mon-fri", hour=15, minute=30),
            id="pull_eod", misfire_grace_time=600, coalesce=True, max_instances=1,
        )
        self._sched.add_job(
            self._generate_signals,
            CronTrigger(day_of_week="mon-fri", hour=9, minute=25),
            id="gen_signals", misfire_grace_time=300, coalesce=True, max_instances=1,
        )
        self._sched.start()
        logger.info("调度引擎已启动：15:30 拉数据 / 09:25 生成信号")

    def shutdown(self, wait: bool = False) -> None:
        """关闭调度器"""
        try:
            self._sched.shutdown(wait=wait)
            logger.info("调度引擎已关闭")
        except Exception as e:
            logger.warning(f"调度引擎关闭异常：{e}")

    async def _pull_eod_data(self) -> None:
        """交易日 15:30：增量拉取当日收盘 K 线 + 宏观数据"""
        try:
            if not is_trading_day(date.today()):
                return
            await run_in_threadpool(self._do_pull_eod)
        except Exception as e:
            logger.error(f"15:30 拉数据任务异常：{e}", exc_info=True)

    async def _generate_signals(self) -> None:
        """交易日 09:25：实例化策略生成当日目标仓位信号"""
        try:
            if not is_trading_day(date.today()):
                return
            await run_in_threadpool(self._do_generate_signals)
        except Exception as e:
            logger.error(f"09:25 生成信号任务异常：{e}", exc_info=True)

    # ====== 任务同步实现（run_in_threadpool 内执行） ======

    def _do_pull_eod(self) -> None:
        """实盘：调 DataFetcher 增量拉取（本期为脚手架，用 Mock 演示）"""
        # 实盘应换为 CompositeDataFetcher/QMT 增量拉取当日数据并入库
        from data.fetcher import MockDataFetcher
        from datetime import datetime
        fetcher = MockDataFetcher()
        today = datetime.combine(date.today(), datetime.min.time())
        logger.info("【调度·拉数据】模拟拉取当日 OHLCV（实盘替换为真实 fetcher）")

    def _do_generate_signals(self) -> None:
        """实盘：实例化策略生成当日目标仓位并落库/推送"""
        # 实盘应：loader.get(strategy_name) → 实例化 → fit → generate_target_weights
        #         → 取最后一日权重 → 推送下单信号 / 落库
        logger.info("【调度·生成信号】模拟生成当日目标仓位（实盘替换为策略执行+落库）")
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_scheduler.py::TestSchedulerService -v`
Expected: PASS — 4 个测试全绿

- [ ] **Step 5: 提交**

```bash
git add server/core/scheduler.py requirements.txt tests/test_scheduler.py
git commit -m "feat(scheduler): 新增 APScheduler 调度服务（15:30 拉数据/09:25 生成信号）"
```

---

## Task 3: `main.py` lifespan 挂载/销毁 scheduler

**Files:**
- Modify: `server/main.py`（lifespan 加 scheduler）
- Test: `tests/test_scheduler.py`（追加 `TestLifespanScheduler`）

**Interfaces:**
- Consumes: `SchedulerService`（Task 2）、模块① lifespan（loader scan）
- Produces: `app.state.scheduler`

- [ ] **Step 1: 写失败测试**

在 `tests/test_scheduler.py` 追加（import 区补）：
```python
from fastapi.testclient import TestClient
from server.main import app
```

```python
class TestLifespanScheduler:
    """测试 lifespan 挂载/销毁 scheduler"""

    def test_lifespan_starts_and_stops_scheduler(self):
        """TestClient 触发 lifespan：启动时 scheduler 就绪，退出时无异常"""
        with TestClient(app) as client:
            assert app.state.scheduler is not None
            assert client.get("/health").status_code == 200
        # 退出 with 块触发 shutdown，不抛异常即通过

    def test_strategies_still_listed_after_scheduler_added(self):
        """scheduler 加入 lifespan 后，/api/v1/strategies 仍可用"""
        with TestClient(app) as client:
            resp = client.get("/api/v1/strategies")
            assert resp.status_code == 200
            names = [it["name"] for it in resp.json()]
            assert "ma_cross" in names
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_scheduler.py::TestLifespanScheduler -v`
Expected: FAIL — `app.state.scheduler` 不存在（lifespan 未挂 scheduler）

- [ ] **Step 3: 改 `server/main.py` 的 lifespan**

在 `server/main.py` 顶部 import 区补：
```python
from server.core.scheduler import SchedulerService
```

把模块① 写入的 lifespan（loader scan 那段）改为同时挂载 scheduler：
```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动：扫描策略 + 启动调度器
    loader = StrategyLoader()
    loader.scan()
    app.state.strategy_loader = loader

    scheduler = SchedulerService()
    scheduler.start()
    app.state.scheduler = scheduler
    yield
    # 销毁：关闭调度器（等待任务退出）
    scheduler.shutdown(wait=False)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_scheduler.py::TestLifespanScheduler -v`
Expected: PASS

- [ ] **Step 5: 全量回归测试**

Run: `python -m pytest tests/ -v`
Expected: 全绿

- [ ] **Step 6: 提交**

```bash
git add server/main.py tests/test_scheduler.py
git commit -m "feat(server): lifespan 挂载/销毁 APScheduler 调度引擎"
```

---

## 验收标准

- [ ] `trading_calendar.is_trading_day` 可用，节假日可注入
- [ ] `SchedulerService` 注册 2 个 cron 任务，异常不崩
- [ ] `main.py` lifespan 挂载/销毁 scheduler，`/health` 与 `/api/v1/strategies` 仍可用
- [ ] `requirements.txt` 含 `apscheduler>=3.10`
- [ ] `python -m pytest tests/ -v` 全绿
- [ ] 3 个独立 commit

## 收尾

5 个模块全部就绪。执行顺序按依赖：②→①→③→⑤→④（②①③ 已在前序 plan，⑤④ 在本系列）。全部 plan 完成后，按 spec 验收整体联调。
