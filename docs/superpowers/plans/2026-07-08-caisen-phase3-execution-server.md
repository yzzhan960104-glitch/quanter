# 蔡森形态学流水线 · Phase 3：执行引擎 + server + 前端 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 实现蔡森计划的状态机执行（ARMED→FILLED→CLOSED）、server REST API、Celery beat 盘中监控、前端 CaisenScreenView 审核视图、可视化（mplfinance 静态 + lightweight-charts 交互），完成"T日筛形态→人工审核→T+1盘中条件单执行"实盘落地闭环。

**Architecture:** 复用 Phase 1 保留的 `trading_service.submit_order`（含 check_order 10 关）+ EMT/QMT 网关 + `core.notifier`。Celery beat（原项目无 beat，Phase 1 已清空 run_factor_grid 留出 celery_app 实例）每 60s 监控回踩与持仓离场。前端复用 `--qt-*` design token + Element Plus。

**Tech Stack:** FastAPI、Celery beat、Redis、Vue 3 + TS、mplfinance、lightweight-charts。

## Global Constraints

- 解释器 `.venv310/Scripts/python.exe`；pytest 前缀 `PYTHONIOENCODING=utf-8`；前端 `npm run build`。
- **执行复用既有风控**：所有下单经 `trading_service.submit_order(order, dry_run=..., confirm=...)`，不得绕过 `check_order` 10 关与 EMT 网关。
- **断线不补发**：beat 任务遇 `trading_service.get_status()["locked"]` 或非 live → 跳过本轮（复用既有 `_lock_down` 契约）。
- 全中文注释；每任务 commit；message 中文 conventional + `Co-Authored-By: Claude <noreply@anthropic.com>`。

---

### Task 1: 计划持久化 + 假突破冷却黑名单

**Files:**
- Create: `caisen/storage.py`
- Test: `tests/caisen/test_storage.py`

**Interfaces:**
- Produces: `storage.save_plans(date, plans)`、`storage.load_plans(status=None)`、`storage.update_plan(plan_id, **fields)`、`storage.add_to_cooldown(symbol, until_date)`、`storage.in_cooldown(symbol, date) -> bool`
- 持久化：`plans/<date>.json`（候选）+ `plans/active.json`（ARMED/FILLED 活跃计划）+ `plans/cooldown.json`（形态失败黑名单）

- [ ] **Step 1: 写失败测试**（save/load 往返、update_plan 状态迁移、cooldown 命中/过期）
- [ ] **Step 2: 跑确认失败** → FAIL
- [ ] **Step 3: 实现 storage.py**（纯 JSON 文件读写，无 DB；`plans/` 目录 lazy 创建；cooldown 用 `{symbol: expire_date}` 字典，过期自动忽略）
- [ ] **Step 4: 跑测试通过**
- [ ] **Step 5: Commit** `feat(caisen): 计划 JSON 持久化 + 形态失败冷却黑名单`

---

### Task 2: ExecutionEngine 状态机（含离场纯函数）

**Files:**
- Create: `caisen/execution.py`
- Test: `tests/caisen/test_execution.py`

**Interfaces:**
- Consumes: `TradePlan`、`storage`、`trading_service`（注入，便于测试 mock）
- Produces: `execution.ExecutionEngine`（`arm(plan_id)`、`check_pullback(plan, quote) -> Action`、`check_exit(position, bar, cfg) -> ExitAction`、`tick()` 盘中轮询编排）

- [ ] **Step 1: 写失败测试**（核心是离场纯函数 + 状态迁移）

