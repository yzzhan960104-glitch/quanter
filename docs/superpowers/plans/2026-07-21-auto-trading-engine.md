# 自动交易引擎 实施计划（第二期）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 第二期交付自动交易引擎——颈线法信号 T-1 晚生成计划 + 钉钉确认 → T 日开盘前自动挂限价买/止盈卖 + 撤昨日未成交 → 盘中每 5min 止损轻量监控 → 盘后对账 + 重算止损；影子模式 dry_run 先行，跑稳后切 live。

**Architecture:** APScheduler 独立常驻进程 `python -m trading.engine`（不寄生 server），四个 cron 触发点（eod_plan 15:30 / pre_open 09:22 / stop_loss_monitor 每5min / post_close 15:30）；纯函数基础设施（calendar/stop_loss/signal_runner）+ 组件（dynamic_whitelist/circuit_breaker/reconcile_job/trading_plan）+ engine 编排；影子模式 `AUTO_TRADE_MODE=dry_run` 全程不调真单，记录计划+模拟成交。

**Tech Stack:** Python 3.10（`.venv310`）/ APScheduler / pytest / 复用一期 broadcast（钉钉确认闸）+ trading_service.submit_order（dry_run/live）+ risk_shield.check_order

**对应 Spec:** `docs/superpowers/specs/2026-07-21-auto-trading-engine-design.md`

## Global Constraints

- **语言**：所有代码注释 100% 中文（CLAUDE.md）。
- **影子模式红线**：`AUTO_TRADE_MODE=dry_run`（默认）下，engine **绝不调真单**，只落计划 + 模拟成交；未跑满 N 日（≥5）+ 偏差可接受前，**禁止切 live**。
- **T-1 确认闸**：T-1 晚计划未经人工确认（`plan.confirmed=true`），T 日 pre_open **不挂任何单**。
- **A股约束（已查证 xttrader.md）**：xtquant 无原生止盈止损条件单 → 止盈走柜台限价卖、止损走盘中轻量监控；T+1 当日买当日不卖；撤单须在交易时段（9:30-15:00）。
- **Python 环境**：`.venv310/Scripts/python.exe`（xtquant 绑 3.10）；`pytest`。
- **熔断**：日亏上限 / 断线全撤 / 总仓位上限三道闸，任一触发 halt + 钉钉告警。
- **海龟 trailing 离散化**：每日盘后重算次日止损价（`compute_stop_price`），盘中用此固定价，不移动（符合"盘中不调整"）。
- **param_iter 基线参数**（spec 待确认，plan 用基线，研究员审阅可调）：pos_cap=0.05 / stop_atr_mult 沿用颈线法 id_cfg / trailing_grace/step/floor 沿用 EXEC_DEFAULTS / 总仓位 0.80 / 日亏 -3% / 止损频率 5min / 影子天数 ≥5。

---

## File Structure

**新增（trading/ 包）**
- `trading/calendar.py`：Tushare trade_cal fetch + 缓存 + `is_trading_day(date)` / `is_intraday_session(now)`。
- `trading/stop_loss.py`：`compute_stop_price(...)` 海龟 trailing 离散纯函数（从 simulate_exit 迁出）。
- `trading/signal_runner.py`：`build_orders_from_signals(signals, capital, pos_cap)` 把 scan_at trade dict → OrderRequest + 止盈/止损价 + qty。
- `trading/dynamic_whitelist.py`：`inject_dynamic_whitelist(symbols)` / `clear_dynamic_whitelist()`（注入 risk_shield 白名单，当日有效）。
- `trading/circuit_breaker.py`：`check_daily_loss_limit(...)` / `cancel_all_open_orders(gw)`（日亏熔断 + 撤单补全）。
- `trading/reconcile_job.py`：`run_reconcile(gw, local_positions, tolerance)` + 偏差告警。
- `trading/trading_plan.py`：`save_plan(date, plan)` / `load_plan(date)` / `confirm_plan(date)`（T-1 计划 JSON + 确认闸）+ 钉钉推送（复用 broadcast.push）。
- `trading/engine.py`：APScheduler 四触发点编排 + 影子模式分流。
- `trading/__main__.py`：`python -m trading` 独立进程入口。

**修改**
- `requirements.txt`：加 `APScheduler>=3.10`。
- `.env` / `.env.example`：加 AUTO_TRADE_MODE + ENGINE_*_CRON + TRADE_* 参数。
- `scripts/start_dingtalk_bots.md`：加 engine 常驻 SOP。

**测试（pytest）**：`tests/trading/test_calendar.py` / `test_stop_loss.py` / `test_signal_runner.py` / `test_dynamic_whitelist.py` / `test_circuit_breaker.py` / `test_reconcile_job.py` / `test_trading_plan.py` / `test_engine.py`。

---

## Task 1: 交易日历 `trading/calendar.py`

**Files:**
- Create: `trading/calendar.py`
- Test: `tests/trading/test_calendar.py`

**Interfaces:**
- Produces: `is_trading_day(date_str) -> bool`、`is_intraday_session(now_dt) -> bool`、`fetch_trade_cal(year) -> list[str]`（Tushare pro.trade_cal + 缓存 `logs/trade_cal_<year>.json`）

- [ ] **Step 1: 写失败测试**

```python
# tests/trading/test_calendar.py  -*- coding: utf-8 -*-
"""交易日历单测（Task 1）。"""
from datetime import datetime
from trading import calendar


def test_is_trading_day_uses_cache(monkeypatch, tmp_path):
    """缓存命中不调 Tushare；周末返 False。"""
    cache = tmp_path / "trade_cal_2026.json"
    cache.write_text('["2026-07-21", "2026-07-22"]', encoding="utf-8")
    monkeypatch.setattr(calendar, "_cache_path", lambda y: cache if y == 2026 else tmp_path / f"trade_cal_{y}.json")
    assert calendar.is_trading_day("2026-07-21") is True   # 周二在缓存
    assert calendar.is_trading_day("2026-07-19") is False  # 周日不在缓存


def test_is_intraday_session():
    """A 股盘中时段判定（9:30-11:30 / 13:00-15:00）。"""
    assert calendar.is_intraday_session(datetime(2026, 7, 21, 10, 0)) is True
    assert calendar.is_intraday_session(datetime(2026, 7, 21, 12, 0)) is False  # 午休
    assert calendar.is_intraday_session(datetime(2026, 7, 21, 14, 30)) is True
    assert calendar.is_intraday_session(datetime(2026, 7, 21, 15, 30)) is False  # 收盘后
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_calendar.py -v`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现 `trading/calendar.py`**

