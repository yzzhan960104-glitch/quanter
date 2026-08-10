# engine.py 模块化拆分（T1）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `trading/engine.py`（3437 行 god module）拆成 8 个职责模块 + engine 收缩到 ~800 行（集群 J/C），行为完全等价。

**Architecture:** 按职责集群外迁（critical/data_ctx/eod_plan/order_state + phases/{pre_open,stop_loss,post_close,exit}）；引入 `EnginePorts` 窄接口消除 `_ACTIVE_ENGINE` 单例桥（phases 不依赖 `TradingEngine` 类）；engine.py re-export 公共符号保证外部零改动；10 Task 渐进，每 Task 独立回归门。

**Tech Stack:** Python 3.10（`.venv310`）、pytest、asyncio、APScheduler。

## Global Constraints

- **行为等价红线**：状态机语义（订单/计划/持仓状态迁移，归 #5）、数据路径（state_store/account_daily/fill 读写口，归 #3）**不变形** —— 移符号时逐行原样，不改逻辑。
- **公共 API 兼容**：engine.py 原 export 的符号全部 re-export，消费方（broadcast/trading_service/orchestrate/tests）`from trading.engine import X` 零改动。
- **每 Task 独立回归门**：该 Task 的回归测试集必须全绿才能 commit；任一红即停，不累积。
- **窄 Ports 边界**：`state_store`/`lake`/`gateway` 等项目级单例保持模块级访问（phases 直接 import），**不**经 Ports 注入（不越界 T6）。
- **Python 环境**：所有命令用 `cd /e/quanter && PYTHONUTF8=1 .venv310/Scripts/python.exe -m pytest ...`。
- **重构 TDD 适配**：本 plan 是纯重构（无新逻辑），故采用「基线绿 → 移符号 → re-export → 回归绿 → commit」安全网模式，不写新失败测试（既有 60+ 单测是安全网）；仅 Task 1 的 `EnginePorts` 是新代码，写单测。

## File Structure

| 文件 | 职责 | 创建/修改 |
|---|---|---|
| `trading/ports.py` | `EnginePorts` dataclass（窄依赖接口） | Create（Task 1） |
| `trading/critical.py` | 集群 A：L1 停调度 + 告警 | Create（Task 2） |
| `trading/order_state.py` | 集群 I：订单回调三分支 + 状态推进 | Create（Task 3） |
| `trading/data_ctx.py` | 集群 B：lake 数据加载 helper | Create（Task 4） |
| `trading/eod_plan.py` | 集群 D：盘后计划生成 | Create（Task 5） |
| `trading/phases/__init__.py` | phases 包标记 | Create（Task 6） |
| `trading/phases/pre_open.py` | 集群 E：盘前挂单 | Create（Task 6） |
| `trading/phases/stop_loss.py` | 集群 F：盘中止损 | Create（Task 7） |
| `trading/phases/post_close.py` | 集群 G：盘后对账 | Create（Task 8） |
| `trading/phases/exit.py` | 集群 H：止盈 | Create（Task 9） |
| `trading/engine.py` | 收缩到集群 J（调度/装配/health_guard）+ C（网关/gate）+ re-export | Modify（每 Task） |
| `tests/trading/test_ports.py` | `EnginePorts` 单测 | Create（Task 1） |

> 符号行号引自 [engine-current-state §1.1](../../architecture/deep-dives/engine-current-state.md)（截至 2026-08-09），代码变更后以符号名为准。

---

## Task 1: EnginePorts + 消除 `_ACTIVE_ENGINE` 单例桥

**Files:**
- Create: `trading/ports.py`
- Create: `tests/trading/test_ports.py`
- Modify: `trading/engine.py`（`_ACTIVE_ENGINE` 5 处使用点：L201 定义 / L2144 赋值 / L740 gate / L893 whitelist 注入 / L1843 whitelist 清空；`__init__` 构造 `_ports`；5 job wrapper 改用 `_ports`）

**Interfaces:**
- Produces: `EnginePorts(gate, whitelist_add, whitelist_clear)`；`TradingEngine._ports` 实例属性；`_ACTIVE_ENGINE` 删除。

- [ ] **Step 1: 写 EnginePorts 单测**

