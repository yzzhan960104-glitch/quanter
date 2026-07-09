# 蔡森形态学流水线 · Phase 1：删除清理 + CreditRegime 迁移 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 删除 backtest/factors/strategies 三大模块及相关 server/前端/测试/配置耦合，将 CreditRegime 迁移到 `core/`，产出"干净专精底盘"——server 可启动、保留测试全绿、前端可 build。

**Architecture:** 删除型重构。每个任务删除一个模块 + 清理其耦合（main.py 路由/scan、config、前端、测试），每任务后 server 必须仍可启动、保留测试仍绿。TDD 在删除任务中体现为"迁移先于删除、保留测试为回归 gate"。

**Tech Stack:** Python 3.10 venv (`.venv310`)、FastAPI、pytest、Vue 3 + TypeScript + Element Plus。

## Global Constraints

- Python 解释器固定用 `.venv310/Scripts/python.exe`，pytest 用 `.venv310/Scripts/python.exe -m pytest`。
- Windows git bash 环境：路径用正斜杠，pytest 命令前缀 `PYTHONIOENCODING=utf-8` 规避 GBK 控制台编码错（既有 commit 已立此规）。
- 每个任务结束必须满足两个 gate：(1) `.venv310/Scripts/python.exe -m pytest tests/ -q` 不出现 ImportError/CollectionError（保留测试可被收集并绿）；(2) `uvicorn server.main:app` 能 import 成功（用 `python -c "import server.main"` 验证）。
- 全中文注释（CLAUDE.md）；删除文件时其注释一并消失，无需迁移注释。
- 频繁 commit：每个任务结束 commit 一次，commit message 中文 conventional 风格，结尾带 `Co-Authored-By: Claude <noreply@anthropic.com>`。
- 严禁提交 `多空轉折一手抓.pdf` 及 `scripts/pages/`（已 .gitignore）。

## File Structure 概览

**删除（整体）：** `backtest/`、`strategies/`、`factors/`（除 `macro_regime.py` 先迁出）、`viz/report.py`、根 `test_hmm_macro.py`。
**删除（server）：** `server/api/v1/{backtest,strategies,factors,explorer,portfolio}.py`、`server/services/{backtest,strategy,factor,portfolio}_service.py`、`server/schemas/{backtest,strategy,factor,portfolio}.py`。
**迁移：** `factors/macro_regime.py` → `core/macro_regime.py`。
**修改：** `server/main.py`（清 import/scan/路由）、`server/celery_app.py`（删 run_factor_grid）、根 `config.py`（删 BACKTEST_CONFIG/FACTOR_CONFIG）、`server/core/config.py`（删 BACKTEST_DEFAULTS/PORTFOLIO_DEFAULTS/API_CONFIG）、前端 `App.vue`/`router/index.ts`（清导航/路由 + 删视图/api/组件）。
**删除测试：** test_backtest*、test_engine_*、test_strategy、test_mytt、test_factors、test_factor_analyzer、test_exploratory_momentum、test_micro_momentum、test_factor_grid_payload、test_explorer_api、test_portfolio_nan_regression。
**保留（核心，不动）：** `trading/`、`data/`、`core/notifier.py`、`server/api/v1/{macro,trading,data,review,logs}.py`、`server/services/trading_service.py`、前端 LiveCockpitView/DashboardView/DataLakeView/ReviewView。

---

### Task 1: CreditRegime 迁移到 core/

**Files:**
- Move: `factors/macro_regime.py` → `core/macro_regime.py`
- Modify: `trading/execution_gateway.py`（MacroAwareGateway 间接用，无直接 import——核实）、`server/api/v1/macro.py`、`scripts/sync_macro_credit.py`
- Modify: `tests/test_macro_regime.py`、`tests/test_sync_macro_credit.py`、`tests/test_execution_gateway_veto.py`

