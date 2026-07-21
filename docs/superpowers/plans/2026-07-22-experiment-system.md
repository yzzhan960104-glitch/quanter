# 实验系统（Experiment System）Implementation Plan · v2

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实盘下单的「策略版本配置中心」+ 二期引擎 `trading/engine.py::_eod` 的策略数据源注入层（gap②）：resolve 在线实验版本+权重 → T-1 晚 `_eod` 遍历产信号（带归因）→ signal_runner/trading_plan 透传归因。

**Architecture:** 新增独立 `experiment/` 包（SQLite 配置中心，零反向依赖）→ 注入 `trading/engine.py::_eod`（替换 `signals=[]` 占位）→ `signal_runner` 资金按 weight 分配 + 归因透传 → `trading_plan` orders 嵌套 dict 带归因落盘。出场复用二期 `stop_loss.compute_stop_price` + 柜台限价止盈（不另造 check_exit）。

**Tech Stack:** Python 3.10（`.venv310/Scripts/python`，xtquant 绑 python310）/ SQLite3 标准库（WAL）/ dataclasses / argparse / pytest / APScheduler（二期引擎既有）。

## v2 修订说明（2026-07-22）

v1 误基于旧 `execution/engine.py`（盘中 tick ARMED→FILLED→CLOSED 范式）。master 合并二期引擎 `trading/`（`04f6c1c`，T-1 定计划 + APScheduler 四触发点）后，v1 的 Task 5/7/8/10（Strategy 协议 check_exit / 颈线法 check_exit / caisen 适配器 / execution engine 改造）**全部作废**。v2 保留 Task 1-4 配置中心（完整代码见 git `dec2253` 的 v1），重写执行侧 Task 5-9 对接二期引擎。

## Global Constraints

- **Python 环境**：miniQMT 用 `.venv310/Scripts/python`；`experiment/` 核心纯标准库
- **零新依赖**：SQLite3/dataclasses/argparse 标准库
- **二期引擎范式**：T-1 `_eod` 扫信号 → `trading_plan` 落盘 → 人工确认 → `pre_open` 挂限价 → 盘中 `stop_loss_monitor` → `post_close` 对账。**不引入盘中 check_exit/tp1-tp2 分级**
- **出场归二期**：止盈=柜台限价卖单（pre_open），止损=`stop_loss.compute_stop_price`（已迁出 simulate_exit）。实验系统不动出场
- **AUTO_TRADE_MODE dry_run 影子先行**：MVP 验收用 dry_run，live 待二期 gap①③ 补全
- **EMT 已废弃**：gateway 只 Mock + miniQMT
- **experiment/ 零反向依赖** strategies/execution/trading/server
- **全中文注释**，TDD，frequent commits，权重和 ≤ 1.0 资金守恒

---

## File Structure

**新建 `experiment/` 包**（Task 1-4，v1 完整代码见 git `dec2253`）：
- `experiment/__init__.py` / `models.py` / `store.py` / `resolver.py` / `cli.py` / `__main__.py`

**修改二期引擎**（Task 5-7，v2 重写）：
- `trading/signal_runner.py` — `PlannedOrder` 加归因字段；`build_orders_from_signals` 按 weight 算 budget
- `trading/engine.py` — `eod_plan` order_dict 透传归因；`_eod` 注入 `resolve_active`（替换占位）
- `experiment/cli.py` — `report` 子命令扫 `logs/trading_plans/plan_*.json`（Task 8）

**测试**：`tests/experiment/`（Task 1-4，v1）+ `tests/trading/`（Task 5-7）+ `tests/experiment/test_e2e_eod_to_plan.py`（Task 9）

---

## Task 1-4：experiment/ 配置中心（v1 保留·完整代码见 git `dec2253`）

v1 的 Task 1-4 在二期引擎合并前已设计完成，与执行引擎无关，**完全有效，照 v1 实现**。完整 TDD 代码（含 models/store/resolver/cli 全部测试与实现）见 `git show dec2253:docs/superpowers/plans/2026-07-22-experiment-system.md` 的 Task 1-4。

**Task 1 · experiment/models.py + 状态机**：`ExperimentStatus`(DRAFT/ACTIVE/ARCHIVED) / `ExperimentVersion` / `AuditLog` / `ActiveExperiment` dataclass + `validate_transition` / `validate_weight_sum`。测试 `tests/experiment/test_models.py`（合法/非法迁移 + 权重和≤1.0）。