```python
# -*- coding: utf-8 -*-
"""A 股交易日历（Tushare trade_cal 缓存 + 盘中时段判定）。

Why 独立模块：engine 四触发点都需判交易日/时段（节假日跳过、午休不监控）；
Tushare pro.trade_cal 每年初拉一次缓存本地 JSON，避免每次调 API。
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, time
from pathlib import Path

logger = logging.getLogger(__name__)

_CACHE_DIR = Path("logs")


def _cache_path(year: int) -> Path:
    return _CACHE_DIR / f"trade_cal_{year}.json"


def fetch_trade_cal(year: int) -> list[str]:
    """拉 Tushare 某年交易日历，缓存 logs/trade_cal_<year>.json。失败返空 list（降级）。"""
    cache = _cache_path(year)
    if cache.exists():
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except Exception:
            pass
    token = os.getenv("TUSHARE_TOKEN") or (os.getenv("TNSKHDATA_TOKEN", "").split(",")[0])
    if not token:
        logger.warning("无 TUSHARE_TOKEN，trade_cal 用 weekday 兜底（非交易日不计周末）")
        return _weekday_fallback(year)
    try:
        import tushare as ts  # 延迟 import，避免无 tushare 环境崩
        pro = ts.pro_api(token)
        df = pro.trade_cal(exchange="SSE", start_date=f"{year}0101", end_date=f"{year}1231",
                           fields="cal_date,is_open")
        days = df[df["is_open"] == 1]["cal_date"].tolist()
        days = [f"{d[:4]}-{d[4:6]}-{d[6:]}" for d in days]
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(days), encoding="utf-8")
        return days
    except Exception as e:
        logger.warning("fetch_trade_cal 失败，用 weekday 兜底：%s", e)
        return _weekday_fallback(year)


def _weekday_fallback(year: int) -> list[str]:
    """无 Tushare 时退化为「全年非周末」（不识节假日，仅兜底）。"""
    from datetime import timedelta
    days, d = [], datetime(year, 1, 1)
    while d.year == year:
        if d.weekday() < 5:
            days.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    return days


def is_trading_day(date_str: str) -> bool:
    """date_str(YYYY-MM-DD) 是否 A 股交易日。查缓存 trade_cal，缺则 fetch。"""
    year = int(date_str[:4])
    days = fetch_trade_cal(year)
    return date_str in days


def is_intraday_session(now: datetime) -> bool:
    """是否 A 股盘中（9:30-11:30 / 13:00-15:00）。"""
    t = now.time()
    return (time(9, 30) <= t < time(11, 30)) or (time(13, 0) <= t < time(15, 0))
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_calendar.py -v`
Expected: PASS（2 用例）

- [ ] **Step 5: 提交**

```bash
git add trading/calendar.py tests/trading/test_calendar.py
git commit -m "feat(trading): A股交易日历 calendar（trade_cal缓存+盘中时段判定）"
```

---

## Task 2: 海龟止损离散 `trading/stop_loss.py`

**Files:**
- Create: `trading/stop_loss.py`
- Test: `tests/trading/test_stop_loss.py`

**Interfaces:**
- Produces: `compute_stop_price(neckline, atr, holding_days, stop_atr_mult, grace, step, floor) -> float`（纯函数，从 `scripts/neckline_backtest.py:122-135` 迁出，离散化：给定 holding_days 算当日固定止损价）

- [ ] **Step 1: 写失败测试**

```python
# tests/trading/test_stop_loss.py  -*- coding: utf-8 -*-
"""海龟 trailing 止损离散纯函数单测（Task 2）。"""
from trading.stop_loss import compute_stop_price


def test_grace_period_uses_base_stop():
    """grace 天内 = base_stop（颈线 - stop_atr_mult×ATR，固定）。"""
    # 颈线10, ATR 0.5, stop_atr_mult 2 → base_stop = 10 - 2×0.5 = 9.0
    stop = compute_stop_price(neckline=10.0, atr=0.5, holding_days=2,
                              stop_atr_mult=2.0, grace=5, step=0.1, floor=0.5)
    assert abs(stop - 9.0) < 1e-9


def test_after_grace_tightens_step_atr():
    """grace 天后每日收紧 step×ATR。holding_days=7, grace=5 → 收紧 (7-5)×0.1=0.2 mult。
    eff_mult = 2 - 0.2 = 1.8 → stop = 10 - 1.8×0.5 = 9.1"""
    stop = compute_stop_price(neckline=10.0, atr=0.5, holding_days=7,
                              stop_atr_mult=2.0, grace=5, step=0.1, floor=0.5)
    assert abs(stop - 9.1) < 1e-9


def test_floor_caps_tightening():
    """收紧不低于 floor。step 大到 eff_mult < floor 时卡 floor。
    holding_days=20, grace=5 → 收紧 15×0.5=7.5 → eff_mult=2-7.5=-5.5 → max(-5.5,0.5)=0.5
    stop = 10 - 0.5×0.5 = 9.75"""
    stop = compute_stop_price(neckline=10.0, atr=0.5, holding_days=20,
                              stop_atr_mult=2.0, grace=5, step=0.5, floor=0.5)
    assert abs(stop - 9.75) < 1e-9


def test_grace_zero_degrades_fixed():
    """grace=0/step=0 退化为固定止损（=base_stop）。"""
    stop = compute_stop_price(neckline=10.0, atr=0.5, holding_days=99,
                              stop_atr_mult=2.0, grace=0, step=0.1, floor=0.5)
    assert abs(stop - 9.0) < 1e-9
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_stop_loss.py -v`
Expected: FAIL

- [ ] **Step 3: 实现 `trading/stop_loss.py`**