**Interfaces:**
- Produces: `core.macro_regime.CreditRegime`（类名/方法不变：`compute(date)->int`、`get_default()`、`history(n)`）
- Consumes: 无（CreditRegime 仅依赖 `data.lake_reader` + pandas）

- [ ] **Step 1: 确认基线——迁移前相关测试当前绿**

Run: `PYTHONIOENCODING=utf-8 .venv310/Scripts/python.exe -m pytest tests/test_macro_regime.py tests/test_sync_macro_credit.py tests/test_execution_gateway_veto.py -q`
Expected: PASS（若已有失败，先记录，迁移后须不引入新失败）。

- [ ] **Step 2: grep 定位所有 import CreditRegime 的位置**

Run: `grep -rn "macro_regime\|CreditRegime" --include=*.py server/ scripts/ tests/ trading/ | grep -v "factors/macro_regime.py"`
Expected: 列出 `server/api/v1/macro.py`、`scripts/sync_macro_credit.py`、`tests/test_macro_regime.py`、`tests/test_sync_macro_credit.py`、`tests/test_execution_gateway_veto.py`（及任何其他命中）。

- [ ] **Step 3: git mv 迁移文件**

Run: `git mv factors/macro_regime.py core/macro_regime.py`
（git mv 保留历史；core/ 目录已存在，含 notifier.py。）

- [ ] **Step 4: 批量改 import 路径 `factors.macro_regime` → `core.macro_regime`**

对 Step 2 命中的每个文件，把 `from factors.macro_regime import` / `from factors import macro_regime` 改为 `from core.macro_regime import`。用 Edit 工具逐文件改（不要用 sed，避免误伤）。典型改动：

```python
# 改前
from factors.macro_regime import CreditRegime
# 改后
from core.macro_regime import CreditRegime
```

- [ ] **Step 5: 跑迁移测试验证无回归**

Run: `PYTHONIOENCODING=utf-8 .venv310/Scripts/python.exe -m pytest tests/test_macro_regime.py tests/test_sync_macro_credit.py tests/test_execution_gateway_veto.py -q`
Expected: PASS（与 Step 1 基线一致）。

- [ ] **Step 6: 验证 server 仍可 import**

Run: `PYTHONIOENCODING=utf-8 .venv310/Scripts/python.exe -c "import server.main"`
Expected: 无 ImportError。

- [ ] **Step 7: Commit**

```bash
git add -A && git commit -m "refactor(macro): CreditRegime 迁移 factors→core（蔡森底盘预备）

CreditRegime 是纯函数（仅依赖 data.lake_reader + pandas），与 FactorLoader 零耦合。
为后续删除整个 factors/ 做准备，先迁移到 core/macro_regime.py。
import 路径同步更新：server/api/macro、sync_macro_credit、3 个相关测试。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 2: 删除 strategies 模块 + 清理 server 耦合

**Files:**
- Delete: `strategies/`（整个目录）、`server/services/strategy_service.py`、`server/api/v1/strategies.py`、`server/schemas/strategy.py`、`tests/test_strategy.py`
- Modify: `server/main.py`（删 StrategyLoader import + scan + strategies_router import + 挂载）

**Interfaces:**
- Consumes: Task 1（CreditRegime 已迁，strategies 不再被 core 依赖）
- Produces: 无（strategies 整体移除）

- [ ] **Step 1: 确认无保留代码 import strategies**

Run: `grep -rn "import strategies\|from strategies" --include=*.py server/ trading/ data/ core/ scripts/ | grep -v "strategies/"`
Expected: 仅 `server/main.py`、`server/services/portfolio_service.py`（portfolio 将在 Task 5 整体删，此处暂留）、`server/services/strategy_service.py`（本任务删）。若命中其他保留模块，先处理该引用再删。

- [ ] **Step 2: 删除 strategies 后端文件**

Run: `git rm -r strategies/ server/services/strategy_service.py server/api/v1/strategies.py server/schemas/strategy.py tests/test_strategy.py`

- [ ] **Step 3: 清理 server/main.py 的 strategies 部分**

Edit `server/main.py`，删除三处：
1. 删 import 行：`from strategies.loader import StrategyLoader`（约 line 34）
2. 删 import 行：`from server.api.v1.strategies import router as strategies_router`（约 line 35）
3. lifespan 内删扫描块（约 line 60-63）：
```python
    # 启动：扫描策略注册到 app.state
    loader = StrategyLoader()
    loader.scan()
    app.state.strategy_loader = loader