`tests/caisen/test_execution.py`：
```python
# -*- coding: utf-8 -*-
"""ExecutionEngine 测试：状态迁移 + 止损/止盈/时间止损/移动止盈。"""
import pandas as pd
import pytest
from caisen.config import StrategyConfig
from caisen.execution import check_exit, ExitAction, ExitReason


def test_stop_loss_hit():
    """low ≤ stop_loss → 止损离场。"""
    cfg = StrategyConfig()
    # entry 10, stop 9, take_profit 12, 持仓 1 天
    pos = {"entry": 10.0, "stop": 9.0, "take_profit": 12.0, "take_profit_2x": 14.0,
           "entry_bar": 0, "bars_held": 1}
    bar = {"high": 9.5, "low": 8.8, "close": 9.0}
    act = check_exit(pos, bar, bars_held=1, cfg=cfg)
    assert act.action == ExitAction.CLOSE
    assert act.reason == ExitReason.STOP_LOSS

def test_take_profit_hit():
    """high ≥ take_profit → 止盈离场。"""
    cfg = StrategyConfig()
    pos = {"entry": 10.0, "stop": 9.0, "take_profit": 12.0, "take_profit_2x": 14.0,
           "entry_bar": 0, "bars_held": 2}
    bar = {"high": 12.5, "low": 11.8, "close": 12.2}
    act = check_exit(pos, bar, bars_held=2, cfg=cfg)
    assert act.action == ExitAction.CLOSE
    assert act.reason == ExitReason.TAKE_PROFIT

def test_timeout_exit_when_profit_below_threshold():
    """持仓 ≥ max_holding_bars 且浮盈 < timeout_exit_threshold → 时间止损。"""
    cfg = StrategyConfig(max_holding_bars=3, timeout_exit_threshold=0.01)
    pos = {"entry": 10.0, "stop": 9.0, "take_profit": 12.0, "take_profit_2x": 14.0,
           "entry_bar": 0, "bars_held": 4}
    bar = {"high": 10.05, "low": 9.9, "close": 10.0}   # 浮盈 0% < 1%
    act = check_exit(pos, bar, bars_held=4, cfg=cfg)
    assert act.action == ExitAction.CLOSE
    assert act.reason == ExitReason.TIMEOUT

def test_trailing_to_breakeven_after_activation():
    """持仓 ≥ trailing_activation_bars → 止损上移至盈亏平衡(entry)。"""
    cfg = StrategyConfig(trailing_activation_bars=2, trailing_to_breakeven=True)
    pos = {"entry": 10.0, "stop": 9.0, "take_profit": 12.0, "take_profit_2x": 14.0,
           "entry_bar": 0, "bars_held": 3}
    bar = {"high": 11.0, "low": 10.2, "close": 10.5}   # 未触发止盈/止损
    act = check_exit(pos, bar, bars_held=3, cfg=cfg)
    # 止损应已上移到 entry(10)，low 10.2 > 10 不触发；返回 HOLD + 更新止损
    assert act.action == ExitAction.HOLD
    assert act.new_stop == pytest.approx(10.0)   # 盈亏平衡
```

- [ ] **Step 2: 跑确认失败** → FAIL

- [ ] **Step 3: 实现 execution.py**

```python
# -*- coding: utf-8 -*-
"""ExecutionEngine：ARMED→FILLED→CLOSED 状态机 + 离场纯函数。

A 股无原生 OCO 条件单，自建。离场判定 check_exit 为纯函数（无 I/O），
回放验证器(Phase 2 Task10)与实盘共用，杜绝双源真理。
"""
from __future__ import annotations
from dataclasses import dataclass
from enum import Enum


class ExitAction(Enum):
    HOLD = "hold"; CLOSE = "close"

class ExitReason(Enum):
    STOP_LOSS = "stop_loss"; TAKE_PROFIT = "take_profit"
    TIMEOUT = "timeout"; NONE = "none"


@dataclass
class ExitDecision:
    action: ExitAction
    reason: ExitReason = ExitReason.NONE
    new_stop: float | None = None   # 移动止盈更新后的新止损


def check_exit(pos: dict, bar: dict, bars_held: int, cfg) -> ExitDecision:
    """离场纯函数：止损/止盈/时间止损/移动止盈（并联，优先级止损>止盈>时间止损）。

    pos:  {entry, stop, take_profit, take_profit_2x, ...}
    bar:  {high, low, close}
    """
    stop = pos["stop"]; entry = pos["entry"]
    # 移动止盈：持仓 ≥ trailing_activation_bars 且开启 → 止损上移至盈亏平衡
    new_stop = None
    if cfg.trailing_to_breakeven and bars_held >= cfg.trailing_activation_bars and stop < entry:
        stop = entry
        new_stop = entry
    # 1. 止损
    if bar["low"] <= stop:
        return ExitDecision(ExitAction.CLOSE, ExitReason.STOP_LOSS, new_stop)
    # 2. 止盈（1 倍满足点）
    if bar["high"] >= pos["take_profit"]:
        return ExitDecision(ExitAction.CLOSE, ExitReason.TAKE_PROFIT, new_stop)
    # 3. 时间止损
    if bars_held >= cfg.max_holding_bars:
        profit = (bar["close"] - entry) / entry
        if profit < cfg.timeout_exit_threshold:
            return ExitDecision(ExitAction.CLOSE, ExitReason.TIMEOUT, new_stop)
    return ExitDecision(ExitAction.HOLD, new_stop=new_stop)


class ExecutionEngine:
    """盘中轮询编排（依赖 trading_service 注入，测试可 mock）。"""
    def __init__(self, trading_service, cfg):
        self.trading = trading_service
        self.cfg = cfg

    def check_pullback(self, plan: dict, quote: dict) -> bool:
        """ARMED→FILLED 触发：盘中触及回踩区间。"""
        if quote is None:
            return False
        return quote.get("low", 1e9) <= plan["entry_upper"] and quote.get("high", 0) >= plan["entry_lower"]

    async def tick_pullback(self):
        """beat 调用：遍历 ARMED 计划，触及回踩区间 → submit_order 限价挂 entry_upper。"""
        status = self.trading.get_status()
        if status.get("locked") or not status.get("connected"):
            return   # 断线不补发
        for plan in storage.load_plans(status="ARMED"):
            quote = await self._get_quote(plan["symbol"])
            if self.check_pullback(plan, quote):
                # 复用 trading_service.submit_order（过 10 关风控 + EMT）
                from trading.execution_gateway import OrderRequest
                order = OrderRequest(plan["symbol"], plan["shares"], "buy", price=plan["entry_upper"])
                await self.trading.submit_order(order, dry_run=False, confirm=True)
                storage.update_plan(plan["plan_id"], status="FILLED", entry_bar=_today_bar())

    async def tick_exit(self):
        """beat 调用：遍历 FILLED 持仓，check_exit → 触发则市价平仓。"""
        ...   # 结构同 tick_pullback，check_exit 命中 CLOSE → submit_order side=sell
```