**Task 2 · experiment/store.py + 审计**：SQLite（`experiment/experiments.db` WAL+事务）`init_db/create_version/promote/set_weight/archive/rollback/list_versions/list_audit`。每次变更单事务写版本表+审计，失败回滚。测试 `tests/experiment/test_store.py`（CRUD + 审计 + 权重溢出拒绝 + 非法迁移拒绝 + params 不可变）。

**Task 3 · experiment/resolver.py**：`resolve_active(db_path=None) -> list[ActiveExperiment]`，只返 ACTIVE+weight>0，实时读不缓存。测试 `tests/experiment/test_resolver.py`。

**Task 4 · experiment/cli.py + __main__.py**：`python -m experiment create|promote|set-weight|archive|rollback|list`（report 在 Task 8 加）。测试 `tests/experiment/test_cli.py`。

> **执行者**：按 git `dec2253` 的 Task 1-4 全文实现（dataclass/store SQL/resolver/CLI 的完整代码与测试都在那里，逐字照做）。这 4 个 task 与二期引擎无任何耦合，先做完跑绿。

---

### Task 5: signal_runner 归因 + 资金权重透传

**Files:**
- Modify: `trading/signal_runner.py:15-65`（`PlannedOrder` + `build_orders_from_signals`）
- Test: `tests/trading/test_signal_runner_attribution.py`

**Interfaces:**
- Consumes: signal dict 带 `experiment_id`/`experiment_weight`（Task 7 的 `_eod` 注入）
- Produces: `PlannedOrder` 加 `experiment_id:str`/`experiment_weight:float`；`build_orders_from_signals` budget = `capital × pos_cap × experiment_weight`

- [ ] **Step 1: 写失败测试**

`tests/trading/test_signal_runner_attribution.py`:
```python
# -*- coding: utf-8 -*-
"""signal_runner 归因 + 权重：PlannedOrder 带 experiment_id，budget 按 weight 分配。"""
import pytest

from trading.signal_runner import PlannedOrder, build_orders_from_signals


def _signal(symbol="000001.SZ", entry=10.0, neckline=10.5, bottom=9.5,
            exp_id="e1", weight=0.2):
    return {"symbol": symbol, "entry_price": entry, "neckline": neckline,
            "bottom": bottom, "experiment_id": exp_id, "experiment_weight": weight}


def test_planned_order_carries_experiment_id():
    """PlannedOrder 含 experiment_id + experiment_weight 归因字段。"""
    orders = build_orders_from_signals(
        [_signal(exp_id="neckline_v6", weight=0.3)],
        capital=1_000_000, pos_cap=0.05,
        atr_map={"000001.SZ": 0.5}, stop_cfg={"stop_atr_mult": 2.0, "tp_h_mult": 2.0})
    assert len(orders) == 1
    assert orders[0].experiment_id == "neckline_v6"
    assert orders[0].experiment_weight == 0.3


def test_budget_scaled_by_weight():
    """qty = weight × capital × pos_cap / entry，向下取整 100 股。"""
    full = build_orders_from_signals([_signal(weight=1.0)], capital=1_000_000,
        pos_cap=0.05, atr_map={"000001.SZ": 0.5}, stop_cfg={"stop_atr_mult": 2.0, "tp_h_mult": 2.0})
    half = build_orders_from_signals([_signal(weight=0.5)], capital=1_000_000,
        pos_cap=0.05, atr_map={"000001.SZ": 0.5}, stop_cfg={"stop_atr_mult": 2.0, "tp_h_mult": 2.0})
    assert full[0].order.qty > half[0].order.qty
    assert full[0].order.qty % 100 == 0   # A 股 100 整手


def test_signal_without_attribution_defaults_weight_one():
    """老 signal（无 experiment_weight）默认 weight=1.0，experiment_id=""（向后兼容）。"""
    s = {"symbol": "000001.SZ", "entry_price": 10.0, "neckline": 10.5, "bottom": 9.5}
    orders = build_orders_from_signals([s], capital=1_000_000, pos_cap=0.05,
        atr_map={"000001.SZ": 0.5}, stop_cfg={"stop_atr_mult": 2.0, "tp_h_mult": 2.0})
    assert orders[0].experiment_weight == 1.0 and orders[0].experiment_id == ""
```