```
4. 删路由挂载：`app.include_router(strategies_router, prefix="/api/v1")`（约 line 155）

- [ ] **Step 4: 验证 server 可 import（portfolio 仍临时引用 strategies，预期会报错——记录）**

Run: `PYTHONIOENCODING=utf-8 .venv310/Scripts/python.exe -c "import server.main" 2>&1 | tail -3`
Expected: 若 `portfolio_service.py` import strategies 报错——这是预期，Task 5 会删 portfolio。若报错不来自 portfolio，需回头处理。

- [ ] **Step 5: 跑保留测试（容忍 portfolio 相关 collection error，Task 5 修复）**

Run: `PYTHONIOENCODING=utf-8 .venv310/Scripts/python.exe -m pytest tests/ -q --co 2>&1 | tail -5`
Expected: 除 portfolio 相关外，无其他 ImportError。

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "refactor: 删除 strategies 多策略模块（蔡森专精化）

删除 strategies/、strategy_service、strategies 路由、schema、test_strategy。
main.py 清理 StrategyLoader scan 与路由挂载。
portfolio_service 仍临时引用 strategies，Task 5 整体删除 portfolio 时修复。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 3: 删除 factors 因子体系 + 清理 server/celery 耦合

**Files:**
- Delete: `factors/`（整个目录，macro_regime.py 已在 Task 1 迁出）、`server/services/factor_service.py`、`server/api/v1/factors.py`、`server/api/v1/explorer.py`、`server/schemas/factor.py`、相关测试（见 Step 1）
- Modify: `server/main.py`（删 FactorLoader import + scan + factors_router/explorer_router import + 挂载）、`server/celery_app.py`（删 run_factor_grid/run_factor_grid_impl）

**Interfaces:**
- Consumes: Task 1（macro_regime 已迁）
- Produces: 无

- [ ] **Step 1: 删除 factors 相关测试**

Run: `git rm tests/test_mytt.py tests/test_factors.py tests/test_factor_analyzer.py tests/test_exploratory_momentum.py tests/test_micro_momentum.py tests/test_factor_grid_payload.py tests/test_explorer_api.py`

- [ ] **Step 2: 删除 factors 后端 + explorer**

Run: `git rm -r factors/ server/services/factor_service.py server/api/v1/factors.py server/api/v1/explorer.py server/schemas/factor.py`

- [ ] **Step 3: 清理 server/main.py 的 factors/explorer 部分**

Edit `server/main.py`，删除：
1. import `from server.api.v1.explorer import router as explorer_router`（约 line 36）
2. import `from server.api.v1.factors import router as factors_router`（约 line 45）
3. lifespan 内 FactorLoader 块（约 line 65-71）：
```python
    # 启动：扫描因子注册表到 app.state（层级二·决策②）
    from factors.base import FactorLoader
    factor_loader = FactorLoader()
    factor_loader.scan()
    app.state.factor_loader = factor_loader
