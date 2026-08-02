# 前端只读化 + 作业驾驶舱 设计文档

- 日期：2026-08-02
- 范围：`presentation/web/`（前端 Vue3 工程）+ `presentation/server/`（后端 FastAPI，仅新增 1 个只读端点）+ `trading/job_ledger.py`（仅新增 1 个只读查询函数）
- 关联迭代：C-8 全 job 启动补跑（`28a3082a`）的可观测出口；前端对齐后端 C1-C8 / gap4 / live 修复迭代

---

## 1. 背景与动机

后端在 C1-C8 / gap4 / live 主链路修复这一轮迭代中新增了大量可观测能力，但绝大部分停留在内部 SQLite 表 / daemon 子进程 / scheduler（如 job 台账 `logs/trading_job_run.db`、启动补跑 `app.state.catchup_task`、pre_open gate 拒因），前端完全看不到——这是"前端没跟上"的字面真相。

同步对齐过程中，研究员明确将**前端整体只读**确立为系统的安全姿态：前端永远不碰钱、不碰状态变更，所有写操作（下单/激活/同步/回测/熔断）走受控脚本 / CLI / QMT 客户端 / cron 自动，并留审计。这既是对齐后端迭代的契机，也是一次前端职责边界的硬性收敛。

本 spec 合并两块紧密相关的工作：
- **Phase 1 · 前端只读化**：撤除所有写 UI 与写调用
- **Phase 2 · 作业驾驶舱**：新增纯观测视图，把 C-8 的 job 台账 / 启动补跑 / gate 拒因暴露给前端

---

## 2. 决策记录（brainstorm 结论）

| 决策点 | 结论 | 理由 |
|---|---|---|
| 只读覆盖范围 | **整个前端系统**只读 | 研究员确立的安全姿态：UI 层永不写 |
| 后端写端点 | **全部保留** | 给脚本/CLI/QMT 客户端用；删端点风险大、误伤现有脚本依赖 |
| 应急熔断 | 前端按钮撤，**不建脚本**，走 QMT 客户端手动断开/撤单 | 研究员风险偏好（已知风险，见 §8） |
| ReviewView | **整个移除**（路由+导航+组件+api 文件） | 其唯一功能 `diagnose` 是写操作，撤后空壳 |
| A 类手动补跑 | **撤销**（前端只读），退化为纯观测 | 与"前端只读"原则一致 |
| 启动补跑进度 | 只暴露 task 整体状态，不做阶段级 SSE 进度 | 不动 `catchup.py` 业务码；轮询够用 |
| 手动补跑 run_eod | （随手动补跑一并撤销） | — |

---

## 3. 总体架构

两层最小改动，共享"前端只读"原则：

- **后端**：几乎零改动。仅 `trading/job_ledger.py` 新增 1 个只读查询函数，`presentation/server/` 新增 1 个 GET 端点（Phase 2）。所有现有 POST 写端点原样保留。
- **前端**：撤除全部写 UI 与写调用（Phase 1）+ 新增 1 个纯观测视图（Phase 2）。

实施可按 Phase 顺序分阶段验收：Phase 1 先落地"前端只读"硬约束，Phase 2 再补作业观测出口。

---

## 4. Phase 1 · 前端只读化

### 4.1 视图层撤除/保留矩阵

| 视图 | 撤除（写操作） | 保留（只读） |
|---|---|---|
| `LiveCockpitView.vue` | connect/disconnect 按钮、下单面板（submitOrder）、撤单（cancelOrder）、紧急熔断按钮（emergencyHalt） | 心跳灯（getStatus 2s 轮询）、资产卡（getAsset）、订单回报列表（getOrders）、持仓 Treemap（getPositions）、流水分页查询（queryTrades）、CSV 导出（exportLiveTrades，导出即读） |
| `CaisenScreenView.vue` | 扫描（scan）、审核 approve/reject（reviewPlan）、激活挂单（activatePlan） | 候选计划列表（listPlans）、计划详情（getPlan）、K 线图（getChart） |
| `ParamLabView.vue` | 新建回测抽屉 `NewReplayDrawer.vue`（submitReplayAsync）、取消任务（cancelReplayTask）、删除任务（deleteReplayTask） | 参数 schema 查看（getConfigSchema）、任务列表（listReplayTasks）、任务详情+收益曲线（getReplayTask） |
| `DataLakeView.vue` | 立即同步按钮（triggerSync） | 数据集表格（getDatasets）、健康轮询 |
| `ReviewView.vue` | **整个移除**（路由 / 导航 / 组件 / api 文件） | — |
| `CockpitView.vue` | （本身只读，零改） | 6 个子组件全保留（StatusCard / AssetCard / DataHealthCard / TradesTable / TerminalLogs / ReplayCompare，均只 GET/SSE） |
| `DashboardView.vue` | （只读，零改） | getSectorFlow |