```python
# tests/trading/test_ports.py
from trading.ports import EnginePorts

def test_ports_holds_three_callbacks():
    calls = {"gate": 0, "add": 0, "clr": 0}
    ports = EnginePorts(
        gate=lambda d, gw: (calls.__setitem__("gate", calls["gate"]+1), None)[1],
        whitelist_add=lambda syms: calls.__setitem__("add", calls["add"]+1),
        whitelist_clear=lambda: calls.__setitem__("clr", calls["clr"]+1),
    )
    ports.gate("2026-01-01", None); ports.whitelist_add(["000001"]); ports.whitelist_clear()
    assert calls == {"gate": 1, "add": 1, "clr": 1}
```

- [ ] **Step 2: 跑单测验证失败**

Run: `PYTHONUTF8=1 .venv310/Scripts/python.exe -m pytest tests/trading/test_ports.py -v`
Expected: FAIL（`No module named 'trading.ports'`）

- [ ] **Step 3: 创建 trading/ports.py**

```python
# -*- coding: utf-8 -*-
"""EnginePorts：phases 外迁函数的窄依赖接口（T1 缝合点 #1 解）。

仅承载 engine 实例特有的、原经 _ACTIVE_ENGINE 单例桥访问的依赖（gate + 动态白名单
读/写/清空）。项目级单例（state_store/lake/gateway）保持模块级访问，phases 直接 import
—— 不越界 T6（state_store SSoT 演进）。
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

@dataclass
class EnginePorts:
    # gate=原 _pre_open_gate：盘前三段闸（plan-confirmed→gateway-health→data-ready），
    # 任一未绿早返 skip。返回值类型对齐 _pre_open_gate 现签名（实现时确认）。
    gate: Callable[[str, Any], Awaitable[Any]]
    whitelist_add: Callable[[list[str]], None]    # 注入标的到 self._dynamic_whitelist
    whitelist_clear: Callable[[], None]           # post_close 清空 self._dynamic_whitelist
```

- [ ] **Step 4: 跑单测验证通过**

Run: `PYTHONUTF8=1 .venv310/Scripts/python.exe -m pytest tests/trading/test_ports.py -v`
Expected: PASS

- [ ] **Step 5: engine.py __init__ 构造 _ports（紧挨 L2144 原 _ACTIVE_ENGINE 赋值处）**

在 `TradingEngine.__init__` 末尾（原 `self._ACTIVE_ENGINE = self` 附近）插入：
```python
from trading.ports import EnginePorts  # 顶部 import 更佳，避免循环——ports.py 不依赖 engine，安全
self._ports = EnginePorts(
    gate=self._pre_open_gate,
    whitelist_add=lambda syms: self._dynamic_whitelist.update(syms) if hasattr(self, '_dynamic_whitelist') else None,
    whitelist_clear=lambda: setattr(self, '_dynamic_whitelist', set()),
)
```
> 实现时核对 `_dynamic_whitelist` 的真实类型（set/list/dict）与初始化点，`whitelist_add`/`clear` 的 lambda 体对齐其真实 mutate 语义（深剖 L893 注入 / L1843 清空）。若 `_dynamic_whitelist` 是 dict，`add` 改为按 key 置位、`clear` 改为 `.clear()`。

- [ ] **Step 6: 消除 _ACTIVE_ENGINE 3 处使用点（gate/whitelist）**

模块级函数原本 `engine = _ACTIVE_ENGINE; engine._pre_open_gate(...)` 等，改为接收 `ports` 参数。因本 Task 尚未外迁 phases（Task 6-8 才迁），此处先把模块级 `pre_open`/`_pre_open_impl`/`post_close` 的签名加 `ports: EnginePorts` 形参，调用处 `_ACTIVE_ENGINE._pre_open_gate` → `ports.gate`、`_ACTIVE_ENGINE._dynamic_whitelist` 注入 → `ports.whitelist_add`、清空 → `ports.whitelist_clear`。调用方（engine.py 内 `_pre_open`/`_post_close` wrapper）传 `self._ports`。

- [ ] **Step 7: 删除 _ACTIVE_ENGINE 定义与赋值（L201 / L2144）**

确认 grep 无残留：`grep -n "_ACTIVE_ENGINE" trading/engine.py` 应无输出。

- [ ] **Step 8: 全量回归门**

Run: `PYTHONUTF8=1 .venv310/Scripts/python.exe -m pytest tests/trading tests/e2e_long_cycle -q`
Expected: 全绿（行为等价，依赖方向显式化但语义不变）。红则定位——多半是 `_dynamic_whitelist` mutate 语义没对齐，回 Step 5 修正 lambda。

