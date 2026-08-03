# 前端只读化 + 作业驾驶舱 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把前端整体收敛为只读（撤除全部写 UI 与写调用），并新增一个纯观测视图「作业驾驶舱」，把 C-8 的 job 台账 / 启动补跑 / pre_open gate 拒因暴露给前端。

**Architecture:** 两层最小改动。后端仅 `trading/job_ledger.py` 新增 1 个只读查询函数 + `presentation/server` 新增 1 个薄 service 函数 + 1 个 GET 端点（Phase 2）；所有现有 POST 写端点原样保留供脚本/CLI/QMT 客户端调用。前端撤除全部写 UI/写调用（Phase 1）+ 新增 1 个纯观测视图（Phase 2）。两阶段顺序验收：Phase 1 先落地「前端只读」硬约束，Phase 2 再补作业观测出口。

**Tech Stack:** 后端 Python 3.10 / FastAPI / SQLite（job_ledger）；前端 Vue 3.5 + TypeScript + Element Plus + lightweight-charts + vitest + vue-tsc。

## Global Constraints

- **全中文注释**（CLAUDE.md 强制）：所有新增/修改代码配备高质量中文注释，说明 Why（交易物理意图 / 只读边界）。
- **撤按钮不留死灰按钮**（spec §4.4）：写操作所在的整个面板/区块直接移除；移除后若留视觉空洞，原位置替换为一行只读提示，指明该业务的替代入口（脚本/CLI/QMT 客户端/cron）。
- **后端写端点全部保留**（spec §9）：不删任何 POST 写 router，供脚本/CLI/QMT 客户端继续调用。
- **不引入只读开关 / 熔断脚本**（spec §9）：不加 env READ_ONLY 403 之类，不建 kill_switch.py。
- **job_ledger 写失败绝不阻断主路径**（job_ledger 设计约束）：GET /jobs 在台账读失败时返 200 + `jobs:[]` + `warning`，不抛 5xx。
- **前端 typecheck 零 warning**（spec §7.1）：`vue-tsc --noEmit` 全过，清理未使用类型/函数后的零未引用。
- **/jobs 端点鉴权继承 trading router**：与 GET /status 同口径挂在 `require_write` 依赖下（测试环境无 `QUANTER_API_TOKEN` 放行，生产由 token + IP 白名单保护，见 `client.ts` 注释）。

### 测试现状对齐说明（对 spec §7.1 的诚实修正）

spec §7.1 措辞「改造视图组件测试更新：LiveCockpitView / CaisenScreenView / ParamLabView / DataLakeView 各自的现有组件测试」基于一个**不准确假设**。代码现状核查结果：

| 测试文件 | 现状 | 本计划处置 |
|---|---|---|
| `src/api/caisen.spec.ts` | 存在（API facade 契约，含写用例 scan/reviewPlan/activatePlan/submitReplayAsync/cancelReplayTask/deleteReplayTask） | 随 Task 2/3 改：移除已删写函数的用例与 import |
| `src/views/ParamLabView.spec.ts` | 存在（mock 含写函数，断言「新建回测」按钮） | Task 7 改：移除写 mock + 改空态断言 |
| `src/components/lab/NewReplayDrawer.spec.ts` | 存在 | 随 Task 3 删（NewReplayDrawer 组件一并删） |
| `src/views/__tests__/CockpitView.spec.ts` | 存在（CockpitView 零改） | 不动 |
| LiveCockpitView / CaisenScreenView / DataLakeView / ReviewView 独立组件测试 | **不存在** | 不新建（YAGNI，避免 scope 膨胀），靠 typecheck + 该域 API facade 测试 + 手动核验达标 |
| 路由测试 | **不存在** | Task 6 新建 `src/router/__tests__/index.spec.ts`（spec §7.1 明确要求，路由增删风险高需守护） |

## File Structure

### 前端（presentation/web/src/）

| 文件 | 改动 | 职责 |
|---|---|---|
| `api/trading.ts` | 改：删 connect/disconnect/submitOrder/cancelOrder/emergencyHalt + 类型 SubmitOrderBody/OrderResultRow；Task 11 加 getJobs + JobRow/CatchupState/JobsSnapshot | 实盘交易 facade（Phase 1 后纯只读 GET） |
| `api/caisen.ts` | 改：删 scan/reviewPlan/activatePlan/submitReplayAsync/cancelReplayTask/deleteReplayTask + 失引用类型 | 蔡森 facade（Phase 1 后纯只读 GET） |
| `api/data.ts` | 改：删 triggerSync + SyncResponse | 数据湖 facade（纯只读 GET） |
| `api/review.ts` | 删整文件 | ReviewView 唯一数据源，随视图删 |
| `api/macro.ts` | 零改 | — |
| `views/LiveCockpitView.vue` | 改：撤连接/断开/下单/撤单/熔断 + 下单面板；保留心跳/资产/订单回报/持仓/CSV 导出 | 实盘观测大屏 |
| `views/CaisenScreenView.vue` | 改：撤扫描/审核/激活；保留候选列表/K 线/关键参数展示 | 蔡森观测大屏 |
| `views/ParamLabView.vue` | 改：撤新建回测/取消/删除 + 删 NewReplayDrawer；保留 schema/任务列表/详情/走势/买卖日志 | 参数实验室（纯观测） |
| `views/DataLakeView.vue` | 改：撤立即同步；保留数据集表 + 后台 syncing 轮询 | 数据湖观测 |
| `components/DatasetTable.vue` | 改：删「立即同步」操作列 + sync emit | 配合 DataLakeView 只读化 |
| `components/lab/NewReplayDrawer.vue` | 删整文件 | 随 ParamLabView 撤新建回测 |
| `views/ReviewView.vue` | 删整文件 | 整个移除 |
| `views/JobCockpitView.vue` | 新建（Task 12） | 作业驾驶舱纯观测视图 |
| `router/index.ts` | 改：删 /review 路由；Task 12 加 /jobs 懒加载 | 路由表 |
| `App.vue` | 改：删「AI 复盘」导航项；加 READ-ONLY 徽标；Task 12 加「作业驾驶舱」导航项 | 顶栏导航壳 |
| `router/__tests__/index.spec.ts` | 新建（Task 6） | 路由增删守护 |
| `views/__tests__/JobCockpitView.spec.ts` | 新建（Task 12） | 驾驶舱组件测试 |
| `api/trading.spec.ts` | 新建（Task 11） | getJobs facade 契约测试 |

### 后端

| 文件 | 改动 | 职责 |
|---|---|---|
| `trading/job_ledger.py` | 加 `snapshot_for_date(business_date, path=None) -> list[dict]`（只读 SELECT） | C-8 台账只读查询 |
| `presentation/server/services/trading_service.py` | 加 `get_jobs(date, engine, catchup_task) -> dict`（聚合台账 + catchup 四态） | 薄封装，无状态 |
| `presentation/server/api/v1/trading.py` | 加 `GET /jobs?date=YYYY-MM-DD` 端点（从 app.state 取 engine/catchup_task） | 只读端点 |
| `tests/trading/test_job_ledger.py` | 追加 snapshot_for_date 单测 | tmp DB 范式 |
| `tests/test_trading_service.py` | 追加 get_jobs 单测 | monkeypatch 范式 |
| `tests/test_trading_api.py` | 追加 GET /jobs 端点集成测试 | TestClient + app.state 注入范式 |

### 验证命令

- 前端 typecheck：`cd presentation/web && npm run typecheck`
- 前端测试：`cd presentation/web && npm test`
- 后端测试：`python -m pytest tests/trading/test_job_ledger.py tests/test_trading_service.py tests/test_trading_api.py -v`（用项目 venv，如 `.venv310/Scripts/python.exe`）

### Task 总览