- [ ] **Step 2: 跑测试验证失败**

Run: `python -m pytest tests/trading/test_signal_runner_attribution.py -v`
Expected: FAIL — `PlannedOrder` 无 `experiment_id` 字段

- [ ] **Step 3: 改 signal_runner.py**

读 `trading/signal_runner.py:15-65`，改造：

```python
@dataclass
class PlannedOrder:
    """计划单（OrderRequest + 出场价 + 实验归因）。"""
    order: OrderRequest
    stop_price: float
    take_profit: float
    neckline: float
    experiment_id: str = ""        # 归因：所属实验版本（_eod 注入）
    experiment_weight: float = 1.0 # 归因：落盘时冻结的资金权重


def build_orders_from_signals(signals, *, capital, pos_cap, atr_map, stop_cfg):
    """信号 → PlannedOrder。资金按 signal.experiment_weight 分配（灰度权重落地）。"""
    stop_mult = stop_cfg.get("stop_atr_mult", 2.0)
    tp_mult = stop_cfg.get("tp_h_mult", 2.0)
    out = []
    for s in signals:
        sym = s.get("symbol")
        entry = s.get("entry_price")
        neckline = s.get("neckline")
        bottom = s.get("bottom")
        atr = atr_map.get(sym) if sym else None
        if not sym or entry is None or neckline is None or bottom is None or atr is None:
            continue
        weight = s.get("experiment_weight", 1.0)        # 每信号各自的权重（灰度分流）
        budget = capital * pos_cap * weight             # weight 在此落地为资金额度
        qty = int(budget / float(entry) / 100) * 100
        if qty <= 0:
            continue
        h = float(neckline) - float(bottom)
        stop_price = float(neckline) - stop_mult * float(atr)
        take_profit = float(neckline) + tp_mult * h
        out.append(PlannedOrder(
            order=OrderRequest(symbol=sym, qty=float(qty), side="buy", price=float(entry)),
            stop_price=stop_price, take_profit=take_profit, neckline=float(neckline),
            experiment_id=s.get("experiment_id", ""),
            experiment_weight=weight,
        ))
    return out
```

- [ ] **Step 4: 跑测试验证通过**

Run: `python -m pytest tests/trading/test_signal_runner_attribution.py tests/trading/test_signal_runner.py -v`
Expected: PASS（新归因测试 + 既有 signal_runner 测试零回归）

- [ ] **Step 5: Commit**

```bash
git add trading/signal_runner.py tests/trading/test_signal_runner_attribution.py
git commit -m "feat(trading): signal_runner 归因+资金权重（PlannedOrder 带 experiment_id·budget 按 weight）"
```

---

### Task 6: eod_plan order_dict 透传归因 + trading_plan 往返

**Files:**
- Modify: `trading/engine.py:127-172`（`eod_plan` 的 PlannedOrder → order_dict 转换）
- Test: `tests/trading/test_trading_plan_attribution.py`

**Interfaces:**
- Consumes: Task 5 的 `PlannedOrder.experiment_id`/`experiment_weight`
- Produces: `trading_plan.save_plan` 落盘的 orders 嵌套 dict 带 `experiment_id`/`experiment_weight`

- [ ] **Step 1: 写失败测试**

`tests/trading/test_trading_plan_attribution.py`:
```python
# -*- coding: utf-8 -*-
"""eod_plan order_dict 透传归因 + trading_plan save/load 往返保真。"""
import pytest

from trading import trading_plan


def test_save_plan_preserves_experiment_attribution(tmp_path, monkeypatch):
    """orders 嵌套 dict 带 experiment_id/experiment_weight，save→load 往返保真。"""
    monkeypatch.setenv("TRADE_PLAN_DIR", str(tmp_path))
    orders = [{
        "order": {"symbol": "000001.SZ", "qty": 1000, "side": "buy", "price": 10.0},
        "stop_price": 9.0, "take_profit": 11.0,
        "experiment_id": "neckline_v6_20260722", "experiment_weight": 0.2,
    }]
    trading_plan.save_plan("2026-07-22", orders)
    loaded = trading_plan.load_plan("2026-07-22")
    assert loaded["orders"][0]["experiment_id"] == "neckline_v6_20260722"
    assert loaded["orders"][0]["experiment_weight"] == 0.2


def test_old_plan_without_attribution_loads_ok(tmp_path, monkeypatch):
    """老 plan（无归因字段）load 不崩（向后兼容，report 归「未归因」桶）。"""
    monkeypatch.setenv("TRADE_PLAN_DIR", str(tmp_path))
    orders = [{"order": {"symbol": "X", "qty": 100, "side": "buy", "price": 10},
               "stop_price": 9, "take_profit": 11}]
    trading_plan.save_plan("2026-07-20", orders)
    loaded = trading_plan.load_plan("2026-07-20")
    assert "experiment_id" not in loaded["orders"][0]   # 老无字段，不崩
```