**业务连续性说明**（撤前端写入口后，各业务如何继续）：
- 实盘下单：盘前由 `pre_open` cron（09:22）自动挂单；手动补挂走 `trading/tools/trigger_pre_open_once.py`；任意单走 QMT 客户端
- 蔡森审核/激活：`veto_plan.py` 提供否决；approve/activate 走 EOD 事件链自动产出 + pre_open 自动挂单
- 数据同步：`data/tools/sync_*.py` 脚本 + daemon 子进程 + `run_daily_incremental.bat` 等 schtasks 入口
- 回测提交：backtest 域脚本/CLI（前端只看结果）
- 应急熔断：QMT 客户端手动断开/撤单（见 §8 已知风险）

### 4.2 API 层清理

| 文件 | 删除 | 保留 |
|---|---|---|
| `src/api/trading.ts` | connect / disconnect / submitOrder / cancelOrder / emergencyHalt + 随之失去引用的 `SubmitOrderBody` 类型 | getStatus / getPositions / getAsset / getOrders / queryTrades / exportLiveTrades |
| `src/api/caisen.ts` | scan / reviewPlan / activatePlan / submitReplayAsync / cancelReplayTask / deleteReplayTask | listPlans / getPlan / getChart / getConfigSchema / listReplayTasks / getReplayTask |
| `src/api/data.ts` | triggerSync | getDatasets |
| `src/api/review.ts` | **整文件删除** | — |
| `src/api/macro.ts` | （零改） | getSectorFlow |

> `OrderResultRow` 若仅被撤除的下单/撤单函数使用则一并清理；`OrderRow`（GET /orders 回报）仍被保留的 `getOrders` 使用，需保留。清理时以 `vue-tsc` typecheck 无未使用引用为准。

### 4.3 路由与导航

- `src/router/index.ts`：移除 `/review` 路由项与 `ReviewView` import；新增 `/jobs` 路由项（懒加载 `JobCockpitView`，Phase 2）
- `src/App.vue` 顶部导航：移除「AI 复盘」项；实盘段新增「作业驾驶舱」项（置于「综合看板」之后）
- **全局只读徽标**：顶栏常驻 `READ-ONLY 只读` 红色徽标，明示当前前端无任何写能力——避免使用者误以为"按钮消失"是 bug，也避免日后误判哪些操作该在前端做

### 4.4 撤按钮后的 UI 处置原则

**不留守死的灰按钮**。写操作所在的整个面板/区块直接移除；若移除后留下明显视觉空洞，则在该位置替换为一行只读提示，说明该业务的替代入口。例如：
- 原下单面板位置 → 「下单请赴 QMT 客户端 / 盘前由 pre_open cron 自动挂单」
- 原熔断按钮位置 → 「应急熔断请赴 QMT 客户端手动断开」
- 原数据同步按钮位置 → 「数据同步由 daemon + cron 自动触发」
- 原 ReviewView 路由 → 直接移除，不留空页面

### 4.5 后端

**零改动**。所有 POST 写 router（trading / caisen / data / review）原样保留，供脚本/CLI/QMT 客户端继续调用。

---

## 5. Phase 2 · 作业驾驶舱（A 类 · 纯观测）

### 5.1 后端

**`trading/job_ledger.py`** 新增只读查询函数（唯一动 trading 处，零副作用）：

```python
def snapshot_for_date(business_date: str, path: Optional[str] = None) -> list[dict]:
    """读某业务日全部 job 的最新台账行（只读 SELECT，不改状态机）。
    返回 [{name, status, started_at, finished_at, message}, ...]。无记录返 []。
    与 job_ledger「写失败绝不影响交易关键路径」的设计约束一致。"""
```

**`presentation/server/services/trading_service.py`** 新增薄封装：

```python
def get_jobs(date: str, engine, catchup_task) -> dict:
    """聚合当天 job 台账 + 启动补跑 task 状态。
    - 台账：job_ledger.snapshot_for_date(date)
    - catchup：探测 catchup_task（task.done()/task.exception()）→ state in
      {running, done, failed, not_started}；done/failed 时附 result dict。
    engine/catchup_task 由 router 从 app.state 传入，service 保持无状态。"""
```

**`presentation/server/api/v1/trading.py`** 新增端点：

```
GET /api/v1/trading/jobs?date=YYYY-MM-DD    date 缺省 = clock.today()
```

router 通过 `request.app.state.trading_engine` 与 `request.app.state.catchup_task` 取实例传 service。

> 不新增任何 POST 端点（前端只读，手动补跑撤销）。

### 5.2 端点契约

```jsonc
// GET /api/v1/trading/jobs?date=2026-08-02
{
  "date": "2026-08-02",
  "jobs": [
    {"name": "pipeline", "status": "done",    "started_at": "...", "finished_at": "...", "message": ""},
    {"name": "pre_open", "status": "skipped", "started_at": "...", "finished_at": "...", "message": "gate③ 未过：data_ready 未就绪（T 日缺失）"}
  ],
  "catchup": {
    "state": "running",   // running | done | failed | not_started
    "result": null         // done/failed 时填 run_startup_catchup 返回的 dict
  }
}
```