```
4. 路由挂载 `app.include_router(explorer_router, prefix="/api/v1")`（约 line 159）
5. 路由挂载 `app.include_router(factors_router, prefix="/api/v1")`（约 line 168）

- [ ] **Step 4: 清理 server/celery_app.py 的 run_factor_grid**

Edit `server/celery_app.py`：删除 `run_factor_grid_impl` 函数（约 line 30-121）与 `@celery_app.task def run_factor_grid`（约 line 124-139）。保留模块顶部 `celery_app = Celery(...)` 实例与 `celery_app.conf.task_default_queue`（Phase 3 蔡森 beat 复用）。删除后 `celery_app.py` 仅剩 Celery 实例装配与 import（`pandas`/`json`/`os` 若无其他用途一并删 import）。

- [ ] **Step 5: 验证 server 可 import（仍容忍 portfolio 报错）**

Run: `PYTHONIOENCODING=utf-8 .venv310/Scripts/python.exe -c "import server.main" 2>&1 | tail -3`
Expected: 仅 portfolio 相关报错（Task 5 修复），无 factors/explorer 报错。

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "refactor: 删除 factors 因子体系与 explorer 因子沙盒（蔡森专精化）

删除 factors/（macro_regime 已迁 core）、factor_service、factors/explorer 路由、schema、
7 个因子相关测试。main.py 清理 FactorLoader scan 与路由。celery_app 删 run_factor_grid，
保留 Celery 实例供 Phase 3 蔡森 beat 复用。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 4: 删除 backtest 模块 + 清理耦合

**Files:**
- Delete: `backtest/`、`server/services/backtest_service.py`、`server/api/v1/backtest.py`、`server/schemas/backtest.py`、`viz/report.py`、根 `test_hmm_macro.py`、相关测试
- Modify: `server/main.py`（删 backtest_router）、根 `config.py`（删 BACKTEST_CONFIG/FACTOR_CONFIG）、`server/core/config.py`（删 BACKTEST_DEFAULTS/PORTFOLIO_DEFAULTS/API_CONFIG）

**Interfaces:**
- Produces: 无

- [ ] **Step 1: 删除 backtest 相关测试**

Run: `git rm tests/test_backtest.py tests/test_backtest_serialize.py tests/test_backtest_stream.py tests/test_backtest_nan_regression.py tests/test_backtest_benchmark.py tests/test_backtest_schema.py tests/test_engine_events.py tests/test_engine_minute.py test_hmm_macro.py`

- [ ] **Step 2: 删除 backtest 后端 + viz/report**

Run: `git rm -r backtest/ server/services/backtest_service.py server/api/v1/backtest.py server/schemas/backtest.py viz/report.py`

- [ ] **Step 3: 清理 server/main.py 的 backtest 部分**

Edit `server/main.py`：
1. 删 import `from server.api.v1.backtest import router as backtest_router`（约 line 27）
2. 删路由挂载 `app.include_router(backtest_router, prefix="/api/v1")`（约 line 153）

- [ ] **Step 4: 清理根 config.py**

Edit `config.py`：删除 `BACKTEST_CONFIG = {...}`（约 line 77-85）与 `FACTOR_CONFIG = {...}`（约 line 88-94）两个字典。保留 `MACRO_CONFIG`（CreditRegime 用社融）、`LAKE_CONFIG`、`DATASET_REGISTRY` 等。

- [ ] **Step 5: 清理 server/core/config.py**

Edit `server/core/config.py`：删除 `BACKTEST_DEFAULTS`、`PORTFOLIO_DEFAULTS`、`API_CONFIG` 三个字典。保留 `DATA_DEFAULTS`、`CORS_ORIGINS`、`LOG_CONFIG`。同步删模块 docstring 中"回测引擎默认参数"相关行。

- [ ] **Step 6: 核实 viz/test_viz 是否纯回测依赖**

Run: `grep -n "import\|from" tests/test_viz.py | head -20`
判定：若 test_viz.py 仅 import viz.report（已删）或 backtest，则 `git rm tests/test_viz.py`；若也测 viz.interactive（保留），则 Edit 删除其中涉及 report/backtest 的测试函数，保留 interactive 部分。

- [ ] **Step 7: 验证 server 可 import（仍容忍 portfolio）**

Run: `PYTHONIOENCODING=utf-8 .venv310/Scripts/python.exe -c "import server.main" 2>&1 | tail -3`
Expected: 仅 portfolio 相关报错。

- [ ] **Step 8: Commit**

```bash
git add -A && git commit -m "refactor: 删除 backtest 通用回测引擎（蔡森专精化）