**Phase 1（前端只读化）：** Task 1 LiveCockpitView+trading.ts · Task 2 CaisenScreenView+caisen.ts(scan/review/activate) · Task 3 ParamLabView+NewReplayDrawer 删+caisen.ts(replay 写) · Task 4 DataLakeView+DatasetTable+data.ts · Task 5 ReviewView 整删 · Task 6 路由撤 /review + READ-ONLY 徽标 + router 测试 · Task 7 Phase 1 测试收口。

**Phase 2（作业驾驶舱）：** Task 8 job_ledger.snapshot_for_date · Task 9 trading_service.get_jobs · Task 10 GET /jobs 端点 · Task 11 前端 getJobs+facade 测试 · Task 12 JobCockpitView+组件测试+路由导航接入。


---

## Phase 1 · 前端只读化

### Task 1: LiveCockpitView 撤写 + trading.ts 清理写函数/类型

**Files:**
- Modify: `presentation/web/src/views/LiveCockpitView.vue`
- Modify: `presentation/web/src/api/trading.ts`

**目标态：** LiveCockpitView 仅保留只读观测能力——心跳灯（getStatus 2s 轮询）、资产卡（getAsset）、订单回报列表（getOrders，**无撤单列**）、持仓 Treemap（getPositions）、持仓明细表、CSV 导出（exportLiveTrades）。撤除：连接/断开按钮、下单面板、撤单、紧急熔断按钮。trading.ts 删除 connect/disconnect/submitOrder/cancelOrder/emergencyHalt 五个写函数 + 类型 SubmitOrderBody/OrderResultRow（OrderRow 保留，订单回报列表用）。

**LiveCockpitView.vue 改动清单（subagent 读文件后按下列锚点 Edit）：**

- [ ] **Step 1: import 收敛为只读函数。** 将 `import { getStatus, getPositions, emergencyHalt, exportLiveTrades, connect, disconnect, submitOrder, cancelOrder, getOrders, getAsset, type TradingStatus, type PositionRow, type OrderRow, type Asset, } from '../api/trading'` 改为：
```ts
import {
  getStatus, getPositions, exportLiveTrades,
  getOrders, getAsset,
  type TradingStatus, type PositionRow, type OrderRow, type Asset,
} from '../api/trading'
```

- [ ] **Step 2: 删除写相关响应式状态与下单表单。** 删除 `halting / halted / connecting / submitting` 四个 ref、整个 `orderForm` ref、`orderModeLabel` computed。保留 `status / positions / asset / orders` 与 `modeDisplay / fetchStatus / statusTimer / onMounted / onBeforeUnmount`。

- [ ] **Step 3: 删除写操作函数。** 删除 `onConnect / onDisconnect / onSubmitOrder / onCancelOrder / isCancelable / onHalt` 六个函数。**保留** `orderId(row)` 与 `sideLabel(row)`（订单回报列表的「订单号/方向」展示列仍用）。保留 `lastNDays / exportRange / exporting / onExport / runningStrategies / treemapOption`（CSV 导出 + 持仓展示）。

- [ ] **Step 4: template top-bar 撤连接/熔断按钮，替换只读提示。** 删除 top-bar 内的「连接/断开」两个 `<button class="conn-btn ...">` 与整个 `<el-popconfirm>` 紧急熔断块。在心跳 `<div class="heartbeat">` 之后追加一个只读提示徽标：
```html
<span class="ro-tag" title="前端只读：下单/连接/熔断请赴 QMT 客户端或 cron">READ-ONLY 只读</span>
```

- [ ] **Step 5: 删除整个下单面板 `<section class="order-panel">...</section>`，原位替换为只读提示：**
```html
<section class="readonly-panel">
  <span class="readonly-hint">下单由盘前 pre_open cron（09:22）自动挂单；手动补挂走 trading/tools/trigger_pre_open_once.py；任意单请赴 QMT 客户端。前端只读，不下单。</span>
</section>
```

- [ ] **Step 6: 订单回报列表撤「操作」撤单列。** 标题 `<div class="chart-title">委托订单（实时回报；SUBMITTED/PARTIAL_FILLED 可撤）</div>` 改为 `<div class="chart-title">委托订单（实时回报，只读）</div>`。删除 `<el-table-column label="操作" width="100">` 整列（含其内 `<el-button>撤单</el-button>` 与 `<span v-else>`）。

- [ ] **Step 7: 删除写相关 CSS。** 删除 `.conn-btn / .conn-btn.connect / .conn-btn.disconnect / .conn-btn:hover:not(:disabled) / .conn-btn:disabled` 与 `.halt-btn / .halt-btn:hover:not(:disabled) / .halt-btn:disabled / .halt-btn.halted / @keyframes pulse` 与 `.order-panel / .order-form / .order-form :deep(.el-form-item) / .mode-sim / .mode-live`。新增只读提示样式（追加到 `<style scoped>` 末尾）：
```css
.ro-tag { font-size: 11px; font-weight: 700; color: #fff; background: #c62828; padding: 2px 8px; border-radius: 4px; margin-left: 8px; }
.readonly-panel { background: var(--qt-bg-card); border: 1px solid var(--qt-border); border-radius: 6px; padding: 10px 14px; }
.readonly-hint { font-size: 12px; color: var(--qt-text-secondary); line-height: 1.7; }
```

**trading.ts 改动清单：**

- [ ] **Step 8: 删除写函数与失引用类型。** 删除 `emergencyHalt()`、`connect()`、`disconnect()`、`submitOrder()`、`cancelOrder()` 五个函数；删除 `SubmitOrderBody` 与 `OrderResultRow` 两个 interface（确认 OrderRow 保留——getOrders 用）。保留 getStatus/getPositions/exportLiveTrades/getOrders/getAsset/queryTrades 与各自类型。

- [ ] **Step 9: typecheck 验证零未引用。**
Run: `cd presentation/web && npm run typecheck`
Expected: PASS，无「'connect' is declared but...」之类未使用报错。

- [ ] **Step 10: 提交。**
```bash
git add presentation/web/src/views/LiveCockpitView.vue presentation/web/src/api/trading.ts
git commit -m "feat(web): LiveCockpitView 撤写（连接/下单/撤单/熔断）+ trading.ts 清理写函数"
```


### Task 2: CaisenScreenView 撤写 + caisen.ts 删 scan/reviewPlan/activatePlan

**Files:**
- Modify: `presentation/web/src/views/CaisenScreenView.vue`
- Modify: `presentation/web/src/api/caisen.ts`
- Modify: `presentation/web/src/api/caisen.spec.ts`

**目标态：** CaisenScreenView 退化为纯观测——候选计划列表（listPlans，可刷新）+ lightweight-charts K 线（getChart）+ 选中计划关键参数展示。撤除：扫描入口、approve/reject 审核、activate 激活、edits 微调表单。候选计划由 EOD 事件链自动产出；审核否决走 `veto_plan.py`；激活挂单由 pre_open cron 自动执行。

**CaisenScreenView.vue 改动清单：**

- [ ] **Step 1: import 收敛。** `import { scan, listPlans, getChart, reviewPlan, activatePlan, type CandidatePlan, type ChartData, type ScanRequestBody, } from '../api/caisen'` 改为：
```ts
import {
  listPlans, getChart,
  type CandidatePlan, type ChartData,
} from '../api/caisen'
```

- [ ] **Step 2: 删除写相关状态与表单。** 删除 `scanning / reviewing / activating` 三个 ref、`scanForm` ref、`editForm` ref、`const today = new Date()...` 行（仅 scanForm.date 用）、`canActivate / canReview` 两个 computed。

- [ ] **Step 3: 精简 selectedPlan watch。** watch(selectedPlan) 内删除「同步 editForm」那 5 行（`editForm.value = {...}`），保留拉图表逻辑（loadingChart/getChart/renderChart）。

- [ ] **Step 4: 删除写操作函数。** 删除 `onScan / onReview / onActivate` 三个函数。保留 `refreshPlans / onSelectPlan / initChart / destroyChart / renderChart` 与所有徽章辅助函数（patternTagType/patternLabel/statusTagType）。