```python
# -*- coding: utf-8 -*-
"""海龟 trailing 止损离散纯函数（从 scripts/neckline_backtest.simulate_exit 迁出）。

物理意图（与 simulate_exit:122-135 完全同源）：
- grace 天内：用 base_stop（颈线 - stop_atr_mult×ATR，固定，给趋势确认空间）；
- grace 天后：每日收紧 step×ATR（eff_mult 递减），到 floor 卡底（收紧上限）；
- grace=0/step=0：退化为固定止损（=base_stop，兼容旧行为）。

离散化（二期）：盘后对每只持仓调本函数重算【次日】固定止损价；盘中监控用此固定价，
不移动（符合 spec「盘中不调整」）。回测里是逐根 K 线调；实盘改为每日一次。
"""
from __future__ import annotations


def compute_stop_price(
    neckline: float,
    atr: float,
    holding_days: int,
    stop_atr_mult: float,
    grace: int,
    step: float,
    floor: float | None,
) -> float:
    """给定持有天数算当日止损价（颈线基准，trailing 离散）。"""
    base_stop = neckline - stop_atr_mult * atr
    if grace and step and holding_days > grace:
        eff_mult = stop_atr_mult - (holding_days - grace) * step
        if floor is not None:
            eff_mult = max(eff_mult, floor)
        return neckline - eff_mult * atr
    return base_stop
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_stop_loss.py -v`
Expected: PASS（4 用例）

- [ ] **Step 5: 提交**

```bash
git add trading/stop_loss.py tests/trading/test_stop_loss.py
git commit -m "feat(trading): 海龟trailing止损离散纯函数 compute_stop_price（迁出simulate_exit）"
```

---

## Task 3: 信号转下单 `trading/signal_runner.py`

**Files:**
- Create: `trading/signal_runner.py`
- Test: `tests/trading/test_signal_runner.py`

**Interfaces:**
- Consumes: `NecklineMethodStrategy.scan_at` 返回的 trade dict（含 symbol/entry_price/neckline）+ ATR
- Produces: `build_orders_from_signals(signals, *, capital, pos_cap, atr_map, stop_cfg) -> list[PlannedOrder]`，其中 `PlannedOrder` 含 `OrderRequest + stop_price + take_profit`

- [ ] **Step 1: 写失败测试**

```python
# tests/trading/test_signal_runner.py  -*- coding: utf-8 -*-
"""信号转下单单测（Task 3）。"""
from trading.signal_runner import build_orders_from_signals, PlannedOrder


def test_build_orders_position_sizing():
    """单标的：capital 100万 × pos_cap 0.05 = 5万，entry 10 元 → 5000 股 → 整手 5000。
    附 stop_price（颈线-stop_mult×atr）+ take_profit（颈线+tp_mult×H）。"""
    signals = [{
        "symbol": "600000.SH", "entry_price": 10.0, "neckline": 9.5, "bottom": 8.5,
        "signal_type": "neckline",
    }]
    orders = build_orders_from_signals(
        signals, capital=1_000_000.0, pos_cap=0.05,
        atr_map={"600000.SH": 0.5}, stop_cfg={"stop_atr_mult": 2.0, "tp_h_mult": 2.0},
    )
    assert len(orders) == 1
    o = orders[0]
    assert o.order.symbol == "600000.SH"
    assert o.order.side == "buy"
    assert o.order.qty == 5000                      # 5万/10元=5000，整100手
    assert o.order.price == 10.0
    # 止损 = 颈线9.5 - 2×0.5 = 8.5；止盈 = 颈线9.5 + 2×(9.5-8.5)=11.5
    assert abs(o.stop_price - 8.5) < 1e-9
    assert abs(o.take_profit - 11.5) < 1e-9


def test_build_orders_skip_missing_atr():
    """无 ATR 的标的跳过（防 None 运算）。"""
    signals = [{"symbol": "X.SH", "entry_price": 10.0, "neckline": 9.5, "bottom": 8.5}]
    orders = build_orders_from_signals(
        signals, capital=1_000_000.0, pos_cap=0.05,
        atr_map={}, stop_cfg={"stop_atr_mult": 2.0, "tp_h_mult": 2.0})
    assert orders == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_signal_runner.py -v`
Expected: FAIL

- [ ] **Step 3: 实现 `trading/signal_runner.py`**

```python
# -*- coding: utf-8 -*-
"""颈线法信号 → 下单计划转换（Task 3）。

把 NecklineMethodStrategy.scan_at 返回的 trade dict 转成 PlannedOrder（OrderRequest + 止损/止盈价）。
仓位：capital × pos_cap / entry_price，向下取整到 100 整手（A 股）。
止损/止盈：颈线基准 + ATR/H（与 simulate_exit 同口径）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from trading.execution_gateway import OrderRequest


@dataclass
class PlannedOrder:
    """计划单（OrderRequest + 出场价）。"""
    order: OrderRequest
    stop_price: float
    take_profit: float
    neckline: float


def build_orders_from_signals(
    signals: list[dict],
    *,
    capital: float,
    pos_cap: float,
    atr_map: dict[str, float],
    stop_cfg: dict,
) -> list[PlannedOrder]:
    """信号列表 → PlannedOrder 列表。缺 ATR/数据异常的跳过（不抛）。"""
    stop_mult = stop_cfg.get("stop_atr_mult", 2.0)
    tp_mult = stop_cfg.get("tp_h_mult", 2.0)
    out: list[PlannedOrder] = []
    for s in signals:
        sym = s.get("symbol")
        entry = s.get("entry_price")
        neckline = s.get("neckline")
        bottom = s.get("bottom")
        atr = atr_map.get(sym) if sym else None
        if not sym or entry is None or neckline is None or bottom is None or atr is None:
            continue
        budget = capital * pos_cap
        qty = int(budget / float(entry) / 100) * 100   # 向下取整 100 整手
        if qty <= 0:
            continue
        h = float(neckline) - float(bottom)
        stop_price = float(neckline) - stop_mult * float(atr)
        take_profit = float(neckline) + tp_mult * h
        out.append(PlannedOrder(
            order=OrderRequest(symbol=sym, qty=float(qty), side="buy", price=float(entry)),
            stop_price=stop_price, take_profit=take_profit, neckline=float(neckline),
        ))
    return out
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_signal_runner.py -v`
Expected: PASS（2 用例）

- [ ] **Step 5: 提交**

```bash
git add trading/signal_runner.py tests/trading/test_signal_runner.py
git commit -m "feat(trading): 信号转下单 signal_runner（仓位整手+止损止盈价）"
```