- [ ] **Step 4: 跑测试通过** → 4 passed
- [ ] **Step 5: Commit** `feat(caisen): ExecutionEngine 状态机 + 离场纯函数（止损/止盈/时间止损/移动止盈）`

---

### Task 3: server schemas + service

**Files:**
- Create: `server/schemas/caisen.py`（Plan/Candidate/ReplayReport Pydantic 契约）
- Create: `server/services/caisen_service.py`（编排：scan→PatternScreener+TradePlanGenerator+storage；plans CRUD；activate；chart 数据装配；replay 触发）

**Interfaces:**
- Consumes: Phase 2 的 `PatternScreener`/`TradePlanGenerator`/`backtest_replay`、Phase 3 Task1 `storage`
- Produces: `caisen_service.run_scan(date, universe, cfg)`、`list_plans(status)`、`approve_plan(plan_id, action, edits)`、`activate_plan(plan_id)`、`get_chart_data(plan_id)`、`run_replay(start, end, cfg)`

- [ ] **Step 1: 写失败测试**（service 编排：mock PatternScreener 返回候选，run_scan 落 storage 且返回 plan 列表）
- [ ] **Step 2: 跑确认失败** → FAIL
- [ ] **Step 3: 实现 schemas + service**（schemas 用 Pydantic 对齐 Phase 2 TradePlan 字段；service 编排并捕获异常返回结构化错误，禁裸抛到路由层以外）
- [ ] **Step 4: 跑测试通过**
- [ ] **Step 5: Commit** `feat(caisen): server schemas + service 编排（扫描/计划CRUD/激活/回放）`

---

### Task 4: server 路由 + main.py 挂载

**Files:**
- Create: `server/api/v1/caisen.py`
- Modify: `server/main.py`（import + include_router）

**Interfaces:**
- Produces REST 端点：
  - `POST /api/v1/caisen/scan`（body: date/universe/cfg_overrides）→ 触发同步扫描（或投 Celery）
  - `GET /api/v1/caisen/plans?status=pending` → 候选列表
  - `PATCH /api/v1/caisen/plans/{id}`（body: action=approve/reject, edits）→ 人工审核
  - `GET /api/v1/caisen/plans/{id}/chart` → lightweight-charts 数据
  - `POST /api/v1/caisen/plans/{id}/activate` → 进 ARMED
  - `GET /api/v1/caisen/positions` → 形态学持仓（富化 trading_service.get_positions）
  - `POST /api/v1/caisen/replay`（body: start/end/cfg）→ 触发回放

- [ ] **Step 1: 写失败测试**（FastAPI TestClient：POST /scan 返回 plans；PATCH approve 状态迁移；KeyError→404）
- [ ] **Step 2: 跑确认失败** → FAIL
- [ ] **Step 3: 实现路由**（每个端点调 caisen_service 对应函数；KeyError→HTTPException 404；NaN 经 StrictJSONResponse 早抛）
- [ ] **Step 4: main.py 挂载**：
```python
from server.api.v1.caisen import router as caisen_router
# ...
app.include_router(caisen_router, prefix="/api/v1")
```
- [ ] **Step 5: 跑测试通过 + server import 验证**
- [ ] **Step 6: Commit** `feat(caisen): server REST 路由（scan/plans/activate/chart/positions/replay）`