- [ ] **Step 5: top-bar 撤扫描按钮 + 改副标题。** 删除「触发扫描」`<el-button ... @click="onScan">触发扫描</el-button>`。副标题 `<span class="subtitle">scan → 选中 → 看图 → approve → activate</span>` 改为 `<span class="subtitle">候选列表（EOD 自动产出）→ 选中 → 看图（只读观测）</span>`。保留「刷新列表」按钮。

- [ ] **Step 6: 底部审核区整块替换为只读提示。** 删除整个 `<section class="bottom-card">...</section>`（含扫描参数 form-block 与审核操作 form-block），原位替换为：
```html
<section class="bottom-card">
  <div class="readonly-hint">
    蔡森候选计划由 EOD 事件链自动产出；审核否决请赴 <code>veto_plan.py</code>；激活挂单由 pre_open cron（09:22）自动执行。本视图仅作只读观测。
  </div>
</section>
```
追加 CSS：`.readonly-hint { font-size: 12px; color: var(--qt-text-secondary); line-height: 1.7; padding: 8px 4px; } .readonly-hint code { color: var(--qt-accent); font-family: var(--qt-font-mono); }`。其余 `.bottom-card / .bottom-grid / .form-block / .block-title / .empty-block` CSS 可保留（无害）或一并删（subagent 自行判断整洁度）。

- [ ] **Step 7: typecheck 验证。**
Run: `cd presentation/web && npm run typecheck`
Expected: 此刻 caisen.ts 仍导出 scan/reviewPlan/activatePlan，但 CaisenScreenView 不再 import——typecheck 应 PASS（未使用 export 不报错）。若报 CaisenScreenView 内残留引用 → 回查 Step 1-6 是否删净。

**caisen.ts 改动清单：**

- [ ] **Step 8: 删除 scan/reviewPlan/activatePlan 三函数 + 失引用类型。** 删除 `scan()`、`reviewPlan()`、`activatePlan()` 三个函数；删除 `ScanRequestBody` 与 `PlanReviewBody` 两个 interface（确认无其他引用——CaisenScreenView 已不 import）。保留 listPlans/getPlan/getChart/getConfigSchema/listReplayTasks/getReplayTask 与各自类型（Task 3 再删 replay 写函数）。

**caisen.spec.ts 改动清单：**

- [ ] **Step 9: 移除已删写函数的契约用例与 import。** 从 `import { ... } from './caisen'` 移除 `scan, reviewPlan, activatePlan`；删除三个对应 `it(...)` 用例（scan/reviewPlan/activatePlan）。保留 listPlans/getPlan/getChart 用例。

- [ ] **Step 10: 验证 + 提交。**
Run: `cd presentation/web && npm run typecheck && npm test`
Expected: typecheck PASS；vitest 全过（caisen.spec.ts 剩余用例绿）。
```bash
git add presentation/web/src/views/CaisenScreenView.vue presentation/web/src/api/caisen.ts presentation/web/src/api/caisen.spec.ts
git commit -m "feat(web): CaisenScreenView 撤写（扫描/审核/激活）+ caisen.ts 删 scan/review/activate"
```

### Task 3: ParamLabView 撤写 + 删 NewReplayDrawer + caisen.ts 删 replay 写函数

**Files:**
- Modify: `presentation/web/src/views/ParamLabView.vue`
- Delete: `presentation/web/src/components/lab/NewReplayDrawer.vue`
- Delete: `presentation/web/src/components/lab/NewReplayDrawer.spec.ts`
- Modify: `presentation/web/src/api/caisen.ts`
- Modify: `presentation/web/src/api/caisen.spec.ts`

**目标态：** ParamLabView 退化为纯观测——参数 schema 查看（getConfigSchema）、异步任务列表（listReplayTasks）、任务详情+收益曲线（getReplayTask）、买卖日志。撤除：新建回测抽屉（NewReplayDrawer）、提交回测（submitReplayAsync）、取消任务（cancelReplayTask）、删除任务（deleteReplayTask）、FAILED「以此参数重提」入口。回测提交走 backtest 域脚本/CLI，前端只看结果。

**ParamLabView.vue 改动清单：**

- [ ] **Step 1: import 收敛。** 将 caisen import 改为：
```ts
import {
  getConfigSchema, listReplayTasks, getReplayTask,
} from '@/api/caisen'
import type { ReplayTask, ReplayTaskDetail, ReplayTaskStatus } from '@/api/caisen'
```
删除 `NewReplayDrawer` import 行与 `ElMessageBox` import（仅 onDelete 用 ElMessageBox，删；ElMessage 保留）。

- [ ] **Step 2: 删除写相关状态与函数。** 删除 `drawerVisible / submitting` 两个 ref；删除 `onSubmit / onCancel / onDelete` 三个函数。保留 `configSchema / tasks / selectedId / selected / statusFilter / pollTimer / loadSchema / loadTasks / selectTask / refreshSelectedIfChanged / poll / ensurePolling / onMounted / onUnmounted` 及参数详情 computed。

- [ ] **Step 3: 顶栏撤「新建回测」按钮。** 删除 `<el-button type="primary" size="small" @click="drawerVisible = true">＋ 新建回测</el-button>`。

- [ ] **Step 4: FAILED 区撤「以此参数重提」入口。** FAILED 分支 `<div v-else-if="selected?.status === 'FAILED'" class="qt-empty lab-failed">回测失败：{{ selected.error }}<el-button size="small" @click="drawerVisible = true">以此参数重提</el-button></div>` 改为：
```html
<div v-else-if="selected?.status === 'FAILED'" class="qt-empty lab-failed">
  回测失败：{{ selected.error }}
</div>
```

- [ ] **Step 5: 任务行撤取消/删除按钮。** 删除 `<span class="task-actions">...</span>`（含 onCancel/onDelete 两个 el-button）。

- [ ] **Step 6: 空态文案改。** `<div v-else class="qt-empty">点 ＋新建回测 开始第一次实验</div>` 改为 `<div v-else class="qt-empty">暂无回测任务（回测由脚本/CLI 提交，前端只读观测）</div>`。

- [ ] **Step 7: 删除 NewReplayDrawer 组件挂载。** 删除模板末尾 `<NewReplayDrawer v-model:visible="drawerVisible" :config-schema="configSchema" :prefill="selected?.cfg_override" :submitting="submitting" @submit="onSubmit" />`。

- [ ] **Step 8: 删除 NewReplayDrawer 组件与其测试文件。**
```bash
git rm presentation/web/src/components/lab/NewReplayDrawer.vue
git rm presentation/web/src/components/lab/NewReplayDrawer.spec.ts
```

**caisen.ts 改动清单：**

- [ ] **Step 9: 删除 replay 三个写函数 + 失引用类型。** 删除 `submitReplayAsync()`、`cancelReplayTask()`、`deleteReplayTask()` 三函数；删除 `ReplayAsyncRequestBody` 与 `CancelResponse` 两个 interface。保留 listReplayTasks/getReplayTask + ReplayTask/ReplayTaskDetail/ReplayTaskStatus 类型。

**caisen.spec.ts 改动清单：**

- [ ] **Step 10: 移除 replay 写用例。** 从 import 移除 `submitReplayAsync, cancelReplayTask, deleteReplayTask`；删除三个对应 `it(...)`（submitReplayAsync/cancelReplayTask/deleteReplayTask）。保留 listReplayTasks/getReplayTask 用例。`mockPost` 若已无任何用例引用则从文件移除（保留 `mockGet`/`mockPatch`/`mockDelete` 中仍被引用的）。

- [ ] **Step 11: 验证 + 提交。**
Run: `cd presentation/web && npm run typecheck && npm test`
Expected: typecheck PASS；vitest 全过（NewReplayDrawer.spec 已删，caisen.spec 剩余用例绿）。
```bash
git add -A presentation/web/src
git commit -m "feat(web): ParamLabView 撤写（新建/取消/删除回测）+ 删 NewReplayDrawer + caisen.ts 删 replay 写函数"
```