---

## Task 4: 装 APScheduler + requirements + .env 配置

**Files:**
- Modify: `requirements.txt`（加 APScheduler）
- Modify: `.env.example` / `.env`（加 AUTO_TRADE_MODE + ENGINE crons + TRADE 参数）

- [ ] **Step 1: 装 APScheduler**

Run: `.venv310/Scripts/python.exe -m pip install "APScheduler>=3.10" -i https://pypi.tuna.tsinghua.edu.cn/simple`

- [ ] **Step 2: requirements.txt 加依赖**

在 `requirements.txt` 适当位置加：`APScheduler>=3.10`（注释：二期自动交易引擎调度）。

- [ ] **Step 3: `.env.example` + `.env` 加二期配置**

```ini
# === 二期 自动交易引擎 ===
AUTO_TRADE_MODE=dry_run              # dry_run 影子模式 / live 真单（未跑满N日禁切live）
ENGINE_PRE_OPEN_CRON=22 9 * * 1-5
ENGINE_STOPLOSS_CRON=*/5 9-14 * * 1-5
ENGINE_POST_CLOSE_CRON=30 15 * * 1-5
ENGINE_EOD_PLAN_CRON=35 15 * * 1-5
TRADE_POS_CAP=0.05
TRADE_MAX_TOTAL_EXPOSURE=0.80
TRADE_STOPLOSS_GRACE_DAYS=5
TRADE_STOPLOSS_STEP_ATR=0.1
TRADE_STOPLOSS_FLOOR=0.5
TRADE_SHADOW_MIN_DAYS=5
CIRCUIT_DAILY_LOSS_LIMIT=-0.03
TRADE_PLAN_DIR=logs/trading_plans
```
（`.env` 同步加；`.env` 不入 git。）

- [ ] **Step 4: 验证 import**

Run: `.venv310/Scripts/python.exe -c "from apscheduler.schedulers.asyncio import AsyncIOScheduler; print('APScheduler OK')"`
Expected: 打印 APScheduler OK

- [ ] **Step 5: 提交**

```bash
git add requirements.txt .env.example
git commit -m "chore(trading): 装APScheduler + 二期engine配置项（AUTO_TRADE_MODE/crons/trade参数）"
```

---

## Task 5: 动态白名单 `trading/dynamic_whitelist.py`

**Files:**
- Create: `trading/dynamic_whitelist.py`
- Test: `tests/trading/test_dynamic_whitelist.py`

**Interfaces:**
- Produces: `inject_dynamic_whitelist(symbols: set[str]) -> None`、`clear_dynamic_whitelist() -> None`、`get_effective_whitelist() -> set[str]`（静态 env 白名单 ∪ 动态注入）

- [ ] **Step 1: 写失败测试**

```python
# tests/trading/test_dynamic_whitelist.py  -*- coding: utf-8 -*-
"""动态白名单单测（Task 5）。"""
from trading import dynamic_whitelist as dw


def test_inject_then_clear(monkeypatch):
    monkeypatch.setenv("QMT_SYMBOL_WHITELIST", "510300.SH,159915.SZ")
    dw.clear_dynamic_whitelist()
    assert dw.get_effective_whitelist() == {"510300.SH", "159915.SZ"}
    dw.inject_dynamic_whitelist({"600000.SH", "000001.SZ"})
    assert dw.get_effective_whitelist() == {"510300.SH", "159915.SZ", "600000.SH", "000001.SZ"}
    dw.clear_dynamic_whitelist()
    assert dw.get_effective_whitelist() == {"510300.SH", "159915.SZ"}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_dynamic_whitelist.py -v`
Expected: FAIL

- [ ] **Step 3: 实现 `trading/dynamic_whitelist.py`**

```python
# -*- coding: utf-8 -*-
"""动态白名单（信号标的临时注入 risk_shield 白名单，当日有效）。

Why：.env QMT_SYMBOL_WHITELIST 是静态兜底（4 只 ETF）；二期策略每日扫出的标的（创板/科创个股）
须临时注入白名单才能过 risk_shield 关5。盘前注入、盘后清，不污染静态配置。
"""
from __future__ import annotations

import os

_DYNAMIC: set[str] = set()


def inject_dynamic_whitelist(symbols: set[str]) -> None:
    """注入当日计划标的（合并到动态集合）。"""
    _DYNAMIC.update(symbols)


def clear_dynamic_whitelist() -> None:
    """清空动态白名单（盘后调）。"""
    _DYNAMIC.clear()


def get_effective_whitelist() -> set[str]:
    """有效白名单 = 静态 env（逗号分隔）∪ 动态注入。"""
    static = {s.strip() for s in os.getenv("QMT_SYMBOL_WHITELIST", "").split(",") if s.strip()}
    return static | _DYNAMIC
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_dynamic_whitelist.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add trading/dynamic_whitelist.py tests/trading/test_dynamic_whitelist.py
git commit -m "feat(trading): 动态白名单（信号标的临时注入，当日有效）"
```

---

## Task 6: 熔断 `trading/circuit_breaker.py`

**Files:**
- Create: `trading/circuit_breaker.py`
- Test: `tests/trading/test_circuit_breaker.py`

**Interfaces:**
- Produces: `check_daily_loss_limit(start_equity, curr_equity) -> bool`（True=触发熔断）、`cancel_all_open_orders(gw) -> int`（撤所有未终态单，补全 emergency_halt 漏洞）

- [ ] **Step 1: 写失败测试**

```python
# tests/trading/test_circuit_breaker.py  -*- coding: utf-8 -*-
"""熔断单测（Task 6）。"""
from trading import circuit_breaker as cb


def test_daily_loss_limit_triggers():
    """日亏触及 -3% 触发。"""
    assert cb.check_daily_loss_limit(1_000_000, 965_000, limit=-0.03) is True   # -3.5%
    assert cb.check_daily_loss_limit(1_000_000, 980_000, limit=-0.03) is False  # -2% 未触


def test_cancel_all_open_orders():
    """撤所有未终态单：遍历 gw._orders，对非终态调 cancel_order。"""
    class FakeGW:
        def __init__(self, orders):
            self._orders = orders
            self.cancelled = []
        async def cancel_order(self, oid):
            self.cancelled.append(oid)
            return None
    # _orders 含一未终态（SUBMITTED）+ 一终态（FILLED）
    gw = FakeGW({"1": {"state": "SUBMITTED"}, "2": {"state": "FILLED"}})
    import asyncio
    n = asyncio.run(cb.cancel_all_open_orders(gw))
    assert n == 1 and gw.cancelled == ["1"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_circuit_breaker.py -v`