- [ ] **Step 9: Commit**

```bash
git add trading/ports.py trading/engine.py tests/trading/test_ports.py
git commit -m "refactor(engine): T1-1 引入 EnginePorts + 消除 _ACTIVE_ENGINE 单例桥"
```

---

## Task 2: 抽 critical.py（集群 A · 最独立）

**Files:**
- Create: `trading/critical.py`
- Modify: `trading/engine.py`（移出符号 + re-export + `_critical_guard` 装饰的 5 个 job wrapper 改 import）

**Interfaces:**
- Produces: `critical._alert_critical(msg)`、`critical._CriticalHalt`、`critical._critical_guard(coro_method)`、`critical.halt_for(halted_flag_setter)`（原 `_halt` 的 free-function 化，接收一个设置 `_halted=True` 的回调）、`critical.guard_skip_rounds(fail_count)`。
- Consumes: Task 1 的无（critical 零下游交易耦合）。

**移出符号**（深剖 §1.1 集群 A）：`_alert_critical`（L112）、`class _CriticalHalt`（L141）、`_critical_guard`（L155）、类方法 `_halt`（L2527）、`_guard_skip_rounds`（L2550）。

> **注意**：`_halt`/`_guard_skip_rounds` 是实例方法（访问 `self._halted` 等）。迁 critical.py 时改为接收 engine 引用或回调——但 critical 不应反向依赖 `TradingEngine` 类。解法：`_halt` 改为 `halt(msg, set_halted: Callable)`，engine 的 wrapper 传 `lambda: setattr(self, '_halted', True)`；或在 engine 留薄 wrapper 调 critical。**实现时优先「critical 留纯函数 + engine 留薄 wrapper」**，避免 critical → engine 耦合。

- [ ] **Step 1: 跑基线确认绿**

Run: `PYTHONUTF8=1 .venv310/Scripts/python.exe -m pytest tests/trading/test_critical_guard.py tests/trading/test_pre_open_l1_halt.py tests/trading/test_stop_loss_l1_halt.py -v`
Expected: 全绿（记录为基线）。

- [ ] **Step 2: 创建 trading/critical.py，移入符号**

把 `_alert_critical`、`_CriticalHalt`、`_critical_guard` 逐字移入 critical.py（它们已是模块级，无 `self` 依赖，直接搬）。`_halt`/`_guard_skip_rounds` 改为上述「free function + 回调」形态。`_mode()`/`_trade_cfg()`（L208/L217）一并移入（模式/配置读口，属集群 A 基础设施）。

- [ ] **Step 3: engine.py re-export + job wrapper import 改向**

engine.py 顶部加：
```python
from trading.critical import (_alert_critical, _CriticalHalt, _critical_guard,
                               _mode, _trade_cfg)  # noqa: F401  公共 API 兼容
```
engine 内 `_halt`/`_guard_skip_rounds` 留薄实例方法 wrapper（调 critical 的 free function + 传 `self._halted` setter），或直接复用 critical。5 个 job wrapper 上的 `@_critical_guard` 装饰器引用不变（经 re-export）。

- [ ] **Step 4: 跑回归门**

Run: `PYTHONUTF8=1 .venv310/Scripts/python.exe -m pytest tests/trading/test_critical_guard.py tests/trading/test_pre_open_l1_halt.py tests/trading/test_stop_loss_l1_halt.py tests/trading/test_l2_aggregated_critical.py -v`
Expected: 全绿。

- [ ] **Step 5: Commit**

```bash
git add trading/critical.py trading/engine.py
git commit -m "refactor(engine): T1-2 抽 critical.py（集群 A 停调度/告警基础设施）"
```

---

## Task 3: 抽 order_state.py（集群 I · 缝合点 #2）

**Files:**
- Create: `trading/order_state.py`
- Modify: `trading/engine.py`

**Interfaces:**
- Produces: `order_state.handle_order_update(engine_ref, update)`（原 `_handle_order_update`，三分支 async_response/order/trade）、`order_state.order_direction(gw, order_id)`、`order_state.advance_order_state_from_status(update)`。
- Consumes: state_store（模块级）、`_place_take_profit`（Task 9 才迁，本 Task 暂经 engine 引用调）。

**移出符号**：`_handle_order_update`（L3053，~215 行）、`_order_direction`（L3268）、`_advance_order_state_from_status`（L3330）。