- [ ] **Step 2: 跑测试验证**

Run: `python -m pytest tests/trading/test_trading_plan_attribution.py -v`
Expected: 多数直接 PASS（save_plan 是 JSON 透传）。重点在 Step 3 确保 `eod_plan` 产 order_dict 时带上归因。

- [ ] **Step 3: 改 eod_plan 的 PlannedOrder → order_dict 透传**

读 `trading/engine.py:127-172`（`eod_plan`），找到 PlannedOrder 转 order_dict 的位置，确保透传归因：

```python
# eod_plan 内 PlannedOrder → order_dict（在 save_plan 前）：
order_dicts = [
    {
        "order": {"symbol": po.order.symbol, "qty": po.order.qty,
                  "side": po.order.side, "price": po.order.price},
        "stop_price": po.stop_price,
        "take_profit": po.take_profit,
        "experiment_id": po.experiment_id,           # 透传归因
        "experiment_weight": po.experiment_weight,   # 透传归因
    }
    for po in orders
]
trading_plan.save_plan(date, order_dicts)
```

`trading_plan.save_plan` 是 JSON 透传（既有逻辑不改），归因字段随 order_dict 落盘。

- [ ] **Step 4: 跑测试验证通过**

Run: `python -m pytest tests/trading/test_trading_plan_attribution.py tests/trading/ -v`
Expected: PASS（归因往返 + 既有 trading 测试零回归）

- [ ] **Step 5: Commit**

```bash
git add trading/engine.py tests/trading/test_trading_plan_attribution.py
git commit -m "feat(trading): eod_plan order_dict 透传 experiment_id 归因（trading_plan JSON 透传）"
```

---

### Task 7: trading/engine.py::_eod 注入 resolve_active（替换 signals=[] 占位）

**Files:**
- Modify: `trading/engine.py:470-481`（`_eod` 触发点，替换 TODO 占位）
- Reference: `strategies/neckline_method.py::scan_at`（识别内核）、`strategies/registry.build_strategy`、`experiment.resolver.resolve_active`
- Test: `tests/trading/test_engine_eod_injection.py`

**Interfaces:**
- Consumes: `experiment.resolver.resolve_active`（Task 3）、`strategies.registry.build_strategy`、颈线法 `scan_at`
- Produces: `_eod` 调 `eod_plan(signals=[带 experiment_id 的信号], ...)`，替换原 `signals=[]`

- [ ] **Step 1: 写失败测试**