### Task 4: DataLakeView 撤写 + DatasetTable 删操作列 + data.ts 删 triggerSync

**Files:**
- Modify: `presentation/web/src/views/DataLakeView.vue`
- Modify: `presentation/web/src/components/DatasetTable.vue`
- Modify: `presentation/web/src/api/data.ts`

**目标态：** DataLakeView 退化为纯观测——数据集表格（getDatasets）+ 后台 syncing 态自适应轮询（保留：daemon/cron 后台同步仍会产生 syncing，前端需观测其收敛）。撤除：「立即同步」手动触发（triggerSync）。DatasetTable 删「操作」列与 sync emit。

**DataLakeView.vue 改动清单：**

- [ ] **Step 1: import 撤 triggerSync。** `import { getDatasets, triggerSync, type DatasetAsset } from '@/api/data'` 改为 `import { getDatasets, type DatasetAsset } from '@/api/data'`。

- [ ] **Step 2: 删除 onSync 函数。** 删除整个 `async function onSync(key: string) {...}`。保留 `fetchDatasets / anySyncing / ensurePolling / onMounted / onBeforeUnmount`。

- [ ] **Step 3: template 撤 DatasetTable 的 sync 绑定。** `<DatasetTable :datasets="datasets" @sync="onSync" />` 改为 `<DatasetTable :datasets="datasets" />`。

**DatasetTable.vue 改动清单：**

- [ ] **Step 4: 删除 sync emit 声明。** 删除 `const emit = defineEmits<{ (e: 'sync', key: string): void }>()`。

- [ ] **Step 5: 删除「操作」列。** 删除整个 `<el-table-column label="操作" width="120" fixed="right">...</el-table-column>`（含「立即同步」el-button）。

**data.ts 改动清单：**

- [ ] **Step 6: 删除 triggerSync + SyncResponse。** 删除 `triggerSync()` 函数与 `SyncResponse` interface。保留 `getDatasets` + `DatasetAsset / DatasetStatus` 类型。

- [ ] **Step 7: 验证 + 提交。**
Run: `cd presentation/web && npm run typecheck && npm test`
Expected: typecheck PASS（DatasetTable.spec.ts 若存在并断言操作列需同步更新——核查 `src/components/DatasetTable.spec.ts`，若其断言「立即同步」按钮存在则改为断言该列不存在或删除该断言）。
```bash
git add presentation/web/src/views/DataLakeView.vue presentation/web/src/components/DatasetTable.vue presentation/web/src/components/DatasetTable.spec.ts presentation/web/src/api/data.ts
git commit -m "feat(web): DataLakeView 撤立即同步 + DatasetTable 删操作列 + data.ts 删 triggerSync"
```

> **核查点：** `src/components/DatasetTable.spec.ts` 现存——subagent 须读它，若有「立即同步」按钮存在性断言则更新（删除该断言或改为 `expect(wrapper.findAll('button').filter(b=>b.text().includes('立即同步'))).toHaveLength(0)`）。

### Task 5: ReviewView 整删

**Files:**
- Delete: `presentation/web/src/views/ReviewView.vue`
- Delete: `presentation/web/src/api/review.ts`

**目标态：** ReviewView 唯一功能 `diagnose` 是写操作（POST LLM 推理），撤后空壳，整个移除（路由/导航在 Task 6 处理）。

- [ ] **Step 1: 删除 ReviewView.vue 与 review.ts。**
```bash
git rm presentation/web/src/views/ReviewView.vue
git rm presentation/web/src/api/review.ts
```

- [ ] **Step 2: 验证（此步 router 仍引用 ReviewView，typecheck 会 FAIL——属预期，Task 6 修 router）。**
Run: `cd presentation/web && npm run typecheck`
Expected: **FAIL**，报 `router/index.ts` 引用已删的 ReviewView。这是预期的中间态，Task 6 Step 1 立即修复。**不要在此 commit**，与 Task 6 合并提交。

### Task 6: 路由撤 /review + App.vue 撤「AI 复盘」导航 + READ-ONLY 徽标 + 新建 router 测试

**Files:**
- Modify: `presentation/web/src/router/index.ts`
- Modify: `presentation/web/src/App.vue`
- Create: `presentation/web/src/router/__tests__/index.spec.ts`

**目标态：** 路由移除 /review 项与 ReviewView import（修复 Task 5 的 typecheck）。App.vue 顶部导航移除「AI 复盘」项；顶栏常驻红色「READ-ONLY 只读」徽标。新建 router 测试守护路由增删。「作业驾驶舱」导航项与 /jobs 路由随 Phase 2 Task 12 一起落地（Phase 1 不留悬空导航）。

**router/index.ts 改动清单：**

- [ ] **Step 1: 撤 ReviewView 路由。** 删除 `const ReviewView = () => import('../views/ReviewView.vue')` 行；删除 routes 数组中 `{ path: '/review', name: 'review', component: ReviewView }` 项。同步更新文件头注释（移除「/review → ReviewView」描述行）。

**App.vue 改动清单：**

- [ ] **Step 2: researchNav 撤「AI 复盘」项。** 删除 researchNav 数组中 `{ to: '/review', label: 'AI 复盘', icon: MagicStick }` 项。同步删除 `MagicStick` 图标 import（若已无其他引用）。

- [ ] **Step 3: 顶栏加 READ-ONLY 徽标。** 在 `<span class="nav-brand">Quanter</span>` 之后追加：
```html
<span class="ro-badge" title="前端只读：所有写操作走脚本/CLI/QMT 客户端/cron">READ-ONLY 只读</span>
```
追加 scoped CSS：
```css
.ro-badge {
  font-size: 10px; font-weight: 700; color: #fff;
  background: #c62828; padding: 2px 6px; border-radius: 3px;
  margin-left: var(--qt-space-2); letter-spacing: 0.3px;
}
```

**新建 router 测试：**

- [ ] **Step 4: 写 router 测试。** 创建 `presentation/web/src/router/__tests__/index.spec.ts`：
```ts
import { describe, it, expect } from 'vitest'
import router from '../index'

describe('router 路由表', () => {
  it('已撤除 /review（ReviewView 整删）', () => {
    const paths = router.getRoutes().map((r) => r.path)
    expect(paths).not.toContain('/review')
  })
  it('保留核心只读路由', () => {
    const paths = router.getRoutes().map((r) => r.path)
    for (const p of ['/caisen', '/lab', '/dashboard', '/data', '/live', '/cockpit']) {
      expect(paths).toContain(p)
    }
  })
})
```

- [ ] **Step 5: 验证 + 提交（合并 Task 5 的删除）。**
Run: `cd presentation/web && npm run typecheck && npm test`
Expected: typecheck PASS（ReviewView 引用已清）；vitest 全过（新 router 测试绿）。
```bash
git add -A presentation/web/src
git commit -m "feat(web): 路由撤 /review + 导航撤 AI 复盘 + 顶栏 READ-ONLY 徽标 + router 测试"
```


### Task 7: Phase 1 测试收口（ParamLabView.spec.ts 改 + 全量验收）

**Files:**
- Modify: `presentation/web/src/views/ParamLabView.spec.ts`

**目标态：** ParamLabView.spec.ts 对齐撤写后的组件——移除写函数 mock、移除「新建回测」按钮断言、空态文案对齐「回测由脚本/CLI 提交」。整个 Phase 1 收尾：typecheck + vitest 全绿。

- [ ] **Step 1: 改 ParamLabView.spec.ts。** 该测试当前 `vi.mock('@/api/caisen', ...)` 含 `submitReplayAsync/cancelReplayTask/deleteReplayTask` 三个写函数 mock，并断言 `expect(wrapper.text()).toContain('新建回测')` 与 `toContain('点 ＋新建回测')`。改动：
  1. mock 工厂只保留读函数：`{ getConfigSchema: vi.fn().mockResolvedValue({...}), listReplayTasks: vi.fn().mockResolvedValue([]), getReplayTask: vi.fn().mockResolvedValue(null) }`（删 submitReplayAsync/cancelReplayTask/deleteReplayTask）。
  2. 删除 `expect(wrapper.text()).toContain('新建回测')` 断言。
  3. 空态断言改为 `expect(wrapper.text()).toContain('暂无回测任务')`。
  4. 保留「参数详情/收益率走势/买卖日志/任务列表」四区渲染断言（这些区保留）。