> **依赖处理**：`_handle_order_update` 的 `trade` 分支调 `_place_take_profit`（集群 H，Task 9 迁）。本 Task 迁移时，`handle_order_update` 接收 `engine` 引用（或一个 `on_fill: Callable` 回调），调 `engine._place_take_profit` —— 临时形态，Task 9 迁 H 后改为调 `phases.exit.place_take_profit`。**幂等红线**（fill 表 UNIQUE + `_fill_inserted` 守卫）逐行原样，不改。

- [ ] **Step 1: 跑基线**

Run: `PYTHONUTF8=1 .venv310/Scripts/python.exe -m pytest tests/trading/test_engine_order_update_handler.py tests/trading/test_fill_db_contract.py -v`
Expected: 全绿。

- [ ] **Step 2: 创建 trading/order_state.py，移入 3 符号**

逐字移入。`_handle_order_update` 改为 `async def handle_order_update(self_or_engine, update)`——因 bootstrap 注册的是 `self._on_order_update`（实例绑定），最简形态是把这 3 个方法**保留为接受 engine 实例的函数**：`async def handle_order_update(engine, update)`，内部 `engine._gw`/`engine._place_take_profit` 等访问不变（临时耦合 engine，Task 9 后收口）。

- [ ] **Step 3: engine.py re-export + bootstrap 回调接线**

engine.py 顶部 `from trading.order_state import handle_order_update, order_direction, advance_order_state_from_status  # noqa: F401`。`TradingEngine` 留薄 wrapper `_handle_order_update = lambda self, u: handle_order_update(self, u)`（或 bootstrap 注册处直接绑 `lambda u: handle_order_update(self, u)`）。`set_order_update_callback` 接线点（bootstrap）确认仍指向有效回调。

- [ ] **Step 4: 跑回归门**

Run: `PYTHONUTF8=1 .venv310/Scripts/python.exe -m pytest tests/trading/test_engine_order_update_handler.py tests/trading/test_fill_db_contract.py tests/trading/test_query_trades_db.py -v`
Expected: 全绿。

- [ ] **Step 5: Commit**

```bash
git add trading/order_state.py trading/engine.py
git commit -m "refactor(engine): T1-3 抽 order_state.py（集群 I 订单回调/状态机，缝合点#2）"
```

---

## Task 4: 抽 data_ctx.py（集群 B）

**Files:**
- Create: `trading/data_ctx.py`
- Modify: `trading/engine.py`

**Interfaces:**
- Produces: `data_ctx.load_universe(lake)`、`load_df_upto(lake, symbol, date)`、`load_recent_plan_symbols(days_back, today)`、`resolve_cooldown_days(experiments)`、`load_integrity_ctx(today)`、`resolve_id_window(strategy)`、`plan_data_keys(plan)`。
- Consumes: `data.lake_reader`、`config`（模块级）。

**移出符号**：`_load_universe`（L273）、`_load_df_upto`（L295）、`_load_recent_plan_symbols`（L328）、`_resolve_cooldown_days`（L376）、`_load_integrity_ctx`（L401）、`_resolve_id_window`（L437）（模块级）+ 类方法 `_plan_data_keys`（L2232，改为 free function `plan_data_keys(plan)`）。

> 移入时去掉前导 `_`（它们已是模块内 private，外迁后变公开 API，去 `_` 更一致；re-export 时同时导出旧名 `_load_universe = load_universe` 保兼容）。`_plan_data_keys` 原是实例方法但深剖说不读 self 状态（仅反查 plan），改 free function 安全。

- [ ] **Step 1: 跑基线**

Run: `PYTHONUTF8=1 .venv310/Scripts/python.exe -m pytest tests/trading/test_data_ready.py tests/trading/test_engine_eod_injection.py -v`
Expected: 全绿。

- [ ] **Step 2: 创建 trading/data_ctx.py，移入 7 符号（去前导 _）**

逐字移入，函数名去 `_` 前缀。内部 `from trading import state_store`/`from data.lake_reader import ...` 等模块级依赖照旧。

- [ ] **Step 3: engine.py re-export + 调用处改名**

engine.py 顶部 re-export（含旧 `_` 名兼容）：
```python
from trading.data_ctx import (load_universe, load_df_upto, load_recent_plan_symbols,
    resolve_cooldown_days, load_integrity_ctx, resolve_id_window, plan_data_keys)  # noqa: F401
_load_universe, _load_df_upto = load_universe, load_df_upto  # 旧名兼容
```
engine.py 内部所有 `_load_universe(...)` 调用改为 `load_universe(...)`（或保留旧名经 re-export 也可，二选一统一）。`_plan_data_keys` 调用改为 `plan_data_keys`。