`tests/trading/test_engine_eod_injection.py`:
```python
# -*- coding: utf-8 -*-
"""_eod 注入 resolve_active：遍历在线实验 → 每实验 scan_at → signals 带 experiment_id。"""
import asyncio

import pytest
from unittest.mock import MagicMock, patch, AsyncMock

from trading import engine
from experiment.models import ActiveExperiment


@pytest.fixture
def experiments():
    return [ActiveExperiment(experiment_id="e1", strategy_name="neckline",
                             params={"window": 60}, weight=0.2)]


def test_eod_resolves_experiments_and_tags_signals(experiments, monkeypatch):
    """_eod: resolve → 每实验 scan_at → signal 带 experiment_id/experiment_weight。"""
    captured = {}

    async def fake_eod_plan(date, signals, atr_map, capital):
        captured["signals"] = signals
        return {"n_orders": len(signals)}

    fake_strategy = MagicMock()
    fake_strategy.scan_at = MagicMock(return_value=[
        {"symbol": "000001.SZ", "entry_price": 10.0, "neckline": 10.5, "bottom": 9.5}])
    fake_strategy.atr = MagicMock(return_value=0.5)

    monkeypatch.setattr(engine, "eod_plan", fake_eod_plan)
    monkeypatch.setattr(engine.calendar, "is_trading_day", lambda d: True)
    monkeypatch.setattr("experiment.resolver.resolve_active", lambda: experiments)
    monkeypatch.setattr("strategies.registry.build_strategy", lambda name, **kw: fake_strategy)
    monkeypatch.setattr(engine, "_load_universe", lambda: ["000001.SZ"])
    monkeypatch.setattr(engine, "_load_df_upto", lambda sym, date: MagicMock())

    eng = engine.TradingEngine.__new__(engine.TradingEngine)
    asyncio.run(eng._eod())

    sigs = captured["signals"]
    assert len(sigs) == 1
    assert sigs[0]["experiment_id"] == "e1"
    assert sigs[0]["experiment_weight"] == 0.2


def test_eod_failfast_when_no_active(monkeypatch):
    """无在线实验 → _eod fail-fast（不调 eod_plan 产单）。"""
    monkeypatch.setattr(engine.calendar, "is_trading_day", lambda d: True)
    monkeypatch.setattr("experiment.resolver.resolve_active", lambda: [])
    with patch.object(engine, "eod_plan", new=AsyncMock()) as ep:
        eng = engine.TradingEngine.__new__(engine.TradingEngine)
        asyncio.run(eng._eod())
        ep.assert_not_called()
```

- [ ] **Step 2: 跑测试验证失败**

Run: `python -m pytest tests/trading/test_engine_eod_injection.py -v`
Expected: FAIL — `_eod` 仍传 `signals=[]`（占位未替换）

- [ ] **Step 3: 改造 _eod**

读 `trading/engine.py:470-481`，替换 `_eod` 占位：

```python
async def _eod(self) -> None:
    """cron 包装：节假日跳过；交易日 resolve 在线实验 → 每实验 scan_at → eod_plan。

    v2 注入（二期 gap② 策略数据源）：从 experiment.resolver.resolve_active() 拿在线
    实验，每实验用各自 params 跑 scan_at，signal 标 experiment_id 归因。
    """
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    if not calendar.is_trading_day(today):
        logger.info("eod_plan 跳过：今日非交易日 %s", today)
        return

    from experiment.resolver import resolve_active
    from strategies.registry import build_strategy

    experiments = resolve_active()
    if not experiments:
        logger.warning("_eod 无在线实验，跳过（fail-fast，不产单）")
        return

    universe = _load_universe()                         # 创板科创可交易标的
    signals, atr_map = [], {}
    for exp in experiments:
        strategy = build_strategy(exp.strategy_name, cfg_override=exp.params)
        for sym in universe:
            df = _load_df_upto(sym, today)              # 无前视 .loc[:today]
            try:
                for s in strategy.scan_at(sym, df, today, {}):
                    s["experiment_id"] = exp.experiment_id        # 归因标记
                    s["experiment_weight"] = exp.weight
                    signals.append(s)
                    atr_map[sym] = strategy.atr(sym)
            except Exception as e:
                logger.warning("_eod scan_at %s 异常跳过: %s", sym, e)

    await eod_plan(today, signals, atr_map,
                   capital=float(os.getenv("TRADE_CAPITAL", "1_000_000")))
```

在 `trading/engine.py` 模块级加两个数据加载辅助（从 `strategies/neckline_method.py::scan_at` 既有加载逻辑抽取）：
```python
def _load_universe() -> list:
    """加载创板科创可交易标的池（对齐 strategies/neckline_method 既有 universe）。"""
    # 从 neckline_method.scan_at 的 universe 加载方式抽取
    ...

def _load_df_upto(symbol: str, date: str):
    """加载 symbol 截至 date 的前复权日线（无前视）。"""
    # 从 neckline_method.scan_at 的 df.loc[:T] 加载抽取
    ...
```

**执行者必读**：`_load_universe`/`_load_df_upto`/`strategy.atr` 从 `strategies/neckline_method.py` 既有 `scan_at` 实现抽取（该 `scan_at` 已含 universe 加载 + df.loc[:T] + ATR 预算）。`scan_at` 签名 `(symbol, df_T, T, strategy_state)`，`_load_df_upto` 产 df_T 后直传。