- [ ] **Step 2: Phase 1 全量验收。**
Run: `cd presentation/web && npm run typecheck && npm test`
Expected: typecheck PASS 零 warning；vitest 全部 spec 绿（caisen.spec/ParamLabView.spec/CockpitView.spec/各 cockpit 子组件 spec/DatasetTable.spec/router 测试）。

- [ ] **Step 3: 手动核验只读姿态（subagent 自查清单）。** grep 确认前端再无写调用：
Run: `cd presentation/web && grep -rn "submitOrder\|cancelOrder\|emergencyHalt\|connect\|disconnect\|triggerSync\|scan\|reviewPlan\|activatePlan\|submitReplayAsync\|cancelReplayTask\|deleteReplayTask\|diagnose" src --include=*.vue`
Expected: 仅 `src/api/*.ts` 中**保留的只读函数**可能的同名子串命中（如 `connect` 出现在注释）；**任何 .vue 文件 0 命中**写调用。若 .vue 命中 → 回查对应 Task 是否漏撤。

- [ ] **Step 4: 提交。**
```bash
git add presentation/web/src/views/ParamLabView.spec.ts
git commit -m "test(web): ParamLabView.spec 对齐只读化（撤写 mock + 空态断言）+ Phase 1 收口"
```

> **Phase 1 完成标志：** 前端无任何写 UI/写调用；typecheck + vitest 全绿；顶栏 READ-ONLY 徽标常驻；/review 路由与 ReviewView 已删。进入 Phase 2。


---

## Phase 2 · 作业驾驶舱（A 类 · 纯观测）

### Task 8: job_ledger.snapshot_for_date（只读查询，TDD）

**Files:**
- Modify: `trading/job_ledger.py`
- Test: `tests/trading/test_job_ledger.py`（追加用例）

**Interfaces:**
- Produces: `job_ledger.snapshot_for_date(business_date: str, path: Optional[str] = None) -> list[dict]`，返回 `[{"name","status","started_at","finished_at","message"}]`（按 job_name 升序），无记录返 `[]`。

- [ ] **Step 1: 先写失败的测试。** 在 `tests/trading/test_job_ledger.py` 末尾追加三用例：空表返 `[]`；同日多 job 全返 + 字段映射 `job_name→name` + 按 job_name 排序；按日隔离（只返查询日）。示例断言要点：
```python
def test_snapshot_returns_all_jobs_for_date(tmp_path):
    db = str(tmp_path / "job_run.db")
    job_ledger.begin_run("pipeline", "2026-08-02", "t1", path=db)
    job_ledger.finish_run("pipeline", "2026-08-02", "done", "ok", path=db)
    job_ledger.begin_run("pre_open", "2026-08-02", "t2", path=db)
    job_ledger.finish_run("pre_open", "2026-08-02", "skipped", "gate3 reject", path=db)
    snap = job_ledger.snapshot_for_date("2026-08-02", path=db)
    assert [j["name"] for j in snap] == ["pipeline", "pre_open"]
    assert snap[0]["status"] == "done" and snap[0]["message"] == "ok"
    assert snap[1]["status"] == "skipped" and snap[1]["started_at"] == "t2"
```
另两用例：`test_snapshot_empty_when_no_rows`（init_db 后查空 == []）、`test_snapshot_isolates_by_date`（两日各一条，查 2026-08-02 只返 1 条）。

- [ ] **Step 2: 跑测试确认失败。**
Run: `python -m pytest tests/trading/test_job_ledger.py -v`
Expected: FAIL（`AttributeError: module 'trading.job_ledger' has no attribute 'snapshot_for_date'`）。

- [ ] **Step 3: 实现 snapshot_for_date。** 在 `trading/job_ledger.py` 的 `reset_stale_running` 之后追加（只读 SELECT，复用 `_connect`，job_name 列映射为 name）：
```python
def snapshot_for_date(business_date: str, path: Optional[str] = None) -> list[dict]:
    """读某业务日全部 job 的最新台账行（只读 SELECT，不改状态机）。

    返回 [{name, status, started_at, finished_at, message}, ...]，按 job_name 升序。
    无记录返 []。物理意图（spec §5.1）：GET /trading/jobs 消费——把 C-8 台账暴露给
    前端驾驶舱，让研究员一眼看清「今天 pipeline/pre_open 跑没跑、为何没挂单（gate
    拒因=message）」。纯只读，调用方应 try/except 包裹，读失败降级返空，不阻断观测。
    """
    conn = _connect(path)
    rows = conn.execute(
        "SELECT job_name, status, started_at, finished_at, message "
        "FROM job_run WHERE business_date=? ORDER BY job_name",
        (business_date,),
    ).fetchall()
    conn.close()
    return [
        {"name": r[0], "status": r[1], "started_at": r[2],
         "finished_at": r[3], "message": r[4] or ""}
        for r in rows
    ]
```

- [ ] **Step 4: 跑测试确认通过。**
Run: `python -m pytest tests/trading/test_job_ledger.py -v`
Expected: PASS（原 4 + 新 3 全绿）。

- [ ] **Step 5: 提交。**
```bash
git add trading/job_ledger.py tests/trading/test_job_ledger.py
git commit -m "feat(job_ledger): snapshot_for_date 只读查询（GET /trading/jobs 数据源）"
```


### Task 9: trading_service.get_jobs（聚合台账 + catchup 四态，TDD）

**Files:**
- Modify: `presentation/server/services/trading_service.py`
- Test: `tests/test_trading_service.py`（追加用例）

**Interfaces:**
- Consumes: `job_ledger.snapshot_for_date(date)`（Task 8）
- Produces: `trading_service.get_jobs(date: str, engine, catchup_task) -> dict` → `{"date","jobs":[],"catchup":{"state","result"},"warning"?}`；`catchup.state ∈ {running,done,failed,not_started}`；`catchup.result` 在 done/failed 时为 dict，running/not_started 时为 None。

**关键设计：** catchup 状态解析抽成纯函数 `_resolve_catchup_state(task)`，仅依赖 `task.done()/exception()/result()`（duck typing），测试用 fake task 对象注入，无需真起 asyncio 事件循环。`engine` 参数当前未用，预留未来 engine 内嵌可观测态。

- [ ] **Step 1: 先写失败的测试。** 在 `tests/test_trading_service.py` 末尾追加 fake task 工具 + 5 用例。fake task 仅实现 done/exception/result 三方法；台账用 monkeypatch job_ledger.snapshot_for_date 控制。覆盖：not_started（task=None）、running（done=False）、done（done=True + result dict）、failed（done=True + exc）、台账读失败降级 warning（snapshot_for_date 抛 Exception → jobs:[] + warning，不向上抛）。done 用例的 result 用 `{"pipeline": True, "brief": False, "pre_open": False, "pre_open_note": "", "error": None}`（run_startup_catchup 真实返回结构）。

- [ ] **Step 2: 跑测试确认失败。**
Run: `python -m pytest tests/test_trading_service.py -k get_jobs -v`
Expected: FAIL（get_jobs / _FakeTask 未定义）。