- [ ] **Step 4: 跑回归门**

Run: `PYTHONUTF8=1 .venv310/Scripts/python.exe -m pytest tests/trading/test_data_ready.py tests/trading/test_engine_eod_injection.py tests/trading/test_pipeline_then_eod.py -v`
Expected: 全绿。

- [ ] **Step 5: Commit**

```bash
git add trading/data_ctx.py trading/engine.py
git commit -m "refactor(engine): T1-4 抽 data_ctx.py（集群 B lake 数据加载 helper）"
```

---

## Task 5: 抽 eod_plan.py（集群 D）

**Files:**
- Create: `trading/eod_plan.py`
- Modify: `trading/engine.py`

**Interfaces:**
- Produces: `eod_plan.compute(date, signals, atr_map, capital)`、`eod_plan.sanity_check_date_alignment(today)`。
- Consumes: `strategies`（颈线法 detect_signal）、`data`、`trading.orchestrate.pipeline`、state_store、data_ctx（Task 4）。

**移出符号**：`eod_plan`（L498，模块级核心，~157 行）+ 类方法 `_sanity_check_date_alignment`（L2567，改 free function）。`_eod`/`_pipeline_then_eod`（类方法 wrapper）**留 engine**（深剖 §2.D：wrapper 形态是解耦伏笔，留 engine 调 orchestrate）。

> `eod_plan` 原 signature `async eod_plan(date, signals, atr_map, capital)`。迁出后命名 `compute`（避免模块名=函数名歧义），re-export `eod_plan = compute` 兼容旧调用。

- [ ] **Step 1: 跑基线**

Run: `PYTHONUTF8=1 .venv310/Scripts/python.exe -m pytest tests/trading/test_engine_eod_injection.py tests/trading/test_pipeline_then_eod.py tests/trading/test_veto_plan_db.py tests/trading/test_trigger_eod_once.py -v`
Expected: 全绿。

- [ ] **Step 2: 创建 trading/eod_plan.py，移入 eod_plan + sanity_check**

逐字移入。`_sanity_check_date_alignment` 改 `sanity_check_date_alignment(today)` free function。

- [ ] **Step 3: engine.py re-export + _eod wrapper 改调用**

```python
from trading.eod_plan import compute as eod_plan, sanity_check_date_alignment  # noqa: F401
```
engine `_eod`/`_pipeline_then_eod` wrapper 内调 `eod_plan(...)` 不变（经 re-export 指向新模块）。`_sanity_check_date_alignment` 调用改 `sanity_check_date_alignment`。

- [ ] **Step 4: 跑回归门**

Run: `PYTHONUTF8=1 .venv310/Scripts/python.exe -m pytest tests/trading/test_engine_eod_injection.py tests/trading/test_pipeline_then_eod.py tests/trading/test_veto_plan_db.py tests/trading/test_trigger_eod_once.py tests/trading/test_engine_sanity_check.py -v`
Expected: 全绿。

- [ ] **Step 5: Commit**

```bash
git add trading/eod_plan.py trading/engine.py
git commit -m "refactor(engine): T1-5 抽 eod_plan.py（集群 D 盘后计划生成）"
```

---

## Task 6: 抽 phases/pre_open.py（集群 E）

**Files:**
- Create: `trading/phases/__init__.py`（空包标记）
- Create: `trading/phases/pre_open.py`
- Modify: `trading/engine.py`

**Interfaces:**
- Produces: `phases.pre_open.pre_open(ports, date)`、`phases.pre_open._pre_open_impl(ports, date)`。
- Consumes: `EnginePorts`（Task 1）、`order_state`（Task 3）、`data_ctx`（Task 4）、state_store、gateway。

**移出符号**：`pre_open`（L655，job_ledger 包裹）+ `_pre_open_impl`（L704，~330 行最大单函数）。类方法 `_pre_open`（L2852，wrapper）**留 engine**（APScheduler 绑定），内部改调 `phases.pre_open.pre_open(self._ports, date)`。

> `_pre_open_impl` 原经 `_ACTIVE_ENGINE` 访问 gate/whitelist，Task 1 已改经 `ports` 参数。本 Task 把整个函数搬到 phases/pre_open.py，signature `async def _pre_open_impl(ports, date)`。