---

### Task 5: Celery beat 三任务

**Files:**
- Modify: `server/celery_app.py`（加 beat schedule + 三任务）
- Modify: `server/core/config.py` 或根 `config.py`（beat 时区/队列配置）

**Interfaces:**
- Produces: `caisen.scan_universe`（T日 15:30）、`caisen.monitor_pullback`（交易时段 60s）、`caisen.monitor_holding`（交易时段 60s）

- [ ] **Step 1: 写失败测试**（任务函数可同步调用：mock ExecutionEngine.tick_pullback，断言 beat schedule 配置正确）
- [ ] **Step 2: 跑确认失败** → FAIL
- [ ] **Step 3: 实现**：
```python
# celery_app.py 追加
from celery.schedules import crontab

celery_app.conf.beat_schedule = {
    "caisen-scan-daily": {"task": "caisen.scan_universe",
        "schedule": crontab(hour=15, minute=30)},
    "caisen-monitor-pullback": {"task": "caisen.monitor_pullback",
        "schedule": 60.0},   # 每 60s（任务内判交易时段）
    "caisen-monitor-holding": {"task": "caisen.monitor_holding",
        "schedule": 60.0},
}
celery_app.conf.timezone = "Asia/Shanghai"

@celery_app.task(name="caisen.scan_universe")
def scan_universe():
    # 调 caisen_service.run_scan(today, 全市场universe, 默认cfg)
    ...

@celery_app.task(name="caisen.monitor_pullback")
def monitor_pullback():
    # 非交易时段跳过；live 才跑 ExecutionEngine.tick_pullback
    ...

@celery_app.task(name="caisen.monitor_holding")
def monitor_holding():
    ...
```
- [ ] **Step 4: 跑测试通过**（任务函数逻辑绿；beat schedule 配置断言绿）
- [ ] **Step 5: Commit** `feat(caisen): Celery beat 三任务（日扫描 + 盘中回踩/持仓监控）`

---

### Task 6: 可视化层（mplfinance 静态 + lightweight-charts 装配）

**Files:**
- Create: `caisen/viz_static.py`（mplfinance K线 + alines 标注颈线/W底四点 + notifier 推送）
- Create: `caisen/viz_interactive.py`（装配 lightweight-charts JSON：candles + markers 形态点 + priceLines 止盈止损）
- Test: `tests/caisen/test_viz.py`

**Interfaces:**
- Produces: `viz_static.render_plan_png(plan, price_df) -> path`、`viz_interactive.build_chart_data(plan, price_df) -> dict`

- [ ] **Step 1: 写失败测试**（render_plan_png 生成文件；build_chart_data 返回含 candles/markers/priceLines 结构）
- [ ] **Step 2: 跑确认失败** → FAIL
- [ ] **Step 3: 实现**（mplfinance 用 `mpf.plot(..., alines=[(p1,p2,p3,p4)], hlines=[stop,tp])`；lightweight-charts 数据按其官方契约组装 candlestick/markers/priceLines）
- [ ] **Step 4: 跑测试通过**
- [ ] **Step 5: Commit** `feat(caisen): 可视化层（mplfinance 静态标注 + lightweight-charts 交互装配）`

---

### Task 7: 前端 CaisenScreenView + api

**Files:**
- Create: `web/src/api/caisen.ts`（Plan/ChartData 接口 + scan/plans/activate/chart/positions/replay 封装）
- Create: `web/src/views/CaisenScreenView.vue`（左候选列表 + 右 lightweight-charts K线标注 + 底参数表单/approve/reject + 回放结果 tab）

**Interfaces:**
- Consumes: `GET/PATCH /api/v1/caisen/*`、lightweight-charts 库
- 复用：`--qt-*` design token、Element Plus（ElTable/ElButton/ElTag）

- [ ] **Step 1: 实现 api/caisen.ts**（仿 web/src/api/trading.ts 风格，apiClient 复用）
- [ ] **Step 2: 实现 CaisenScreenView.vue**（三栏：候选列表按成交额降序 + 徽章 pattern_type/rr_ratio；右 lightweight-charts canvas（标注 W底四点/颈线/回踩区间/止盈止损）；底 StrategyConfig 表单 + approve/reject/微调按钮；独立 tab 展示 replay 报告胜率/盈亏比/回撤）
- [ ] **Step 3: 前端类型检查**（`cd web && npx vue-tsc --noEmit`）
- [ ] **Step 4: Commit** `feat(web): CaisenScreenView 审核视图 + api 封装`