删除 backtest/、backtest_service/route/schema、viz/report、test_hmm_macro、8 个回测测试。
根 config 删 BACKTEST_CONFIG/FACTOR_CONFIG；server/core/config 删 BACKTEST/PORTFOLIO/API 默认值。
蔡森上线前验证由 Phase 2 专用回放验证器承担，不再需要通用回测引擎。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 5: 删除 portfolio（HMM 组合回测）+ 修复 server 启动

**Files:**
- Delete: `server/services/portfolio_service.py`、`server/api/v1/portfolio.py`、`server/schemas/portfolio.py`、`tests/test_portfolio_nan_regression.py`
- Modify: `server/main.py`（删 portfolio_router import + 挂载）

**Interfaces:**
- Produces: server 启动无 ImportError（portfolio 是最后一个引用已删模块的保留代码）

**说明：** portfolio_service 全文是 HMM 组合回测（import backtest.engine + strategies.hmm_macro_strategy），无持仓展示逻辑——持仓展示由 trading_service.get_positions + LiveCockpitView 承担。故 portfolio 整体删。

- [ ] **Step 1: 删除 portfolio**

Run: `git rm server/services/portfolio_service.py server/api/v1/portfolio.py server/schemas/portfolio.py tests/test_portfolio_nan_regression.py`

- [ ] **Step 2: 清理 server/main.py 的 portfolio 部分**

Edit `server/main.py`：
1. 删 import `from server.api.v1.portfolio import router as portfolio_router`（约 line 28）
2. 删路由挂载 `app.include_router(portfolio_router, prefix="/api/v1")`（约 line 154）

- [ ] **Step 3: 验证 server 可 import（应彻底无错）**

Run: `PYTHONIOENCODING=utf-8 .venv310/Scripts/python.exe -c "import server.main" && echo OK`
Expected: 输出 `OK`，无任何 ImportError。

- [ ] **Step 4: 跑全量保留测试**

Run: `PYTHONIOENCODING=utf-8 .venv310/Scripts/python.exe -m pytest tests/ -q 2>&1 | tail -15`
Expected: 全绿（残余 test_final_fixes/test_macro_api/test_akshare_north_dragon/test_sync_fundamentals 若失败，进入 Task 7 核实；其余必须绿）。

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "refactor: 删除 portfolio HMM 组合回测（蔡森专精化）

portfolio_service 全文是 HMM 组合回测（依赖已删的 backtest+strategies），无持仓展示逻辑。
持仓展示由 trading_service.get_positions + LiveCockpitView 承担，故 portfolio 整体删除。
server 启动至此无 ImportError，保留测试全绿。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 6: 前端清理（删回测/因子/策略视图与导航）

**Files:**
- Delete: `web/src/views/{TerminalView,BacktestView,ExplorerView,FactorManagerView,StrategyArchitectView}.vue`、`web/src/api/{backtest,explorer,factors,strategy}.ts`
- Delete（核实后）: `web/src/components/{ProChart,AttributionPanel,FactorMatrixCard,MetricCards,NavChart,ParamForm,StrategyParamForm,TerminalLogs,TerminalWatermark,UniverseCard,ExecutionPlanGraph}.vue`（仅删无保留视图引用的）
- Modify: `web/src/router/index.ts`（删路由 + import，`/` 临时指 DashboardView）、`web/src/App.vue`（清理 researchNav）、`web/src/composables/useTerminalState.ts`（若仅回测终端用则删）

**Interfaces:**
- Produces: 前端 `npm run build` 通过；导航只剩 宏观驾驶舱/数据湖/AI复盘/实盘中控 + 临时首页

- [ ] **Step 1: 核实哪些组件被保留视图引用**