- [ ] **Step 1: 跑基线**

Run: `PYTHONUTF8=1 .venv310/Scripts/python.exe -m pytest tests/trading/test_pre_open_ledger.py tests/trading/test_pre_open_ledger_semantics.py tests/trading/test_engine_pre_open_gate.py tests/trading/test_e2e_trading_flow.py -v`
Expected: 全绿。

- [ ] **Step 2: 创建 trading/phases/__init__.py（空）+ trading/phases/pre_open.py，移入 pre_open + _pre_open_impl**

`phases/pre_open.py` 顶部：
```python
from trading.ports import EnginePorts
from trading import state_store, job_ledger
# 其他原 _pre_open_impl 的模块级 import 照搬
```
逐字移入 `pre_open`/`_pre_open_impl`，把 `ports: EnginePorts` 形参加上（Task 1 已在 engine 内改过签名，本 Task 是把代码物理迁移到 phases/）。

- [ ] **Step 3: engine.py re-export + _pre_open wrapper 改调用**

```python
from trading.phases.pre_open import pre_open  # noqa: F401
```
`TradingEngine._pre_open`（wrapper，L2852）改为：
```python
async def _pre_open(self):
    await pre_open(self._ports, <date 解析>)
```

- [ ] **Step 4: 跑回归门**

Run: `PYTHONUTF8=1 .venv310/Scripts/python.exe -m pytest tests/trading/test_pre_open_ledger.py tests/trading/test_pre_open_ledger_semantics.py tests/trading/test_engine_pre_open_gate.py tests/trading/test_e2e_trading_flow.py tests/trading/test_pre_open_l1_halt.py -v`
Expected: 全绿。

- [ ] **Step 5: Commit**

```bash
git add trading/phases/ trading/engine.py
git commit -m "refactor(engine): T1-6 抽 phases/pre_open.py（集群 E 盘前挂单）"
```

---

## Task 7: 抽 phases/stop_loss.py（集群 F · 最大 695 行）

**Files:**
- Create: `trading/phases/stop_loss.py`
- Modify: `trading/engine.py`

**Interfaces:**
- Produces: `phases.stop_loss.stop_loss_monitor(...)`、`scan_expired_positions(today, max_holding)`、`close_expired_positions(gw, expired)`。
- Consumes: gateway（`gw.query_stock_positions`/`gw.get_quotes`）、state_store、`strategies.compute_stop_price`。

**移出符号**：`stop_loss_monitor`（L1034，~380 行，含内嵌 `_stop_already_placed`/`_record_stop` 闭包）+ `_scan_expired_positions`（L1416）+ `_close_expired_positions`（L1454）。类方法 `_stoploss`（L2862，wrapper，含 job_ledger + critical_guard）**留 engine**。

> 闭包 `_stop_already_placed`/`_record_stop` 随 `stop_loss_monitor` 一并搬入（它们是 stop_loss_monitor 内部 def，整体移动）。`stop_loss_monitor` signature 较长（深剖 §1.1：`stop_loss_monitor(...)`），保持原参数列表不变。

- [ ] **Step 1: 跑基线**

Run: `PYTHONUTF8=1 .venv310/Scripts/python.exe -m pytest tests/trading/test_stop_loss.py tests/trading/test_stop_loss_l1_halt.py tests/trading/test_stop_loss_monitor_decide_exit.py tests/trading/test_engine_stoploss_inject.py tests/trading/test_stoploss_post_close_gate.py -v`
Expected: 全绿。

- [ ] **Step 2: 创建 trading/phases/stop_loss.py，移入 3 符号（含闭包）**

逐字移入，`_scan_expired_positions`/`_close_expired_positions` 去 `_` 前缀（`scan_expired_positions`/`close_expired_positions`）。

- [ ] **Step 3: engine.py re-export + _stoploss wrapper 改调用**

```python
from trading.phases.stop_loss import stop_loss_monitor  # noqa: F401
```
`TradingEngine._stoploss` wrapper 内调 `stop_loss_monitor(...)` 不变（经 re-export）。

- [ ] **Step 4: 跑回归门**

Run: `PYTHONUTF8=1 .venv310/Scripts/python.exe -m pytest tests/trading/test_stop_loss.py tests/trading/test_stop_loss_l1_halt.py tests/trading/test_stop_loss_monitor_decide_exit.py tests/trading/test_engine_stoploss_inject.py tests/trading/test_stoploss_post_close_gate.py -v`
Expected: 全绿。