- [ ] **Step 3: 实现 get_jobs + _resolve_catchup_state。** 在 `presentation/server/services/trading_service.py` 末尾追加：
```python
def _resolve_catchup_state(task) -> dict:
    """把启动补跑 asyncio.Task 探测为 {state, result}（spec §5.2 catchup 四态）。

    纯函数，仅调 task.done()/exception()/result()（duck typing）——不依赖 asyncio
    事件循环，故测试可用 fake 对象注入。状态：None→not_started；未 done→running；
    exception 非 None→failed+result={'error':...}；否则 done+result=run_startup_catchup 返回 dict。
    """
    if task is None:
        return {"state": "not_started", "result": None}
    if not task.done():
        return {"state": "running", "result": None}
    exc = task.exception()
    if exc is not None:
        return {"state": "failed", "result": {"error": str(exc)}}
    return {"state": "done", "result": task.result()}


def get_jobs(date: str, engine, catchup_task) -> dict:
    """聚合当天 job 台账 + 启动补跑 task 状态（GET /trading/jobs 消费，spec §5.1）。

    台账读失败降级 jobs:[] + warning（台账是操作元数据，绝不阻断观测主路径）。
    engine 当前未用，预留未来读 engine 内嵌可观测态。router 从 app.state 传入，
    本函数无状态。返回 {"date","jobs":[...],"catchup":{"state","result"},"warning"?}。
    """
    from trading import job_ledger
    out: dict = {"date": date, "jobs": [],
                 "catchup": {"state": "not_started", "result": None}}
    try:
        out["jobs"] = job_ledger.snapshot_for_date(date)
    except Exception as e:
        logger.exception("job 台账读取失败（GET /jobs 降级返空，不阻断观测）")
        out["jobs"] = []
        out["warning"] = f"job 台账读取失败：{e}"
    out["catchup"] = _resolve_catchup_state(catchup_task)
    return out
```

- [ ] **Step 4: 跑测试确认通过。**
Run: `python -m pytest tests/test_trading_service.py -v`
Expected: PASS（新 5 + 原有用例全绿）。

- [ ] **Step 5: 提交。**
```bash
git add presentation/server/services/trading_service.py tests/test_trading_service.py
git commit -m "feat(trading_service): get_jobs 聚合台账 + catchup 四态（GET /trading/jobs 业务层）"
```


### Task 10: GET /api/v1/trading/jobs 端点（TDD）

**Files:**
- Modify: `presentation/server/api/v1/trading.py`
- Test: `tests/test_trading_api.py`（追加用例）

**Interfaces:**
- Consumes: `trading_service.get_jobs(date, engine, catchup_task)`（Task 9）、`clock.today()`（date 缺省）、`request.app.state.trading_engine` / `request.app.state.catchup_task`（router 注入）。
- Produces: `GET /api/v1/trading/jobs?date=YYYY-MM-DD`（date 缺省 = clock.today()），返 `{date, jobs, catchup, warning?}`。

**鉴权：** 端点加在 trading_router 内，自动继承 `require_write` 依赖（与 GET /status 同口径；测试环境无 `QUANTER_API_TOKEN` 放行）。

- [ ] **Step 1: 先写失败的测试。** 在 `tests/test_trading_api.py` 顶部 import 区加 `from presentation.server.main import app`（若未 import）；在文件内定义局部 `_FakeTask`（duck typing，实现 done/exception/result，与 test_trading_service 同构）。追加 4 用例：

```python
class _FakeTask:
    def __init__(self, done, exc=None, result=None):
        self._done, self._exc, self._res = done, exc, result
    def done(self): return self._done
    def exception(self): return self._exc
    def result(self): return self._res


def test_jobs_default_date_today(client, monkeypatch):
    """GET /jobs 无 date → date 缺省 = clock.today()；无 catchup_task → not_started。"""
    from trading import clock
    # 清掉可能残留的 catchup_task（TestClient 不跑 lifespan，state 本来就没它）
    monkeypatch.setattr(app.state, "catchup_task", None, raising=False)
    r = client.get("/api/v1/trading/jobs")
    assert r.status_code == 200
    body = r.json()
    assert body["date"] == clock.today()
    assert body["catchup"]["state"] == "not_started"
    assert isinstance(body["jobs"], list)


def test_jobs_catchup_running(client, monkeypatch):
    """注入未完成 catchup_task → state=running。"""
    monkeypatch.setattr(app.state, "catchup_task", _FakeTask(done=False), raising=False)
    r = client.get("/api/v1/trading/jobs")
    assert r.json()["catchup"]["state"] == "running"


def test_jobs_catchup_done(client, monkeypatch):
    """注入已完成 catchup_task → state=done + result 透传。"""
    res = {"pipeline": True, "brief": False, "pre_open": False, "pre_open_note": "", "error": None}
    monkeypatch.setattr(app.state, "catchup_task", _FakeTask(done=True, result=res), raising=False)
    body = client.get("/api/v1/trading/jobs").json()
    assert body["catchup"]["state"] == "done"
    assert body["catchup"]["result"] == res


def test_jobs_with_explicit_date(client, monkeypatch):
    """GET /jobs?date=2026-07-31 → date 原样回显。"""
    monkeypatch.setattr(app.state, "catchup_task", None, raising=False)
    r = client.get("/api/v1/trading/jobs?date=2026-07-31")
    assert r.json()["date"] == "2026-07-31"
```

- [ ] **Step 2: 跑测试确认失败。**
Run: `python -m pytest tests/test_trading_api.py -k jobs -v`
Expected: FAIL（404 — `/jobs` 端点未注册；或 AttributeError）。

- [ ] **Step 3: 实现端点。** 在 `presentation/server/api/v1/trading.py`：
  1. import 区追加：`from fastapi import APIRouter, HTTPException, Query, Request`（在现有 `Query` 后加 `Request`）、`from trading import clock`、在 `from presentation.server.services.trading_service import (...)` 的导入列表里追加 `get_jobs`。
  2. 在 `asset_endpoint` 之后追加：
```python
@router.get("/jobs", summary="作业驾驶舱：当天 job 台账 + 启动补跑状态（只读）")
async def jobs_endpoint(
    request: Request,
    date: str | None = Query(None, description="业务日 YYYY-MM-DD，缺省=clock.today()"),
) -> dict:
    """聚合当天 pipeline/pre_open 台账 + 启动补跑 catchup 四态（spec §5）。

    只读、无副作用。台账读失败降级 jobs:[] + warning（service 层处理），绝不 5xx。
    engine / catchup_task 从 app.state 取（lifespan 装配；未装配时 get_jobs 降级 not_started）。
    """
    engine = getattr(request.app.state, "trading_engine", None)
    catchup_task = getattr(request.app.state, "catchup_task", None)
    return get_jobs(date or clock.today(), engine, catchup_task)
```

- [ ] **Step 4: 跑测试确认通过。**
Run: `python -m pytest tests/test_trading_api.py -v`
Expected: PASS（新 4 + 原有用例全绿）。

- [ ] **Step 5: 提交。**
```bash
git add presentation/server/api/v1/trading.py tests/test_trading_api.py
git commit -m "feat(server): GET /api/v1/trading/jobs 作业驾驶舱只读端点"
```

### Task 11: 前端 trading.ts getJobs + 类型 + facade 测试

**Files:**
- Modify: `presentation/web/src/api/trading.ts`
- Create: `presentation/web/src/api/trading.spec.ts`

**目标态：** trading.ts 新增 `getJobs(date)` 与 `JobRow / CatchupState / JobsSnapshot` 类型，对齐后端 GET /trading/jobs 契约。新建 trading.spec.ts 守护 getJobs 调用姿势（URL/params/timeout），与 caisen.spec.ts 同范式。

- [ ] **Step 1: 在 trading.ts 追加类型与函数。** 文件末尾追加：
```ts
// ============ Phase 2 · 作业驾驶舱（GET /trading/jobs，只读） ============

/** 单个 job 台账行（对齐后端 job_ledger.snapshot_for_date 返回项）。 */
export interface JobRow {
  name: string                            // pipeline / pre_open / ...
  status: 'running' | 'done' | 'skipped' | 'failed'
  started_at: string
  finished_at: string | null              // running 时为 null
  message: string                         // gate 拒因（pre_open skipped 时最有价值）
}

/** 启动补跑四态（对齐后端 _resolve_catchup_state）。 */
export type CatchupState = 'running' | 'done' | 'failed' | 'not_started'

/** GET /trading/jobs 响应（作业驾驶舱数据源）。 */
export interface JobsSnapshot {
  date: string
  jobs: JobRow[]
  catchup: { state: CatchupState; result: Record<string, unknown> | null }
  warning?: string                        // 台账读失败时填，前端可折叠提示
}

/** GET /trading/jobs?date=：当天 job 台账 + 启动补跑状态（作业驾驶舱数据源，只读）。 */
export function getJobs(date: string): Promise<JobsSnapshot> {
  return apiClient.get('/api/v1/trading/jobs', { params: { date }, timeout: 10000 })
}
```