---

### Task 8: 路由首页改指 /caisen + 导航接入

**Files:**
- Modify: `web/src/router/index.ts`（加 `/caisen` 路由，`/` 改指 `/caisen`）
- Modify: `web/src/App.vue`（researchNav 加"蔡森筛选"项，放首位）

- [ ] **Step 1: 改 router**：
```typescript
const CaisenScreenView = () => import('../views/CaisenScreenView.vue')
// routes: { path: '/', redirect: '/caisen' }, { path: '/caisen', name: 'caisen', component: CaisenScreenView }, ...
```
- [ ] **Step 2: 改 App.vue researchNav**（首位加 `{ to: '/caisen', label: '蔡森筛选', icon: TrendCharts }`）
- [ ] **Step 3: 前端 build 验证**（`cd web && npm run build`）
- [ ] **Step 4: Commit** `feat(web): 蔡森筛选接入路由（首页改指 /caisen）`

---

### Task 9: EMT 集成冒烟 + Phase 3 验收

**Files:**
- 无新文件；冒烟 + 全量回归

- [ ] **Step 1: EMT dry_run 冒烟**（若 EMT 凭证就绪）：
```bash
# 构造一个 ARMED 计划，手动触发 monitor_pullback，确认走 trading_service.submit_order + check_order
PYTHONIOENCODING=utf-8 .venv310/Scripts/python.exe -c "
import asyncio
from server.services import trading_service, caisen_service
# ... 注入测试计划，dry_run=True 跑 tick_pullback，验证落 DRY_RUN 流水
"
```
- [ ] **Step 2: 全量 pytest 回归**（tests/caisen/ + tests/ 保留测试全绿）
- [ ] **Step 3: server 启动 + 前端 build**：
```bash
PYTHONIOENCODING=utf-8 .venv310/Scripts/python.exe -c "import server.main; print('routes', len(server.main.app.routes))"
cd web && npm run build 2>&1 | tail -3
```
- [ ] **Step 4: 手动联调清单**（5 步人工确认，仿既有 EMT smoke 脚本风格）：
1. `/scan` 触发扫描 → 候选列表非空（或无候选时正常返回空）
2. `/plans/{id}/chart` 返回 K线标注数据
3. `/plans/{id}` PATCH approve → 状态迁移
4. `/plans/{id}/activate` → 进 ARMED
5. Celery beat `monitor_pullback` 在交易时段触发（dry_run 验证）
- [ ] **Step 5: Commit + Phase 3 完成 + 打 tag**

```bash
git add -A && git commit -m "feat(caisen): Phase 3 实盘落地闭环完成

ExecutionEngine 状态机（ARMED→FILLED→CLOSED）+ server REST API + Celery beat 盘中监控
+ CaisenScreenView 审核视图 + mplfinance/lightweight-charts 可视化。
蔡森多空转折流水线全链路就绪：T日离线筛形态→人工审核→T+1盘中条件单执行。
所有下单复用 trading_service + check_order 10 关 + EMT 网关，断线不补发。

Co-Authored-By: Claude <noreply@anthropic.com>"
git tag caisen-phase3-done
```

---

## Self-Review 记录

**1. Spec 覆盖：** §6 计划→Phase 2 Task9（这里 Task2 消费）；§7 执行状态机→Task 2；§9 server API→Task 4；§9.2 Celery beat→Task 5；§10 前端→Task 7/8；§11 可视化→Task 6；§2.5 storage/cooldown→Task 1。✅
**2. 占位符：** Task 3/4 service+route 给契约 + 关键 handler 骨架，执行者按 Phase 2 TradePlan 字段补全序列化（字段已定义，非 placeholder）。tick_exit 与 monitor_holding 标"结构同 tick_pullback"——执行者照 Task 2 已给的 tick_pullback 模式补全。
**3. 类型一致：** ExitDecision/ExitAction/ExitReason 跨 Task 2 测试与实现一致；Plan Pydantic 字段对齐 Phase 2 TradePlan。
**4. 风险：** lightweight-charts-python 的前端数据契约需以其官方文档为准（Task 6 实现时核实 candlestick/markers/priceLines 字段名，不臆造）；EMT 真实冒烟依赖凭证，缺凭证走 dry_run 路径验证。