- [ ] **Step 5: Commit**

```bash
git add trading/phases/stop_loss.py trading/engine.py
git commit -m "refactor(engine): T1-7 抽 phases/stop_loss.py（集群 F 盘中止损，695 行）"
```

---

## Task 8: 抽 phases/post_close.py（集群 G）

**Files:**
- Create: `trading/phases/post_close.py`
- Modify: `trading/engine.py`

**Interfaces:**
- Produces: `phases.post_close.post_close(ports, ...)`、`seq_for_real_oid(gw, real_oid)`、`order_state_to_db(state)`。
- Consumes: `EnginePorts`（whitelist_clear）、state_store（`get_start_equity` 熔断基线）、gateway。

**移出符号**：`post_close`（L1564，~290 行）+ `_seq_for_real_oid`（L1857）+ `_order_state_to_db`（L1870）。类方法 `_post_close`（L3027，wrapper）**留 engine**。

> `post_close` 原经 `_ACTIVE_ENGINE` 清空 whitelist，Task 1 已改经 `ports.whitelist_clear()`。本 Task signature `async def post_close(ports, ...)`。

- [ ] **Step 1: 跑基线**

Run: `PYTHONUTF8=1 .venv310/Scripts/python.exe -m pytest tests/trading/test_post_close_reconcile.py tests/trading/test_circuit_breaker.py tests/trading/test_stoploss_post_close_gate.py tests/trading/test_reconcile_job.py -v`
Expected: 全绿。

- [ ] **Step 2: 创建 trading/phases/post_close.py，移入 3 符号**

逐字移入，`_seq_for_real_oid`/`_order_state_to_db` 去 `_` 前缀。

- [ ] **Step 3: engine.py re-export + _post_close wrapper 改调用**

```python
from trading.phases.post_close import post_close  # noqa: F401
```
`TradingEngine._post_close` wrapper 改调 `post_close(self._ports, <date>)`。

- [ ] **Step 4: 跑回归门**

Run: `PYTHONUTF8=1 .venv310/Scripts/python.exe -m pytest tests/trading/test_post_close_reconcile.py tests/trading/test_circuit_breaker.py tests/trading/test_stoploss_post_close_gate.py tests/trading/test_reconcile_job.py -v`
Expected: 全绿。

- [ ] **Step 5: Commit**

```bash
git add trading/phases/post_close.py trading/engine.py
git commit -m "refactor(engine): T1-8 抽 phases/post_close.py（集群 G 盘后对账/熔断）"
```

---

## Task 9: 抽 phases/exit.py（集群 H · 收口 order_state 的 take_profit 耦合）

**Files:**
- Create: `trading/phases/exit.py`
- Modify: `trading/engine.py`、`trading/order_state.py`

**Interfaces:**
- Produces: `phases.exit.place_take_profit(...)`、`placed(...)`/`record_tp(...)`（内嵌闭包）。
- Consumes: state_store（`has_order(TP1)` 幂等）、gateway。

**移出符号**：`place_take_profit`（L1882，模块级，含 `_placed`/`_record_tp`）+ 类方法 `_place_take_profit`（L3378，5 行薄 wrapper，删——直接用模块级）。

> **收口点**：Task 3 的 `order_state.handle_order_update` 临时经 engine 引用调 `_place_take_profit`。本 Task 迁 H 后，`handle_order_update` 改为直接 `from trading.phases.exit import place_take_profit`，删除 engine 引用耦合（order_state → phases.exit 单向）。

- [ ] **Step 1: 跑基线**

Run: `PYTHONUTF8=1 .venv310/Scripts/python.exe -m pytest tests/trading/test_engine_order_update_handler.py tests/trading/test_fill_db_contract.py -v`
Expected: 全绿。

- [ ] **Step 2: 创建 trading/phases/exit.py，移入 place_take_profit（含闭包）**

逐字移入。

- [ ] **Step 3: order_state.py 收口——直接 import phases.exit**

`trading/order_state.py` 顶部加 `from trading.phases.exit import place_take_profit`，`handle_order_update` 的 trade 分支调 `place_take_profit(...)` 不再经 engine 引用。

- [ ] **Step 4: engine.py re-export + 删类方法 _place_take_profit**

```python
from trading.phases.exit import place_take_profit  # noqa: F401
```
删 `TradingEngine._place_take_profit`（L3378），调用方改用模块级。