`jobs[].status` 状态语义沿用 job_ledger：`running / done / skipped / failed`。`message` 字段即 gate 拒因（pre_open skipped 时由 engine 写入"gate③ 未过：…"），是驾驶舱最有价值的信息——让研究员一眼看清"今天为何没挂单"。

### 5.3 前端

- `src/api/trading.ts` 新增 `getJobs(date)` + 类型 `JobRow` / `CatchupState`
- `src/views/JobCockpitView.vue` 新增纯观测视图，两块布局：
  - ① **job 状态表**：pipeline / pre_open 当天 status 着色（done 绿 / skipped 黄 / failed 红 / running 蓝）+ message（gate 拒因）完整展示
  - ② **启动补跑卡**：catchup.state 动画（running 旋转）+ done/failed 后展示 result 详情（pipeline / brief / pre_open 各子任务结果）
  - **无任何操作按钮**，刷新策略：常态 15s 轮询；启动补跑期间（开机头几分钟，catchup.state=running）降到 3s
- `onBeforeUnmount` 显式 `clearInterval`，与现有视图防泄漏纪律一致

---

## 6. 错误处理

| 场景 | 处置 |
|---|---|
| job_ledger 读失败（DB 锁/损坏） | 端点返 200 + `jobs: []` + `warning` 字段（台账是操作元数据，绝不阻断主路径，与 job_ledger 设计约束一致） |
| engine 未装配（lifespan 失败） | `catchup.state = "not_started"`，不报错 |
| catchup_task 未创建（非生产/未到 lifespan） | `catchup.state = "not_started"` |
| catchup_task 抛异常 | `catchup.state = "failed"` + `result.error` 填异常信息 |
| 前端轮询遇 5xx/网络错误 | `client.ts` 已有统一 Toast + 不崩，轮询继续 |
| 撤按钮后用户误触已移除的入口 | 全局 READ-ONLY 徽标 + 各位置只读提示文案兜底 |

---

## 7. 测试策略

### 7.1 前端
- **改造视图组件测试更新**：LiveCockpitView / CaisenScreenView / ParamLabView / DataLakeView 各自的现有组件测试（vitest），移除对已撤写操作的断言（按钮存在/交互），保留只读渲染断言不退化
- **删除 ReviewView 相关测试**（组件测试 + 路由测试中 `/review` 断言）
- **JobCockpitView 新增组件测试**：状态着色、gate 拒因展示、catchup 状态切换、轮询启停、`onBeforeUnmount` 清理定时器
- **路由测试**：`/review` 移除、`/jobs` 新增
- **typecheck**：`vue-tsc --noEmit` 全过（含清理未使用类型/函数后的零 warning）

### 7.2 后端
- `job_ledger.snapshot_for_date` 单测（tmp DB，含空表/多 job/单 job 场景）
- `GET /api/v1/trading/jobs` 端点集成测试（tmp DB + fake `app.state.trading_engine` / `catchup_task`，覆盖 running/done/failed/not_started 四态）

---

## 8. 已知风险（书面留痕）

1. **应急熔断无前端快速入口**：前端按钮撤除且不建脚本，应急依赖 QMT 客户端手动断开/撤单。盘中策略失控时存在操作延迟风险。**对冲措施**：Phase 2 作业驾驶舱的 pre_open gate 拒因强提示，让研究员在前端就能提前发现"今天为何没挂单 / 数据是否就绪"，减少需要应急的概率。研究员已明确接受此风险偏好。
2. **多项业务无前端入口**：蔡森审核/激活、回测提交、手动下单、数据同步触发——均需通过脚本/CLI/QMT 客户端/cron 自动进行。已记入 §4.1 业务连续性说明。

---

## 9. 不做（YAGNI 明确）

- ❌ 不建熔断脚本（`scripts/kill_switch.py` 之类）
- ❌ 不删后端任何写端点
- ❌ 不加 env 只读开关（READ_ONLY 403 之类，过度设计）
- ❌ 不做启动补跑 SSE 实时阶段进度（轮询 task 整体状态够用）
- ❌ 不收编 `post_open` / `post_close` 进台账（YAGNI，需时再加）
- ❌ 不补下单/激活/回测/同步的独立 CLI（走现有脚本 + QMT 客户端 + cron）
- ❌ 不做历史趋势聚合/多日对比（GET /trading/jobs 的 `date` 参数保留单日查询灵活性、缺省 today；前端默认只查当天，多日趋势后续再议）

---

## 10. 后续阶段预告（B / C 类，独立 spec）

本次 A 类（作业驾驶舱）完成后，按既定"分阶段全做"路线：
- **B 类**：实盘交易语义补全——熔断粘滞锁状态（区分健康 live vs `_risk_halted` 粘滞锁）、持仓 drift（position_book 本地侧 vs broker 真实侧）。均只读端点 + 前端展示。
- **C 类**：策略/复盘产物归档——review_report T 日归档查看、discovery 冠军/轮次/converged。均只读端点 + 前端展示。

B/C 类同样遵循本 spec 确立的"前端只读"原则，不引入新的写操作。