- [ ] **Step 2: 新建 trading.spec.ts。** 创建 `presentation/web/src/api/trading.spec.ts`（仿 caisen.spec.ts：vi.mock('./client') 剥离 HTTP，只断言调用姿势）：
```ts
import { describe, it, expect, vi, beforeEach } from 'vitest'

vi.mock('./client', () => ({
  apiClient: { get: vi.fn(), post: vi.fn() },
}))

import { apiClient } from './client'
import { getJobs } from './trading'

const mockGet = vi.mocked(apiClient.get)

beforeEach(() => {
  mockGet.mockReset()
  mockGet.mockResolvedValue({} as any)
})

describe('trading facade getJobs 契约', () => {
  it('getJobs: GET /api/v1/trading/jobs，params 含 date，timeout 10000', async () => {
    await getJobs('2026-08-02')
    expect(mockGet).toHaveBeenCalledWith(
      '/api/v1/trading/jobs',
      { params: { date: '2026-08-02' }, timeout: 10000 },
    )
  })
})
```

- [ ] **Step 3: 验证 + 提交。**
Run: `cd presentation/web && npm run typecheck && npm test`
Expected: typecheck PASS；trading.spec.ts 绿。
```bash
git add presentation/web/src/api/trading.ts presentation/web/src/api/trading.spec.ts
git commit -m "feat(web): trading.ts getJobs + JobRow/CatchupState 类型 + facade 测试"
```


### Task 12: JobCockpitView.vue + 组件测试 + 路由/导航接入

**Files:**
- Create: `presentation/web/src/views/JobCockpitView.vue`
- Create: `presentation/web/src/views/__tests__/JobCockpitView.spec.ts`
- Modify: `presentation/web/src/router/index.ts`
- Modify: `presentation/web/src/App.vue`
- Modify: `presentation/web/src/router/__tests__/index.spec.ts`

**目标态：** 新增纯观测视图「作业驾驶舱」（/jobs）。两块布局：① job 状态表（pipeline/pre_open 当天 status 着色 + gate 拒因 message）；② 启动补跑卡（catchup 四态 + done/failed 后展示 result）。轮询：常态 15s，catchup.state=running 时 3s；onBeforeUnmount clearInterval。无任何操作按钮。

- [ ] **Step 1: 创建 JobCockpitView.vue。** 完整内容（script setup + template + style）。设计要点：
  - import：`ref, computed, onMounted, onBeforeUnmount` from vue；`getJobs, type JobsSnapshot, type JobRow, type CatchupState` from `@/api/trading`；`logger` from `@/utils/logger`。
  - `businessDate = new Date().toISOString().slice(0,10)`（进入视图凝固一次，防跨午夜漂移）。
  - `snapshot = ref<JobsSnapshot|null>(null)`；`loading` ref；`timer` 句柄；`POLL_NORMAL=15000`、`POLL_FAST=3000`。
  - `fetchJobs()`：`loading=true` → `snapshot.value = await getJobs(businessDate)`（catch 仅 logger.error，不阻断轮询）→ finally `loading=false` → 末尾调 `reschedule()`。
  - `reschedule()`：clearInterval 旧 timer；按 `snapshot.catchup.state==='running'` 选 POLL_FAST/POLL_NORMAL 重设 setInterval(fetchJobs, ms)。
  - `onMounted(async()=>{ await fetchJobs() })`；`onBeforeUnmount(()=>{ if(timer){clearInterval(timer);timer=null} })`。
  - 辅助：`statusType(s)` 映射 done→success / skipped→warning / failed→danger / running→primary；`statusLabel(s)` 映射 running→执行中 / done→已完成 / skipped→跳过 / failed→失败。
  - computed：`isCatchupRunning`、`catchupTagType`（running→primary / done→success / failed→danger / 默认 info）、`catchupStateLabel`（running→运行中 / done→已完成 / failed→失败 / not_started→未启动）、`catchupResultLines`（把 catchup.result 的 Object.entries 渲染为 `key: value` 行）。
  - template：`.jobs-view` 根；page-header（标题「作业驾驶舱」+ 副标题「当天 pipeline / pre_open 台账 + 启动补跑状态（只读观测，无操作）」）；① `.qt-card` 含 `el-table`（列：Job/状态(tag)/开始时间/结束时间/说明，说明列 skipped 黄、failed 红，`v-loading=loading`，empty-text「今日无 job 记录（或台账未初始化）」）+ `v-if snapshot.warning` 警示行；② `.qt-card` 启动补跑卡（标题含 running 时 `.spinner` 旋转、`el-tag :type=catchupTagType` 显 catchupStateLabel、轮询节奏提示、result 行列表或空态提示）。
  - style scoped：`.jobs-view` flex 纵向 padding overflow auto；`.qt-card` 卡片底色边框；`.mono` 等宽；`.msg-warn`/`.msg-fail` 着色；`.spinner` 12px 圆形 `@keyframes spin` 旋转动画；`.hint`/`.warn-line` 次要文本色。

  subagent 按「设计要点」+ Task 11 已定义的 `JobsSnapshot/JobRow/CatchupState` 类型生成完整 .vue（参考 LiveCockpitView/CockpitView 的 token 风格 `--qt-*` + Element Plus 用法）。生成后自查：无操作按钮、`clearInterval` 在 onBeforeUnmount、轮询节奏随 catchup.state 切换。


- [ ] **Step 2: 创建组件测试 `presentation/web/src/views/__tests__/JobCockpitView.spec.ts`。** mock getJobs；用 fake timers 验轮询节奏 + unmount 清理。骨架（subagent 补全 EP polyfill）：
```ts
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import JobCockpitView from '../JobCockpitView.vue'

const getJobsMock = vi.hoisted(() => vi.fn())
vi.mock('@/api/trading', () => ({ getJobs: getJobsMock }))

// jsdom polyfill（ResizeObserver/matchMedia，与 ParamLabView.spec 同范式）
class MockObserver { observe() {} unobserve() {} disconnect() {} takeRecords() { return [] } }
;(globalThis as any).ResizeObserver = MockObserver
;(globalThis as any).IntersectionObserver = MockObserver
;(globalThis as any).matchMedia = (globalThis as any).matchMedia || ((q: string) => ({
  matches: false, media: q, onchange: null, addListener: vi.fn(), removeListener: vi.fn(),
  addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
}))

function mockSnapshot(overrides: Record<string, unknown> = {}) {
  return {
    date: '2026-08-02',
    jobs: [{ name: 'pre_open', status: 'skipped', started_at: 't1', finished_at: 't2',
             message: 'gate3 未过：data_ready 未就绪' }],
    catchup: { state: 'not_started', result: null },
    ...overrides,
  }
}

beforeEach(() => { vi.useFakeTimers(); getJobsMock.mockReset() })
afterEach(() => { vi.useRealTimers() })

describe('JobCockpitView', () => {
  it('渲染 job 表 + gate 拒因 + skipped 标签', async () => {
    getJobsMock.mockResolvedValue(mockSnapshot())
    const w = mount(JobCockpitView, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    expect(w.text()).toContain('pre_open')
    expect(w.text()).toContain('gate3 未过：data_ready 未就绪')
    expect(w.text()).toContain('跳过')
  })

  it('catchup=running → 显运行态 + 3s 快轮询触发新请求', async () => {
    getJobsMock.mockResolvedValue(mockSnapshot({ catchup: { state: 'running', result: null } }))
    mount(JobCockpitView, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    const n = getJobsMock.mock.calls.length
    await vi.advanceTimersByTimeAsync(3000)
    expect(getJobsMock.mock.calls.length).toBeGreaterThan(n)
  })

  it('catchup=done → 展示 result 子任务行', async () => {
    getJobsMock.mockResolvedValue(mockSnapshot({
      catchup: { state: 'done', result: { pipeline: true, error: null } },
    }))
    const w = mount(JobCockpitView, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    expect(w.text()).toContain('pipeline: true')
  })

  it('unmount 后定时器已清（60s 内无新请求）', async () => {
    getJobsMock.mockResolvedValue(mockSnapshot())
    const w = mount(JobCockpitView, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    const n = getJobsMock.mock.calls.length
    w.unmount()
    await vi.advanceTimersByTimeAsync(60000)
    expect(getJobsMock.mock.calls.length).toBe(n)
  })
})
```