- [ ] **Step 4: 跑测试验证通过**

Run: `python -m pytest tests/trading/test_engine_eod_injection.py tests/trading/ -v`
Expected: PASS（注入测试 + 既有 engine 测试零回归——`_eod` 改造只影响信号源，pre_open/stoploss/post_close 不动）

- [ ] **Step 5: Commit**

```bash
git add trading/engine.py tests/trading/test_engine_eod_injection.py
git commit -m "feat(trading): _eod 注入 resolve_active（二期 gap② 策略数据源·替换 signals 占位）"
```

---

### Task 8: report 归因聚合命令（扫 trading_plans）

**Files:**
- Modify: `experiment/cli.py`（加 report 子命令，落点 logs/trading_plans/）
- Test: `tests/experiment/test_report.py`

**Interfaces:**
- Consumes: 扫 `logs/trading_plans/plan_*.json`（`TRADE_PLAN_DIR` env）
- Produces: CLI `report --since <date>` 按 experiment_id 聚合

- [ ] **Step 1: 写失败测试**

`tests/experiment/test_report.py`:
```python
# -*- coding: utf-8 -*-
"""report 命令：扫 logs/trading_plans/plan_*.json 按 experiment_id 聚合。"""
import pytest
from unittest.mock import patch

from experiment import cli


def test_report_aggregates_by_experiment(capsys, monkeypatch):
    """同 experiment_id 的 order 聚到一组。"""
    plans = [{"date": "2026-07-10", "confirmed": True, "orders": [
        {"order": {"symbol": "A", "qty": 100, "side": "buy", "price": 10},
         "stop_price": 9, "take_profit": 11,
         "experiment_id": "e_prod", "experiment_weight": 0.8},
        {"order": {"symbol": "B", "qty": 100, "side": "buy", "price": 10},
         "stop_price": 9, "take_profit": 11,
         "experiment_id": "e_cand", "experiment_weight": 0.2}]}]
    monkeypatch.setattr(cli, "_DEFAULT_DB", ":memory:")
    with patch("experiment.cli._load_all_plans", return_value=plans):
        rc = cli.main(["report", "--since", "2026-07-01"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "e_prod" in out and "e_cand" in out


def test_report_handles_unattributed_orders(capsys, monkeypatch):
    """无 experiment_id 的老 order 归「未归因」桶，不崩。"""
    plans = [{"date": "2026-07-01", "confirmed": True, "orders": [
        {"order": {"symbol": "X", "qty": 100, "side": "buy", "price": 10},
         "stop_price": 9, "take_profit": 11}]}]
    monkeypatch.setattr(cli, "_DEFAULT_DB", ":memory:")
    with patch("experiment.cli._load_all_plans", return_value=plans):
        rc = cli.main(["report", "--since", "2026-07-01"])
    assert rc == 0
    assert "未归因" in capsys.readouterr().out
```

- [ ] **Step 2: 跑测试验证失败**

Run: `python -m pytest tests/experiment/test_report.py -v`
Expected: FAIL — `report` 子命令未注册 / `_load_all_plans` 不存在

- [ ] **Step 3: 实现 report**

`experiment/cli.py` 加：
```python
import glob
import json
import os


def _load_all_plans(since: str = None) -> list:
    """扫 logs/trading_plans/plan_*.json（按 experiment_id 聚合用）。"""
    plan_dir = os.getenv("TRADE_PLAN_DIR", "logs/trading_plans")
    plans = []
    for path in sorted(glob.glob(os.path.join(plan_dir, "plan_*.json"))):
        try:
            with open(path, encoding="utf-8") as f:
                p = json.load(f)
            if since and p.get("date", "") < since:
                continue
            plans.append(p)
        except Exception:
            continue
    return plans


def _report(args) -> int:
    """按 experiment_id 聚合 trading_plan orders：n/权重/标的数。"""
    plans = _load_all_plans(args.since)
    groups = {}
    for p in plans:
        for o in p.get("orders", []):
            eid = o.get("experiment_id") or "未归因"
            g = groups.setdefault(eid, {"n": 0, "weight": None, "symbols": set()})
            g["n"] += 1
            g["weight"] = o.get("experiment_weight")
            g["symbols"].add(o["order"]["symbol"])
    print(f"{'experiment_id':30}{'订单数':>8}{'权重':>8}{'标的数':>8}")
    for eid, g in sorted(groups.items()):
        w = f"{g['weight']:.2f}" if g["weight"] is not None else "-"
        print(f"{eid:30}{g['n']:>8}{w:>8}{len(g['symbols']):>8}")
    return 0
```