Run: `cd web && grep -rln "PositionsTable\|DatasetTable" src/views/{DashboardView,DataLakeView,LiveCockpitView,ReviewView}.vue`
判定：PositionsTable/DatasetTable 若被保留视图引用则保留；其余回测/因子/策略组件（ProChart/AttributionPanel/FactorMatrixCard/MetricCards/NavChart/ParamForm/StrategyParamForm/TerminalLogs/TerminalWatermark/UniverseCard/ExecutionPlanGraph）核实后删。

Run: `cd web && grep -rln "useTerminalState" src/views/ src/App.vue`
判定：若仅 TerminalView 用，则 `useTerminalState.ts` 随 TerminalView 一起删。

- [ ] **Step 2: 删除回测/因子/策略视图与 api**

Run: `cd web && git rm src/views/TerminalView.vue src/views/BacktestView.vue src/views/ExplorerView.vue src/views/FactorManagerView.vue src/views/StrategyArchitectView.vue src/api/backtest.ts src/api/explorer.ts src/api/factors.ts src/api/strategy.ts`

- [ ] **Step 3: 删除 Step 1 核实为"无保留视图引用"的组件**

根据 Step 1 结果，删除不被 DashboardView/DataLakeView/LiveCockpitView/ReviewView 引用的组件。典型（待 Step 1 确认）：
Run: `cd web && git rm src/components/ProChart.vue src/components/AttributionPanel.vue src/components/FactorMatrixCard.vue src/components/MetricCards.vue src/components/NavChart.vue src/components/ParamForm.vue src/components/StrategyParamForm.vue src/components/TerminalLogs.vue src/components/TerminalWatermark.vue src/components/UniverseCard.vue src/components/ExecutionPlanGraph.vue src/composables/useTerminalState.ts`
（若某组件被保留视图引用，从该命令中移除。）

- [ ] **Step 4: 改 router/index.ts——删路由，首页临时指 DashboardView**

Edit `web/src/router/index.ts`，整体替换为：

```typescript
import { createRouter, createWebHistory } from 'vue-router'
import DashboardView from '../views/DashboardView.vue'
import LiveCockpitView from '../views/LiveCockpitView.vue'
const DataLakeView = () => import('../views/DataLakeView.vue')
const ReviewView = () => import('../views/ReviewView.vue')

const router = createRouter({
  history: createWebHistory(),
  routes: [
    // 首页临时指宏观驾驶舱（回测终端已删；Phase 3 建 CaisenScreenView 后改指 /caisen）
    { path: '/', redirect: '/dashboard' },
    { path: '/dashboard', name: 'dashboard', component: DashboardView },
    { path: '/live', name: 'live', component: LiveCockpitView },
    { path: '/data', name: 'data', component: DataLakeView },
    { path: '/review', name: 'review', component: ReviewView },
  ],
})

export default router
```

- [ ] **Step 5: 改 App.vue——清理导航**

Edit `web/src/App.vue`：
1. 替换 `researchNav` 数组为（删回测/归因/沙盒/因子/策略）：
```typescript
const researchNav: NavItem[] = [
  { to: '/dashboard',  label: '宏观驾驶舱', icon: DataBoard },
  { to: '/data',       label: '数据湖',     icon: Files },
  { to: '/review',     label: 'AI 复盘',    icon: MagicStick },
]
```
2. 删除不再使用的图标 import（`TrendCharts, PieChart, Search, Histogram, SetUp`），仅保留 `MagicStick, DataBoard, Files, Monitor`。
3. 同步删 researchNav 注释段中关于"回测终端→归因回测→因子沙盒→因子→策略"的描述行。

- [ ] **Step 6: 前端类型检查 + build**

Run: `cd web && npm run build 2>&1 | tail -15`
Expected: build 成功，无 TS 错误、无 "Cannot find module" 报错。若报某组件缺失引用，回头补删该引用。

- [ ] **Step 7: Commit**