- [ ] **Step 3: 验证组件测试。**
Run: `cd presentation/web && npm test -- JobCockpitView`
Expected: 4 用例全绿（若 mount 因 EP 组件渲染细节微调断言文案，subagent 据实对齐——核心是「gate 拒因可见 / catchup 切换 / 轮询触发 / unmount 停轮询」四语义成立）。

- [ ] **Step 4: router 接入 /jobs。** `presentation/web/src/router/index.ts`：追加 `const JobCockpitView = () => import('../views/JobCockpitView.vue')`；routes 数组追加 `{ path: '/jobs', name: 'jobs', component: JobCockpitView }`（置于 `/cockpit` 之后）。更新文件头注释。

- [ ] **Step 5: App.vue 导航加「作业驾驶舱」项。** `presentation/web/src/App.vue`：图标 import 追加 `Operation`（EP 官方图标）；liveNav 数组在「综合看板」之后、「实盘中控」之前插入 `{ to: '/jobs', label: '作业驾驶舱', icon: Operation }`。

- [ ] **Step 6: router 测试补 /jobs 断言。** `src/router/__tests__/index.spec.ts` 在「保留核心只读路由」用例的 `for (const p of [...])` 列表里加 `'/jobs'`。

- [ ] **Step 7: Phase 2 全量验收 + 提交。**
Run: `cd presentation/web && npm run typecheck && npm test`
Expected: typecheck PASS；vitest 全绿（含 JobCockpitView.spec 4 + router 测试含 /jobs）。
Run: `python -m pytest tests/trading/test_job_ledger.py tests/test_trading_service.py tests/test_trading_api.py -v`
Expected: 后端全绿（Phase 2 累计）。
```bash
git add -A presentation/web/src
git commit -m "feat(web): JobCockpitView 作业驾驶舱纯观测视图 + /jobs 路由 + 导航接入"
```

> **Phase 2 完成标志：** GET /trading/jobs 端点可用（台账 + catchup 四态）；前端 /jobs 视图渲染 job 表 + gate 拒因 + 启动补跑卡；前后端测试全绿。


---

## Self-Review

### 1. Spec 覆盖核对

| spec 章节/要求 | 覆盖 Task |
|---|---|
| §4.1 LiveCockpitView 撤 connect/disconnect/下单/撤单/熔断，保留心跳/资产/订单/持仓/CSV | Task 1 |
| §4.1 CaisenScreenView 撤 scan/review/activate，保留 listPlans/getChart | Task 2 |
| §4.1 ParamLabView 撤 NewReplayDrawer/cancel/delete，保留 schema/list/detail | Task 3 |
| §4.1 DataLakeView 撤 triggerSync，保留 getDatasets + 健康轮询 | Task 4 |
| §4.1 ReviewView 整个移除 | Task 5 |
| §4.2 trading.ts 删 connect/disconnect/submitOrder/cancelOrder/emergencyHalt + SubmitOrderBody/OrderResultRow | Task 1 |
| §4.2 caisen.ts 删 scan/reviewPlan/activatePlan/submitReplayAsync/cancelReplayTask/deleteReplayTask | Task 2/3 |
| §4.2 data.ts 删 triggerSync；review.ts 整删 | Task 4 / Task 5 |
| §4.3 路由撤 /review；App.vue 撤 AI 复盘 + 加 READ-ONLY 徽标 | Task 6 |
| §4.3 /jobs 路由 + 「作业驾驶舱」导航项（spec 注明 Phase 2） | Task 12（合理调整：全套随 Phase 2 落地，避免 Phase 1 悬空导航） |
| §4.4 撤按钮不留死灰按钮 + 只读提示 | Task 1/2/3/4 各自替换只读提示 |
| §5.1 job_ledger.snapshot_for_date | Task 8 |
| §5.1 trading_service.get_jobs | Task 9 |
| §5.1 GET /trading/jobs 端点 | Task 10 |
| §5.2 端点契约（date/jobs/catchup 四态） | Task 9/10（_resolve_catchup_state 四态 + 端点回显） |
| §5.3 前端 getJobs + JobCockpitView（两块布局 + 15s/3s 轮询 + unmount 清理） | Task 11/12 |
| §6 错误处理（台账读失败 200+jobs:[]+warning；catchup not_started/failed；网络错误不崩） | Task 9（warning 降级）+ Task 12（catch 仅 logger） |
| §7.1 前端测试（caisen.spec 改、ParamLabView.spec 改、删 NewReplayDrawer.spec、JobCockpitView 新测、router 测试、typecheck 零 warning） | Task 2/3/6/7/11/12 |
| §7.2 后端测试（snapshot_for_date 单测 + GET /jobs 集成测试四态） | Task 8/10 |
| §9 不做项（不建熔断脚本 / 不删后端写端点 / 不加 READ_ONLY env / 不做 SSE） | 全程遵守，Global Constraints 锁定 |

### 2. Placeholder 扫描

无 TBD/TODO/"实现略"/"类似 Task N"。JobCockpitView 因体量大以「设计要点」给出（锚定已定义类型 + token 风格 + 同仓视图范式），属可执行规格而非占位；subagent 据要点生成完整 .vue 后有组件测试（Task 12 Step 2）四语义守护，可验证。

### 3. 类型一致性

- `snapshot_for_date` 返回项字段 `{name, status, started_at, finished_at, message}` —— Task 8 实现 / Task 9 get_jobs 透传 / Task 11 `JobRow`（name/status/started_at/finished_at/message）三处一致。
- `catchup` 四态 `running/done/failed/not_started` —— Task 9 `_resolve_catchup_state` 产出 / Task 11 `CatchupState` 类型 / Task 12 `catchupTagType`+`catchupStateLabel` switch 分支三处一致。
- `run_startup_catchup` 返回 dict 结构 `{pipeline, brief, pre_open, pre_open_note, error}` —— Task 9 测试 done 用例 / Task 10 测试 done 用例 / Task 12 catchupResultLines 渲染均按此 dict。
- `getJobs(date)` 签名 —— Task 11 定义 / Task 12 调用一致（单参 date，timeout 10000）。
- `get_jobs(date, engine, catchup_task)` 签名 —— Task 9 定义 / Task 10 端点调用一致。
- `/jobs` 端点 `date` 缺省 = `clock.today()` —— Task 10 实现 / Task 10 测试 default_date 用例一致。

### 4. 执行顺序依赖

Phase 1 内 Task 1→7 顺序执行（Task 5 删 ReviewView 致 typecheck FAIL，须 Task 6 立即修 router 后合并验收；Task 5 不单独 commit，已注明）。Phase 2 内 Task 8→9→10→11→12 顺序执行（后端先于前端：Task 11/12 依赖 Task 10 端点契约）。Phase 1 与 Phase 2 可分别验收，互不阻塞——Phase 2 Task 11/12 的 trading.ts getJobs 与 JobCockpitView 是纯增量，不依赖 Phase 1 撤写结果。