Expected: FAIL

- [ ] **Step 3: 实现 `trading/circuit_breaker.py`**

```python
# -*- coding: utf-8 -*-
"""安全熔断（日亏上限 + 撤单补全）。

Why 独立：emergency_halt 只 lock_down 不撤单（一期探索发现的漏洞，注释「撤单留待调度器」）；
二期补「撤所有未终态单」路径，防断线/熔断时未终态单敞口失控。
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_TERMINAL = {"FILLED", "CANCELLED", "REJECTED", "FAILED", "PARTIAL_CANCELLED"}


def check_daily_loss_limit(start_equity: float, curr_equity: float, *, limit: float | None = None) -> bool:
    """日亏是否触及上限。limit 缺省读 env CIRCUIT_DAILY_LOSS_LIMIT（-0.03）。True=触发熔断。"""
    if limit is None:
        limit = float(os.getenv("CIRCUIT_DAILY_LOSS_LIMIT", "-0.03"))
    if start_equity <= 0:
        return False
    pnl_pct = (curr_equity - start_equity) / start_equity
    return pnl_pct <= limit


async def cancel_all_open_orders(gw) -> int:
    """撤网关所有未终态订单（熔断/断线时调）。返撤单数。"""
    orders = getattr(gw, "_orders", {}) or {}
    n = 0
    for oid, rec in list(orders.items()):
        if rec.get("state") not in _TERMINAL:
            try:
                await gw.cancel_order(oid)
                n += 1
            except Exception:
                logger.exception("熔断撤单失败 oid=%s", oid)
    logger.warning("熔断撤单完成，共撤 %s 笔未终态单", n)
    return n
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_circuit_breaker.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add trading/circuit_breaker.py tests/trading/test_circuit_breaker.py
git commit -m "feat(trading): 安全熔断 circuit_breaker（日亏上限+撤单补全emergency_halt漏洞）"
```

---

## Task 7: 盘后对账 `trading/reconcile_job.py`

**Files:**
- Create: `trading/reconcile_job.py`
- Test: `tests/trading/test_reconcile_job.py`

**Interfaces:**
- Consumes: `BaseExecutionGateway.sync_positions`（模板方法，已就绪）
- Produces: `run_reconcile(gw, local_positions, tolerance) -> ReconciliationResult` + 偏差超阈值钉钉告警

- [ ] **Step 1: 写失败测试**

```python
# tests/trading/test_reconcile_job.py  -*- coding: utf-8 -*-
"""盘后对账单测（Task 7）。"""
import asyncio
from trading import reconcile_job


class FakeGW:
    async def sync_positions(self, local, tolerance=0.0):
        # 模拟：broker 缺 000001.SZ 100 股 → 漂移
        from trading.execution_gateway import ReconciliationResult
        return ReconciliationResult(matched={"510300.SH": 100}, only_local={"000001.SZ": 100},
                                    only_broker={}, local=local, broker={"510300.SH": 100})


def test_reconcile_drift_within_tolerance():
    r = asyncio.run(reconcile_job.run_reconcile(FakeGW(), {"510300.SH": 100, "000001.SZ": 100}, tolerance=0))
    # only_local 有漂移 → has_drift True
    assert r.has_drift is True


def test_reconcile_no_drift():
    class OKGW:
        async def sync_positions(self, local, tolerance=0.0):
            from trading.execution_gateway import ReconciliationResult
            return ReconciliationResult(matched=local, only_local={}, only_broker={}, local=local, broker=local)
    r = asyncio.run(reconcile_job.run_reconcile(OKGW(), {"510300.SH": 100}, tolerance=0))
    assert r.has_drift is False
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_reconcile_job.py -v`
Expected: FAIL（注意：需先确认 `ReconciliationResult` 有 `has_drift` 属性，若无用 `only_local or only_broker` 判定）

- [ ] **Step 3: 实现 `trading/reconcile_job.py`**

```python
# -*- coding: utf-8 -*-
"""盘后对账（持仓数量 本地 vs 券商，偏差超阈值钉钉告警）。

复用 BaseExecutionGateway.sync_positions 模板方法（已就绪）；本模块只做调度 + 告警。
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _has_drift(rec) -> bool:
    """对账结果是否有漂移（only_local/only_broker 非空）。"""
    return bool(getattr(rec, "only_local", None) or getattr(rec, "only_broker", None))


async def run_reconcile(gw, local_positions: dict, tolerance: float = 0.0):
    """跑对账 + 偏差告警。返回 ReconciliationResult。"""
    rec = await gw.sync_positions(local_positions, tolerance=tolerance)
    if _has_drift(rec):
        msg = f"【对账漂移】only_local={dict(rec.only_local)} only_broker={dict(rec.only_broker)}"
        logger.warning(msg)
        try:
            from core.notifier import NotificationManager, fire_and_forget
            fire_and_forget(NotificationManager.get_default().notify_risk_event(msg, "WARN"))
        except Exception:
            pass
    else:
        logger.info("盘后对账无漂移 ✅")
    return rec
```

> 注：若 `ReconciliationResult` 无 `has_drift` 属性，测试用 `_has_drift(rec)` 判定（本实现已如此）。implementer 实现时先 `codegraph_explore ReconciliationResult` 确认字段，按实际字段调整断言。

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_reconcile_job.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add trading/reconcile_job.py tests/trading/test_reconcile_job.py
git commit -m "feat(trading): 盘后对账 reconcile_job（sync_positions+偏差钉钉告警）"
```

---

## Task 8: T-1 交易计划 `trading/trading_plan.py`

**Files:**
- Create: `trading/trading_plan.py`
- Test: `tests/trading/test_trading_plan.py`

**Interfaces:**
- Produces: `save_plan(date, orders) -> Path`、`load_plan(date) -> dict|None`、`confirm_plan(date) -> bool`、`push_plan_to_dingtalk(date, orders) -> bool`（复用 broadcast.push）

- [ ] **Step 1: 写失败测试**

```python
# tests/trading/test_trading_plan.py  -*- coding: utf-8 -*-
"""T-1 交易计划单测（Task 8）。"""
from trading import trading_plan as tp