`_build_parser` 加 report 子命令 + `main` 分支 `elif args.cmd == "report": return _report(args)`。

- [ ] **Step 4: 跑测试验证通过**

Run: `python -m pytest tests/experiment/test_report.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add experiment/cli.py tests/experiment/test_report.py
git commit -m "feat(experiment): report 扫 trading_plans 按 experiment_id 聚合归因"
```

---

### Task 9: 端到端 _eod → plan（dry_run 影子模式）

**Files:**
- Test: `tests/experiment/test_e2e_eod_to_plan.py`
- Manual: miniQMT 虚拟盘验收 SOP（待二期 gap①③ 补全）

**Interfaces:**
- Consumes: Task 1-8 全部
- Produces: 端到端归因不断链验证（dry_run）

- [ ] **Step 1: 写端到端测试**

`tests/experiment/test_e2e_eod_to_plan.py`:
```python
# -*- coding: utf-8 -*-
"""端到端（dry_run）：create exp → _eod resolve → scan_at → PlannedOrder(带 exp_id)
→ 归因全程不断链。design v2 §12 验收 2。"""
import asyncio

import pytest
from unittest.mock import MagicMock, patch

from experiment import store, resolver, cli


@pytest.fixture
def db(tmp_path, monkeypatch):
    p = str(tmp_path / "t.db")
    store.init_db(p)
    monkeypatch.setattr("experiment.store._DEFAULT_DB", p)
    monkeypatch.setattr("experiment.resolver._DEFAULT_DB", p)
    return p


def test_e2e_attribution_chain(db, tmp_path, monkeypatch):
    """全链路：experiment_id 从创建→signal→PlannedOrder 全程携带。"""
    cli.main(["create", "--strategy", "neckline", "--params", '{"window":60}',
              "--experiment-id", "e1", "--created-at", "2026-07-22T10:00:00"])
    cli.main(["promote", "e1", "--weight", "1.0"])
    assert resolver.resolve_active()[0].experiment_id == "e1"

    fake_strategy = MagicMock()
    fake_strategy.scan_at = MagicMock(return_value=[
        {"symbol": "000001.SZ", "entry_price": 10.0, "neckline": 10.5, "bottom": 9.5}])
    fake_strategy.atr = MagicMock(return_value=0.5)
    monkeypatch.setattr("strategies.registry.build_strategy", lambda name, **kw: fake_strategy)
    monkeypatch.setattr("trading.engine._load_universe", lambda: ["000001.SZ"])
    monkeypatch.setattr("trading.engine._load_df_upto", lambda s, d: MagicMock())

    captured = {}
    async def fake_eod_plan(date, signals, atr_map, capital):
        captured["signals"] = signals
        from trading.signal_runner import build_orders_from_signals
        captured["orders"] = build_orders_from_signals(
            signals, capital=capital, pos_cap=0.05, atr_map=atr_map,
            stop_cfg={"stop_atr_mult": 2.0, "tp_h_mult": 2.0})
        return {"n_orders": len(captured["orders"])}
    monkeypatch.setattr("trading.engine.eod_plan", fake_eod_plan)
    monkeypatch.setattr("trading.engine.calendar.is_trading_day", lambda d: True)

    from trading import engine
    eng = engine.TradingEngine.__new__(engine.TradingEngine)
    asyncio.run(eng._eod())

    assert captured["signals"][0]["experiment_id"] == "e1"
    assert captured["orders"][0].experiment_id == "e1"
    assert captured["orders"][0].experiment_weight == 1.0
    assert captured["orders"][0].order.qty % 100 == 0
```

- [ ] **Step 2: 跑测试验证**

Run: `python -m pytest tests/experiment/test_e2e_eod_to_plan.py -v`
Expected: PASS（归因 experiment → signal → PlannedOrder 不断链）