```bash
cd web && git add -A && git commit -m "refactor(web): 删除回测/因子/策略前端视图与导航（蔡森专精化）

删除 TerminalView/BacktestView/ExplorerView/FactorManagerView/StrategyArchitectView
及 backtest/explorer/factors/strategy API 与无引用的回测/因子/策略组件。
router 首页临时重定向 /dashboard，导航收敛为 宏观/数据湖/复盘 + 实盘中控。
Phase 3 建 CaisenScreenView 后首页改指 /caisen。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 7: 残余测试核实 + 全量回归验收

**Files:**
- 核实并按需删除/改：`tests/test_final_fixes.py`、`tests/test_macro_api.py`、`tests/test_akshare_north_dragon.py`、`tests/test_sync_fundamentals.py`、`tests/test_viz.py`（Task 4 已处理则跳过）

**Interfaces:**
- Produces: Phase 1 验收 gate 全过——pytest 全绿、server 启动、前端 build。

- [ ] **Step 1: 逐个核实残余测试的 import**

Run:
```bash
for f in test_final_fixes test_macro_api test_akshare_north_dragon test_sync_fundamentals; do
  echo "=== $f ===";
  grep -n "import\|from" tests/$f.py | grep -iE "backtest|factors|strategies|portfolio|explorer" || echo "  (无已删模块引用)";
done
```
判定每个文件：
- 若 import 已删模块 → 若整文件依赖则 `git rm`；若仅个别测试函数依赖则 Edit 删除该函数。
- 若无引用 → 保留，跑通即可。

- [ ] **Step 2: 按判定处理残余测试**

依据 Step 1 输出逐文件 Edit 或 git rm。例：若 test_macro_api 仅 import `factors.macro_regime`（已迁），改成 `core.macro_regime` 即可保留。

- [ ] **Step 3: 全量 pytest 回归**

Run: `PYTHONIOENCODING=utf-8 .venv310/Scripts/python.exe -m pytest tests/ -q 2>&1 | tail -15`
Expected: 全绿，无 collection error。记录最终测试数（应大幅少于删改前，因删了大量回测/因子测试）。

- [ ] **Step 4: server 启动冒烟**

Run: `PYTHONIOENCODING=utf-8 .venv310/Scripts/python.exe -c "import server.main; print('routes:', len(server.main.app.routes))"`
Expected: 打印路由数，无异常。

- [ ] **Step 5: 前端 build 回归**

Run: `cd web && npm run build 2>&1 | tail -5`
Expected: build 成功。

- [ ] **Step 6: Commit + 打 tag 标记 Phase 1 完成**

```bash
git add -A && git commit -m "test: 残余测试核实 + Phase 1 验收（蔡森专精底盘就绪）

核实 test_final_fixes/macro_api/akshare_north_dragon/sync_fundamentals 的已删模块依赖，
纯依赖则删、个别函数依赖则精修、仅路径漂移则改 import。
Phase 1 验收：pytest 全绿、server 启动、前端 build 通过。
项目已瘦身为蔡森形态学专精底盘，可进入 Phase 2 核心算法实现。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Self-Review 记录

**1. Spec 覆盖：** spec §2.1 删除清单→Task 2-5+6 覆盖；§2.2 迁移→Task 1；§2.3 改造（main/config/celery/portfolio/前端 router）→Task 2-6 逐项；§2.4 保留→Global Constraints 声明不动。✅
**2. 占位符扫描：** 无 TBD/TODO；残余测试用 grep 判定逻辑（明确命令+判定），非"适当处理"。✅
**3. 类型/路径一致：** CreditRegime 迁移后类名/方法不变（Task 1 Produces 声明），import 路径 `factors.macro_regime`→`core.macro_regime` 全 plan 一致。✅
**4. 已知风险：** Task 2-4 期间 server 因 portfolio 临时引用已删模块而 import 失败，是预期中间态，Task 5 收尾修复——各 Task Step 已标注"容忍 portfolio 报错"。