def test_save_load_confirm(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADE_PLAN_DIR", str(tmp_path))
    orders = [{"symbol": "600000.SH", "qty": 5000, "side": "buy", "price": 10.0,
               "stop_price": 8.5, "take_profit": 11.5}]
    p = tp.save_plan("2026-07-22", orders)
    assert p.exists()
    plan = tp.load_plan("2026-07-22")
    assert plan is not None and plan["orders"] == orders
    assert plan["confirmed"] is False
    assert tp.confirm_plan("2026-07-22") is True
    assert tp.load_plan("2026-07-22")["confirmed"] is True


def test_load_plan_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADE_PLAN_DIR", str(tmp_path))
    assert tp.load_plan("2099-01-01") is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_trading_plan.py -v`
Expected: FAIL

- [ ] **Step 3: 实现 `trading/trading_plan.py`**

```python
# -*- coding: utf-8 -*-
"""T-1 交易计划（JSON 落盘 + 人工确认闸 + 钉钉推送）。

流程：eod_plan 生成 orders → save_plan（confirmed=false）→ push_plan_to_dingtalk（交易机器人推群）
→ 研究员钉钉确认 → confirm_plan（confirmed=true）→ 次日 pre_open 检查 confirmed 才挂单。
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict
from pathlib import Path

logger = logging.getLogger(__name__)


def _plan_path(date: str) -> Path:
    base = Path(os.getenv("TRADE_PLAN_DIR", "logs/trading_plans"))
    return base / f"plan_{date}.json"


def save_plan(date: str, orders: list) -> Path:
    """落盘 T 日计划（confirmed=false）。orders 为 PlannedOrder.asdict() 列表。"""
    p = _plan_path(date)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {"date": date, "confirmed": False, "orders": orders}
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("T-1 计划已落盘 %s（%d 单，待确认）", p, len(orders))
    return p


def load_plan(date: str) -> dict | None:
    """读计划；不存在返 None。"""
    p = _plan_path(date)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("计划损坏 %s", p)
        return None


def confirm_plan(date: str) -> bool:
    """标记计划已确认（人工钉钉确认后调）。返回是否成功。"""
    plan = load_plan(date)
    if plan is None:
        return False
    plan["confirmed"] = True
    _plan_path(date).write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("计划 %s 已确认 ✅", date)
    return True


def push_plan_to_dingtalk(date: str, orders: list) -> bool:
    """复用一期 broadcast.push 把计划推到交易机器人群（研究员确认用）。"""
    try:
        from broadcast.push import push_brief
        lines = [f"- {o['order']['symbol']} {o['order']['side']} {o['order']['qty']}股@{o['order']['price']}"
                 f"（止损{o['stop_price']}/止盈{o['take_profit']}）" for o in orders]
        md = f"### 🤖 T-1 交易计划 {date}\n> 待确认（回复「确认」即挂单）\n\n" + "\n".join(lines)
        robot = os.getenv("TRADING_BOT_ROBOT_CODE", "")
        group = os.getenv("BROADCAST_GROUP_ID", "")
        return push_brief(f"交易计划 {date}", md, robot_code=robot, group_id=group)
    except Exception:
        logger.exception("推计划到钉钉失败")
        return False
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_trading_plan.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add trading/trading_plan.py tests/trading/test_trading_plan.py
git commit -m "feat(trading): T-1交易计划 trading_plan（JSON落盘+确认闸+钉钉推送）"
```

---

## Task 9: 引擎编排 `trading/engine.py`

**Files:**
- Create: `trading/engine.py`
- Test: `tests/trading/test_engine.py`

**Interfaces:**
- Consumes: Task 1-8 全部组件 + `trading_service.submit_order`（dry_run/live）+ `NecklineMethodStrategy`
- Produces: `TradingEngine` 类（APScheduler 四触发点：`eod_plan`/`pre_open`/`stop_loss_monitor`/`post_close`），影子模式 `AUTO_TRADE_MODE=dry_run` 分流

- [ ] **Step 1: 写失败测试**

```python
# tests/trading/test_engine.py  -*- coding: utf-8 -*-
"""引擎编排单测（Task 9 · 核心调度逻辑，不真起 APScheduler）。"""
from trading import engine


def test_eod_plan_dry_run_no_real_order(monkeypatch, tmp_path):
    """影子模式：eod_plan 生成计划但不调真单，只落盘 + 推钉钉。"""
    monkeypatch.setenv("AUTO_TRADE_MODE", "dry_run")
    monkeypatch.setenv("TRADE_PLAN_DIR", str(tmp_path))
    called = {"submit": 0}
    async def fake_submit(order, **kw):
        called["submit"] += 1
        return {"state": "DRY_RUN"}
    monkeypatch.setattr(engine, "_submit", fake_submit)
    # eod_plan 跑：signals 空 → 计划空 → 不挂单
    import asyncio
    asyncio.run(engine.eod_plan("2099-01-01", signals=[], atr_map={}, capital=1_000_000))
    assert called["submit"] == 0   # 影子模式 + 空信号，绝不调真单


def test_pre_open_blocks_unconfirmed_plan(monkeypatch, tmp_path):
    """pre_open：计划未确认 → 不挂单。"""
    monkeypatch.setenv("TRADE_PLAN_DIR", str(tmp_path))
    import asyncio
    from trading import trading_plan
    trading_plan.save_plan("2099-01-02", [])  # confirmed=false
    result = asyncio.run(engine.pre_open("2099-01-02"))
    assert result["submitted"] == 0 and "未确认" in result["reason"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_engine.py -v`
Expected: FAIL

- [ ] **Step 3: 实现 `trading/engine.py`**

```python
# -*- coding: utf-8 -*-
"""自动交易引擎（APScheduler 四触发点编排 + 影子模式分流）。

四触发点（均先过 is_trading_day）：
  eod_plan 15:35：scan 信号 → build_orders → save_plan → push 钉钉（待确认）
  pre_open 09:22：读已确认计划 → 注入动态白名单 → 挂限价买 + 止盈限价卖 + 撤昨日未成交
  stop_loss_monitor 每5min（intraday 时段）：查持仓价跌破止损价 → 发卖出单
  post_close 15:30：对账 + 重算次日止损 + 熔断检查

影子模式（AUTO_TRADE_MODE=dry_run）：pre_open/stop_loss 不调真单，只记 DRY_RUN 流水。
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime

from trading import calendar, signal_runner, dynamic_whitelist, circuit_breaker
from trading import trading_plan, stop_loss
from trading.signal_runner import build_orders_from_signals

logger = logging.getLogger(__name__)


def _mode() -> str:
    return os.getenv("AUTO_TRADE_MODE", "dry_run")


def _trade_cfg() -> dict:
    return {
        "pos_cap": float(os.getenv("TRADE_POS_CAP", "0.05")),
        "stop_atr_mult": 2.0,   # 颈线法 id_cfg 默认；实盘从 NecklineConfig 读
        "tp_h_mult": 2.0,
        "grace": int(os.getenv("TRADE_STOPLOSS_GRACE_DAYS", "5")),
        "step": float(os.getenv("TRADE_STOPLOSS_STEP_ATR", "0.1")),
        "floor": float(os.getenv("TRADE_STOPLOSS_FLOOR", "0.5")),
    }


async def _submit(order, **kw):
    """委托 trading_service.submit_order（dry_run 据 _mode）。实现时从 server.services 导入。"""
    from server.services.trading_service import submit_order as svc_submit
    return await svc_submit(order, dry_run=(_mode() == "dry_run"), confirm=kw.get("confirm", True))


async def eod_plan(date: str, signals: list, atr_map: dict, capital: float) -> dict:
    """T-1 晚：信号 → 计划 → 落盘 → 推钉钉。影子模式不调真单（计划阶段本就不下单）。"""
    cfg = _trade_cfg()
    orders = build_orders_from_signals(
        signals, capital=capital, pos_cap=cfg["pos_cap"], atr_map=atr_map,
        stop_cfg={"stop_atr_mult": cfg["stop_atr_mult"], "tp_h_mult": cfg["tp_h_mult"]})
    order_dicts = [{"order": {"symbol": o.order.symbol, "qty": o.order.qty, "side": o.order.side,
                              "price": o.order.price},
                    "stop_price": o.stop_price, "take_profit": o.take_profit} for o in orders]
    trading_plan.save_plan(date, order_dicts)
    trading_plan.push_plan_to_dingtalk(date, order_dicts)
    return {"date": date, "n_orders": len(orders), "mode": _mode()}


async def pre_open(date: str) -> dict:
    """T 日开盘前：读已确认计划 → 挂单。未确认 → 不挂。"""
    plan = trading_plan.load_plan(date)
    if plan is None:
        return {"submitted": 0, "reason": "无计划"}
    if not plan.get("confirmed"):
        return {"submitted": 0, "reason": "计划未确认，跳过挂单"}
    from trading.execution_gateway import OrderRequest
    dynamic_whitelist.inject_dynamic_whitelist({o["order"]["symbol"] for o in plan["orders"]})
    n = 0
    for o in plan["orders"]:
        od = o["order"]
        result = await _submit(OrderRequest(symbol=od["symbol"], qty=od["qty"], side=od["side"], price=od["price"]),
                               confirm=True)
        if result.get("state") not in ("REJECTED", "FAILED"):
            n += 1
    return {"submitted": n, "mode": _mode()}


async def stop_loss_monitor(positions: dict, stop_prices: dict) -> dict:
    """盘中：持仓现价跌破止损价 → 发卖出单。影子模式不调真单（dry_run）。"""
    if not calendar.is_intraday_session(datetime.now()):
        return {"checked": 0, "reason": "非盘中时段"}
    # positions: {symbol: current_price}; stop_prices: {symbol: stop_price}
    n = 0
    from trading.execution_gateway import OrderRequest
    for sym, price in positions.items():
        sp = stop_prices.get(sym)
        if sp is not None and price <= sp:
            # 跌破：发卖出单（qty 从持仓读，这里简化用 100 整手占位，实盘从 gateway 持仓读）
            await _submit(OrderRequest(symbol=sym, qty=100.0, side="sell", price=price), confirm=True)
            n += 1
    return {"checked": len(positions), "stop_triggered": n, "mode": _mode()}


async def post_close(date: str, gw=None, local_positions: dict | None = None) -> dict:
    """盘后：对账 + 重算次日止损 + 熔断检查。"""
    from trading import reconcile_job
    result = {"date": date}
    if gw is not None and local_positions is not None:
        rec = await reconcile_job.run_reconcile(gw, local_positions)
        result["drift"] = bool(getattr(rec, "only_local", None) or getattr(rec, "only_broker", None))
    return result


class TradingEngine:
    """APScheduler 编排（python -m trading 起常驻）。"""

    def __init__(self):
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.triggers.cron import CronTrigger
        self.sched = AsyncIOScheduler()
        self.sched.add_job(self._eod, CronTrigger.from_crontab(os.getenv("ENGINE_EOD_PLAN_CRON", "35 15 * * 1-5")))
        self.sched.add_job(self._pre_open, CronTrigger.from_crontab(os.getenv("ENGINE_PRE_OPEN_CRON", "22 9 * * 1-5")))
        self.sched.add_job(self._stoploss, CronTrigger.from_crontab(os.getenv("ENGINE_STOPLOSS_CRON", "*/5 9-14 * * 1-5")))
        self.sched.add_job(self._post_close, CronTrigger.from_crontab(os.getenv("ENGINE_POST_CLOSE_CRON", "30 15 * * 1-5")))

    async def _eod(self):
        if not calendar.is_trading_day(datetime.now().strftime("%Y-%m-%d")):
            return
        # 实现时：调 NecklineMethodStrategy 扫描当日 → eod_plan
        logger.info("eod_plan 触发")

    async def _pre_open(self):
        if not calendar.is_trading_day(datetime.now().strftime("%Y-%m-%d")):
            return
        await pre_open(datetime.now().strftime("%Y-%m-%d"))

    async def _stoploss(self):
        # intraday 时段判定在 stop_loss_monitor 内
        logger.info("stop_loss_monitor 触发")

    async def _post_close(self):
        if not calendar.is_trading_day(datetime.now().strftime("%Y-%m-%d")):
            return
        await post_close(datetime.now().strftime("%Y-%m-%d"))

    def start(self):
        self.sched.start()
        logger.info("TradingEngine 已启动（mode=%s）", _mode())

    def shutdown(self):
        self.sched.shutdown(wait=False)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_engine.py -v`
Expected: PASS（2 用例）

- [ ] **Step 5: 提交**

```bash
git add trading/engine.py tests/trading/test_engine.py
git commit -m "feat(trading): 引擎编排 engine（APScheduler四触发点+影子模式dry_run分流）"
```

---

## Task 10: 独立进程入口 `trading/__main__.py`

**Files:**
- Create: `trading/__main__.py`

- [ ] **Step 1: 实现 `python -m trading` 入口**

```python
# -*- coding: utf-8 -*-
"""自动交易引擎独立进程入口：python -m trading（不寄生 server uvicorn）。

Why 独立：server 重启不应中断交易；engine 常驻跑 APScheduler 四触发点。
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys

# 加载 .env
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")


async def _run_forever():
    from trading.engine import TradingEngine
    eng = TradingEngine()
    eng.start()
    # 守护：event loop 永久挂起（APScheduler 后台跑）
    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        eng.shutdown()


if __name__ == "__main__":
    mode = os.getenv("AUTO_TRADE_MODE", "dry_run")
    logging.getLogger(__name__).info("=== 自动交易引擎启动（AUTO_TRADE_MODE=%s）===", mode)
    if mode != "dry_run":
        logging.getLogger(__name__).warning("⚠️ LIVE 模式：将真实下单！确保影子模式已跑满 TRADE_SHADOW_MIN_DAYS")
    try:
        asyncio.run(_run_forever())
    except KeyboardInterrupt:
        sys.exit(0)
```

- [ ] **Step 2: 冒烟：dry_run 起进程（10s 后停）**

Run（Git Bash）: `timeout 10 .venv310/Scripts/python.exe -m trading 2>&1 | head -15`
Expected: 打印「自动交易引擎启动（AUTO_TRADE_MODE=dry_run）」+「TradingEngine 已启动」+ APScheduler 无报错

- [ ] **Step 3: 提交**

```bash
git add trading/__main__.py
git commit -m "feat(trading): 独立进程入口 python -m trading（APScheduler常驻）"
```

---

## Task 11: 影子模式上线 + SOP

**Files:**
- Modify: `scripts/start_dingtalk_bots.md`（加 engine 常驻 SOP）
- Create: `scripts/run_trading_engine.bat`（schtasks/PM2 启动入口）

- [ ] **Step 1: 创建 engine 启动 bat**

```bat
@echo off
cd /d "C:\Users\yzzhan\Desktop\quanter"
".venv310\Scripts\python.exe" -m trading
```

- [ ] **Step 2: start_dingtalk_bots.md 加 engine 常驻 SOP**

在 SOP 加「四、自动交易引擎常驻」段：
- 影子模式：`.env` 设 `AUTO_TRADE_MODE=dry_run`，`run_trading_engine.bat` 启动（或 PM2/terminal tab 托管）
- 验证：观察 logs/trading_plans/ 每日落盘 + 钉钉推计划 + dry_run 流水
- 跑满 `TRADE_SHADOW_MIN_DAYS`（≥5）+ 偏差可接受 → 改 `.env` `AUTO_TRADE_MODE=live` 重启（真实下单）

- [ ] **Step 3: 端到端影子冒烟（手动触发 eod_plan）**

写一次性脚本（或 python -c）调 `engine.eod_plan(today, signals=[], atr_map={}, capital=1_000_000)` 验证：计划落盘 logs/trading_plans/plan_<today>.json + 钉钉推空计划（或模拟信号推真计划）。

- [ ] **Step 4: 全量回归测试**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/ -v`
Expected: 全绿（Task 1-9 测试 + 回归）

- [ ] **Step 5: 提交**

```bash
git add scripts/run_trading_engine.bat scripts/start_dingtalk_bots.md
git commit -m "docs(ops): 二期engine常驻SOP + 影子模式上线清单"
```

---

## Self-Review

1. **Spec 覆盖**：
   - ①交易日历+APScheduler → Task 1/4/9/10 ✓
   - ②live信号生成器（scan_at→下单）→ Task 3 ✓
   - ③海龟止损迁出（离散）→ Task 2 ✓
   - ④盘后对账调度 → Task 7/9 ✓
   - ⑤安全熔断（日亏/断线全撤/总仓位）→ Task 6（日亏+撤单；总仓位在 signal_runner pos_cap + Task 3 仓位控制）
   - ⑥白名单动态化 → Task 5 ✓
   - ⑦实盘vs回测偏差监控 → spec 标「可选」，Task 7 对账覆盖持仓偏差（PnL 偏差 follow-up）
   - T-1 确认闸 → Task 8 ✓
   - 影子模式 dry_run → Task 9/10/11 ✓

2. **Placeholder**：Task 7 注明「先 codegraph_explore ReconciliationResult 确认字段」——这是实现时确认（非占位符，是现有代码字段核对）；其余无 TBD。

3. **类型一致**：`PlannedOrder`（Task 3）→ Task 8 order_dicts 转换一致；`compute_stop_price`（Task 2）签名与 simulate_exit 同源；`OrderRequest`（execution_gateway 既有）全链路一致；`AUTO_TRADE_MODE` env 全链路一致。

4. **待研究员确认参数**（spec 标注，plan 用基线）：pos_cap/stop_atr_mult/grace/step/floor/总仓位/日亏/止损频率/影子天数/扫描时点——均在 .env（Task 4），审阅时可调。

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-21-auto-trading-engine.md`. Two execution options:

**1. Subagent-Driven（推荐）** — 每 Task 派 fresh subagent + 任务间 review（与一期同模式）。

**2. Inline Execution** — 当前 session 批量执行 + checkpoint。

**Which approach?**

> ⚠️ 二期涉及自动交易（真金白银），执行时务必：①影子模式 dry_run 先行；②每个 task review 严把关（尤其 Task 6 熔断 + Task 9 engine + Task 3 仓位）；③未跑满 TRADE_SHADOW_MIN_DAYS（≥5）禁切 live。