- [ ] **Step 3: miniQMT 虚拟盘手动验收 SOP（待二期 gap①③ 补全后执行）**

前置：二期 gap①（post_close equity 源）/ gap③（行情源）补全前只在 dry_run 验收。补全后：
1. `.venv310/Scripts/python -m experiment create --strategy neckline --params '<v6基线>' --experiment-id neckline_v6_<日期>`
2. `python -m experiment promote neckline_v6_<日期> --weight 0.1`
3. 启动二期引擎：`set AUTO_TRADE_MODE=dry_run && .venv310/Scripts/python -m trading`（先 dry_run 跑 ≥5 交易日）
4. 观察 15:35 `_eod`：resolve → scan_at → `trading_plans/plan_<date>.json`（orders 带 experiment_id）
5. 钉钉确认 → 次日 09:22 `pre_open` 挂限价（dry_run 只记录）
6. 盘中 `stop_loss_monitor` 每 5min（dry_run 只记录）
7. 15:30 `post_close` reconcile 对账
8. `python -m experiment report --since <日期>` 确认归因聚合
9. 回滚演练：promote candidate → 观察 → rollback

dry_run 稳定 + 二期 gap①③ 补全后，`set AUTO_TRADE_MODE=live` 切 miniQMT 虚拟盘（账号 `10110356`）真实验收。

- [ ] **Step 4: 全量回归**

Run: `python -m pytest tests/experiment/ tests/trading/ -v`
Expected: 全绿（实验系统 + 二期引擎零回归）

- [ ] **Step 5: Commit + 合并准备**

```bash
git add tests/experiment/test_e2e_eod_to_plan.py
git commit -m "test(experiment): 端到端 _eod→plan 归因不断链（dry_run）+ miniQMT 验收 SOP"
```

miniQMT 验收通过 + 二期 gap①③ 补全后，分支合并 master。

---

## Self-Review（v2）

**1. Spec coverage（v2 spec）**：
- §3 数据模型/SQLite → Task 1-2（v1）✓
- §4 架构/experiment 包 → Task 1-4（v1）✓
- §5 数据流（_eod 注入/CLI/归因）→ Task 4/7/8 ✓
- §6.2 _eod 改造 → Task 7 ✓
- §6.3 signal_runner 归因+权重 → Task 5 ✓
- §6.5 trading_plan 归因 → Task 6 ✓
- §7 运行模式（dry_run+miniQMT）→ Task 9 ✓
- §8 错误处理（权重红线/事务/fail-fast）→ Task 2/7 ✓
- §9 测试 → 各 Task TDD + Task 9 e2e ✓
- §12 验收（6 条 v2）→ Task 9（归因不断链/CLI/report/红线/二期零回归）✓
- §6.4 出场复用二期 stop_loss → 不需新 task（二期既有）✓

**2. Placeholder scan**：Task 7 的 `_load_universe`/`_load_df_upto` 标了「从 neckline_method.scan_at 抽取」——给了精确引用位置（`strategies/neckline_method.py::scan_at` 已含 universe 加载 + df.loc[:T] + ATR），执行者按既有实现抽取，非占位符。Task 1-4 指向 git `dec2253` 取完整代码（明确引用，非 placeholder）。

**3. Type consistency**：`experiment_id`/`experiment_weight` 跨 Task 5（PlannedOrder）→ Task 6（order_dict）→ Task 7（signal 标记）→ Task 8（report 聚合）字段名一致；`ActiveExperiment`（Task 1）→ `resolve_active`（Task 3）→ Task 7 `_eod` 消费一致；`build_orders_from_signals` 签名 Task 5/9 一致。

**4. v1→v2 作废确认**：v1 Task 5（base 协议 check_exit 扩展）/ Task 7（颈线法 check_exit）/ Task 8（caisen 适配器）/ Task 10（execution/engine 改造）在 v2 完全移除——二期引擎不用盘中 check_exit 范式，execution/engine.py 不在实盘路径。

---

## Execution Handoff

Plan v2 complete and saved to `docs/superpowers/plans/2026-07-22-experiment-system.md`. Two execution options:

1. **Subagent-Driven (recommended)** — 每个 Task 派 fresh subagent，任务间 review
2. **Inline Execution** — 本会话内按 executing-plans 批量执行

Which approach?