- [ ] **Step 5: 跑回归门**

Run: `PYTHONUTF8=1 .venv310/Scripts/python.exe -m pytest tests/trading/test_engine_order_update_handler.py tests/trading/test_fill_db_contract.py tests/trading/test_query_trades_db.py -v`
Expected: 全绿。

- [ ] **Step 6: Commit**

```bash
git add trading/phases/exit.py trading/order_state.py trading/engine.py
git commit -m "refactor(engine): T1-9 抽 phases/exit.py（集群 H 止盈）+ 收口 order_state 耦合"
```

---

## Task 10: 行为等价终验（e2e 长周期双跑）+ 文档同步

**Files:**
- Modify: `docs/architecture/02-module-dependencies.md`、`06-tech-debt.md`、`deep-dives/engine-current-state.md`（更新「最近复核」+ engine god module 项移除/降级）

- [ ] **Step 1: 全量回归（trading 全部单测）**

Run: `PYTHONUTF8=1 .venv310/Scripts/python.exe -m pytest tests/trading -q`
Expected: 全绿（60+ 单测）。

- [ ] **Step 2: e2e 长周期回归（行为等价终验）**

Run: `PYTHONUTF8=1 .venv310/Scripts/python.exe -m pytest tests/e2e_long_cycle -q`
Expected: 全绿（深剖 §0 摘要的长周期时序回放：23 交易日全流程，4 类校验）。

- [ ] **Step 3: 验收检查（engine 行数 + _ACTIVE_ENGINE 清零 + API 兼容）**

```bash
wc -l trading/engine.py                     # 期望 ≤ ~900（仅集群 J+C）
grep -rn "_ACTIVE_ENGINE" trading/          # 期望无输出
grep -rn "from trading.engine import" broadcast/ presentation/ trading/orchestrate/  # 消费方零改动验证
```

- [ ] **Step 4: 同步架构文档**

- `06-tech-debt.md`：engine god module（Critical）项移除或标「T1 完成」；刷新「最近复核」→ 2026-08-10。
- `02-module-dependencies.md`：trading 内部依赖图更新（engine → 8 子模块）。
- `deep-dives/engine-current-state.md`：顶部「最近复核」刷新 + 标注「T1 已拆分，本文档转为历史态/更新为目标态」。
- `plans/wayfinder/T1.md`：status: open → done。

- [ ] **Step 5: Commit**

```bash
git add docs/ plans/wayfinder/T1.md
git commit -m "docs(t1): engine 拆分完成——架构文档同步 + T1 工单闭合"
```

---

## Self-Review

**Spec coverage**：spec §2 的 8 外迁文件 → Task 1-9 全覆盖（ports/critical/order_state/data_ctx/eod_plan/phases×4）；spec §3 EnginePorts → Task 1；spec §4 的 10 step 顺序 → Task 1-10 一一对应；spec §5 行为等价 → 每 Task 回归门 + Task 10 终验；spec §7 边界（不做 T2/T6/broker/策略）→ plan 未涉及。✅

**Placeholder scan**：无 TBD/TODO；每 Task 有精确符号清单（引深剖行号）+ re-export 代码 + 回归命令。Task 1 Step 5/6 的 `_dynamic_whitelist` mutate 语义留了「实现时核对真实类型」的指引——这是必要的运行时核对（lambda 体依赖其真实 set/dict 形态），非占位。✅

**Type consistency**：`EnginePorts(gate, whitelist_add, whitelist_clear)` 三字段在 Task 1 定义、Task 6/8 消费（phases.pre_open/post_close 接 `ports`）；`pre_open`/`post_close`/`stop_loss_monitor`/`eod_plan`/`place_take_profit` 函数名跨 Task 一致（re-export 用旧名兼容）。✅

**已知执行风险**（实现时关注）：
1. `_handle_order_update`/`_halt` 原是实例方法（访问 self），Task 3/2 改为接收 engine 引用/free function + 回调——实现时核对每个 `self.xxx` 访问点，确保改写后语义不变（Task 3 Step 2、Task 2 Step 2 已标注）。
2. `_dynamic_whitelist` 真实类型（set/dict）决定 Task 1 的 lambda 体——先 grep 确认再写。
3. 循环 import：phases/ 不 import engine；order_state → phases.exit（Task 9）；engine → 所有子模块（单向）。若遇循环，用延迟 import 或 TYPE_CHECKING。
