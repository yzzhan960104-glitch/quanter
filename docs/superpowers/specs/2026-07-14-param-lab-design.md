# Spec 2 · Parameter Lab 前端工作台设计（参数训练平台 · Spec 2）

> 2026-07-14 · brainstorm 阶段产出 · 用户已认可，待写实现计划
>
> 依赖：Spec 1 回测异步化（已实现，977caf1→4f25604，700 passed）——本 spec 消费其 SQLite
> 异步任务 API（`POST /replay/async` + `GET /replay/tasks(/{id})` + `POST /replay/tasks/{id}/cancel`）。

## 1. 背景与定位

参数训练平台终态愿景：跑全市场回测 → AI 分析 → 调参 → 再跑，几天闭环训练出最优参数组合（4-spec 分解见项目记忆 `quanter-param-training-platform`）。Spec 1 已把全市场回测异步化（可执行/可观测进度/可取消/可持久化），**Spec 2 是闭环的「人机交互层」**——给研究员一个独立的参数实验室 `/lab`，把「配参 → 提交异步回测 → 看进度 → 审阅结果 → 微调重跑」收敛到一个工作台。

**Spec 2 要解决的问题**：
- Spec 1 的异步 API 已就位，但前端仍走 `/caisen` 视图里的老同步「历史回放」tab（`runReplay` → 同步 `POST /replay`），全市场回测会 HTTP 超时，异步能力无处可用。
- 参数调优缺乏专门的审阅界面：当前回放结果（参数组合 / 收益走势 / 买卖日志）与历史任务混在蔡森筛选页里，迭代闭环不顺。

**现状基线**：
- ✅ Spec 1 异步任务全生命周期 API + SQLite 任务表（`data/replay_tasks.db`）。
- ✅ `GET /caisen/config/schema` 返回 `StrategyConfig.model_json_schema()`，description 已全中文（参数表单单一真相源）。
- ✅ ECharts 主题（`web/src/theme/echarts-terminal-dark.ts`）+ `--qt-*` design token 体系 + utils.css 工具类。
- ✅ `/caisen` 视图（1236 行）内已有一套完整回放结果渲染（equity/统计卡/流水表/形态分布/月度收益）——可抽取复用。
- ⚠️ 老同步 `POST /replay` + `GET /replay/runs`（JSON 归档）链路与异步链路并存；前端 `/caisen` 仍消费老链路。

## 2. 范围与边界

**做（In）**
- 新增独立路由 `/lab`（参数实验室），原生消费 Spec 1 异步任务 API。
- 4 区纵向堆叠画布 +「新建回测」抽屉 + 任务列表 master-detail 交互。
- 后端补 `DELETE /replay/tasks/{task_id}`（清理能力，DB 函数已存在）。
- 下线 `/caisen` 老「历史回放」tab，回放能力收敛到 /lab；顺带清掉 `/caisen` 与老同步链路的死代码 + `caisen.ts` 中前端不再调的老函数。
- AI 建议 = 任务列表里的占位列（Spec 3 填真实 GLM 调用）。

**不做（Out / YAGNI）**
- 不做 SSE/WebSocket 推送——用轮询（无现成推送基建，SQLite + ProcessPool 模型天然适合轮询）。
- 不动老 `/replay/runs`（JSON）后端端点（前端不再调即可，零回归；退役会断 10 个 `run_replay` 测试，收益不抵风险）。
- 不做参数扫描 / 批量提交 / 搜索器 / 自动调参——那是 Spec 4。/lab 一次提交一个异步回测。
- 不做真实 AI 分析调用——Spec 3。

## 3. 已确认决策（brainstorm Q&A 结论）

| 决策点 | 选定 | 理由 |
|---|---|---|
| /lab 与 /caisen 老 tab 关系 | 新建独立 /lab + 下线 /caisen 老「历史回放」tab | 回放能力收敛到 /lab；/caisen 回归纯扫描/审核/激活；合 Spec 1 设计 §9「老 /replay 废弃」+ 记忆「前置：老同步退役」 |
| 历史归档数据源 | /lab 只读 SQLite 任务表（`GET /replay/tasks`） | 单一真相源；老 JSON `/replay/runs` 端点保留但前端不再调（零回归） |
| 清理能力 | 后端补 `DELETE /replay/tasks/{task_id}` | 任务历史不能只增不删；DB 函数 `delete_task` 已存在，仅需薄路由 |
| AI panel 交付程度 | 任务列表内占位列（Spec 3 占位） | 边界清晰、零 scope 蔓延；AI 建议天然按任务维度呈现 |
| 画布布局 | 纵向堆叠（C-v2）：顶部左参数/右走势 → 买卖日志 → 任务列表 | 核心是审阅当前/已选回测；研究员明确诉求：看参数组合 + 收益走势 + 买卖日志 + 任务状态 |
| 参数面板编辑性 | 只读展示 +「新建回测」抽屉（预填当前参数） | 画布专注审阅（贴合「详情」语义）；改参重跑开抽屉，预填当前参数便于微调 |
| 任务列表交互 | master-detail（点行灌入上方三区） | 一处选任务、上方全景审阅，迭代闭环顺 |
| 结果渲染实现 | 抽取复用（A 方案） | 从 /caisen 抽 `ReplayReportPanel.vue`，/lab 引用，无重复 + /caisen 瘦身 |
| 进度观测 | 轮询（~3s，仅存在 PENDING/RUNNING 时） | 无推送基建；YAGNI |

## 4. 路由 / 导航 / 文件结构

**路由** `web/src/router/index.ts`：新增
```ts
const ParamLabView = () => import('../views/ParamLabView.vue')
// routes:
{ path: '/lab', name: 'lab', component: ParamLabView },
```

**导航** `web/src/App.vue` `researchNav`：在「蔡森筛选」后插入（同属研究动线，紧邻）：
```ts
{ to: '/lab', label: '参数实验室', icon: DataAnalysis },   // @element-plus/icons-vue
```

**新增文件**
| 文件 | 职责 |
|---|---|
| `web/src/views/ParamLabView.vue` | 顶层编排：选中任务状态、轮询、master-detail、4 区布局壳；内联「参数详情面板（只读）」「买卖日志表」「任务列表表」 |
| `web/src/components/lab/ReplayReportPanel.vue` | 结果渲染（equity 曲线 ECharts + 统计卡 + 形态分布 + 月度收益 + 流水表），从 /caisen 抽取复用 |
| `web/src/components/lab/NewReplayDrawer.vue` | 「新建回测」抽屉：configSchema 反射的可编辑参数表单（分组折叠）+ start/end/universe + 提交按钮 |
| `web/src/components/lab/paramMeta.ts` | 共享常量：中文标题映射 + 分组（详情面板与抽屉表单共用，见 §8） |

**API client 增量** `web/src/api/caisen.ts`：补异步任务函数 + 类型（见 §6）。

## 5. 画布布局与交互

```
┌─顶栏: 选中任务标识  ｜  状态筛选[全部/运行中/已完成/失败/已取消]  ｜  ＋新建回测 ─┐
├──────────────────────────────┬───────────────────────────────────────────┤
│ 🔵 参数详情（只读）            │ 🟢 收益率走势（ECharts equity 曲线）        │
│   schema 默认 ∪ cfg_override  │   + 统计卡: 命中/胜率/盈亏比/回撤/年化/持仓 │
├──────────────────────────────┴───────────────────────────────────────────┤
│ 📒 买卖日志（trades 表: 标的/形态/买卖日价/离场原因/盈亏比/持仓天）            │
├──────────────────────────────────────────────────────────────────────────┤
│ 📋 任务列表（master）: 状态/区间/标的数/min_rr/命中/胜率/年化/进度/AI[占位]/操作│
└──────────────────────────────────────────────────────────────────────────┘
                                  ↑ 点行 → 灌入上方三区（master-detail）
```

**交互流**：
1. **onMounted**：拉 `GET /config/schema`（参数元信息）+ `GET /replay/tasks`（任务列表）。默认选中最新一条任务（无任务则空态）。
2. **选中任务（master-detail）**：点任务列表行 → `GET /replay/tasks/{id}` 取详情（含 `cfg_override` + `report`）→ 参数面板按 schema 默认 ∪ cfg_override 渲染、走势/统计/日志按 report 渲染。
3. **轮询**：存在 PENDING/RUNNING 任务时，每 3s `GET /replay/tasks`（轻量列表）刷新进度；当选中任务状态变化或转 SUCCESS 时重取其详情（拿 report）。无活跃任务时停轮询（省请求）。
4. **新建回测（抽屉）**：`＋新建回测` 开 `el-drawer`，默认预填当前选中任务的参数（微调重跑）；无选中则填 schema 默认。填 start/end/universe → 提交 `POST /replay/async` → 拿 `task_id` → 关抽屉 → 选中新任务 → 起轮询。
5. **取消**：RUNNING 行「取消」→ `POST /replay/tasks/{id}/cancel` → 继续轮询到 `CANCELLED`（不假设立即生效，防取消竞态）。
6. **删除**：非 RUNNING 行（PENDING / SUCCESS / FAILED / CANCELLED）显示「删除」→ 二次确认 → `DELETE /replay/tasks/{id}` → 刷新列表；删的是选中行则清空选中。**RUNNING 行只显示「取消」不显示「删除」**（防 worker 仍在跑却删了记录造成算力空转：先取消等转 CANCELLED 再删）。

## 6. 前端 API client 增量（`web/src/api/caisen.ts`）

新增类型（对齐 `server/schemas/caisen.py` 的 `ReplayTaskSummary / ReplayTaskDetail / ReplayAsyncRequest / CancelResponse`）：

```ts
export interface ReplayTask {            // GET /replay/tasks 列表项（ReplayTaskSummary）
  task_id: string
  created_at: string
  status: 'PENDING' | 'RUNNING' | 'SUCCESS' | 'FAILED' | 'CANCELLED'
  progress: number                       // 0-100
  start?: string | null
  end?: string | null
  universe_n?: number | null             // -1 = 全市场
  cfg_override?: Record<string, unknown>
}

export interface ReplayTaskDetail extends ReplayTask {   // GET /replay/tasks/{id}
  report?: ReplayReport | null           // SUCCESS 时内嵌完整 ReplayReport
  error?: string | null                  // FAILED 错误信息
  started_at?: string | null
  finished_at?: string | null
  last_heartbeat?: string | null
}

export interface ReplayAsyncRequestBody { start: string; end: string; universe?: string[] | null; cfg_override?: Record<string, unknown> }
export interface CancelResponse { task_id: string; cancelled: boolean; message: string }
```

新增函数：

| 函数 | 端点 | 返回 | 超时 |
|---|---|---|---|
| `submitReplayAsync(body)` | POST `/caisen/replay/async` | `{ task_id: string }` | 10s |
| `listReplayTasks(status?)` | GET `/caisen/replay/tasks` | `ReplayTask[]` | 10s |
| `getReplayTask(id)` | GET `/caisen/replay/tasks/{id}` | `ReplayTaskDetail` | 10s |
| `cancelReplayTask(id)` | POST `/caisen/replay/tasks/{id}/cancel` | `CancelResponse` | 10s |
| `deleteReplayTask(id)` | DELETE `/caisen/replay/tasks/{id}`（新后端） | `{ ok: boolean }` | 10s |

`getConfigSchema()` 与 `ReplayReport`/`EquityPoint`/`Trade` 类型复用现有。任务详情 `report` 即完整 `ReplayReport`（SUCCESS 时），走势/统计/日志一次取齐，无需二次请求。

## 7. 后端增量（仅 1 个端点）

`server/api/v1/caisen.py` 新增（DB 函数 `replay_tasks_db.delete_task` 已存在，仅薄路由）：

```python
@router.delete("/replay/tasks/{task_id}", summary="删除异步任务记录")
def delete_replay_task(task_id: str) -> Dict[str, bool]:
    """删除单条异步任务（清理历史）。task_id 不存在 → 404（与 get 同源契约）。"""
    ok = replay_tasks_db.delete_task(task_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"任务不存在：task_id={task_id!r}")
    return {"ok": True}
```

老 `/replay/runs` 端点不动。**这是本次唯一后端改动**。

## 8. 中文参数映射方案（`paramMeta.ts`）

`caisen/config.py` 的 Field `description` 已是中文但很长（含 rationale），不适合做表单 label。Spec 要求「中文映射」——用一个**扁平 dict（非框架）**，覆盖 `StrategyConfig` 全部字段，按 config.py 7 大分组归类：

```ts
export type ParamGroup = '时间跨度' | '空间高度' | '量价配合' | '交易执行' | '时间止损' | '风控' | '蔡森方法学'
export const PARAM_GROUPS: ParamGroup[] = ['时间跨度','空间高度','量价配合','交易执行','时间止损','风控','蔡森方法学']
export const PARAM_META: Record<string, { title: string; group: ParamGroup }> = {
  // 时间跨度
  min_pattern_bars:    { title: '形态最小跨度', group: '时间跨度' },
  max_pattern_bars:    { title: '形态最大跨度', group: '时间跨度' },
  symmetry_tolerance:  { title: '左右时间对称容忍度', group: '时间跨度' },
  // 空间高度
  zigzag_threshold_atr:    { title: 'ZigZag 波段阈值(ATR倍)', group: '空间高度' },
  min_pattern_depth:       { title: '形态最浅幅度', group: '空间高度' },
  max_pattern_depth:       { title: 'W底最深幅度阈值', group: '空间高度' },
  hs_max_pattern_depth:    { title: '头肩底深度宽阈值', group: '空间高度' },
  w_price_tolerance:       { title: 'W底两底价格容忍度', group: '空间高度' },
  // 量价配合
  right_vol_shrink:        { title: '右底缩量比例上限', group: '量价配合' },
  breakout_vol_multiplier: { title: '突破放量倍数', group: '量价配合' },
  // 交易执行
  pullback_window_bars:    { title: '回踩触发窗口(K线)', group: '交易执行' },
  pullback_max_pct:        { title: '回踩最高价容忍%', group: '交易执行' },
  stop_loss_atr_buffer:    { title: '止损ATR缓冲', group: '交易执行' },
  min_rr_ratio:            { title: '盈亏比下限', group: '交易执行' },
  // 时间止损
  max_holding_bars:        { title: '最大持仓周期', group: '时间止损' },
  timeout_exit_threshold:  { title: '超时砍亏浮盈阈值', group: '时间止损' },
  trailing_activation_bars:{ title: '移动止盈激活天数', group: '时间止损' },
  trailing_to_breakeven:   { title: '移动止盈锁本金', group: '时间止损' },
  // 风控
  liquidity_min_amount: { title: '流动性成交额下限', group: '风控' },
  hv_window:            { title: '历史波动率窗口', group: '风控' },
  hv_max_quantile:      { title: 'HV异常分位上限', group: '风控' },
  max_position_pct:     { title: '单标的占总资金上限', group: '风控' },
  macro_regime_veto:    { title: '宏观收缩期一票否决', group: '风控' },
  confirm_bars:         { title: 'ZigZag末尾pivot确认窗', group: '风控' },
  // 蔡森方法学
  neckline_height_multiple:  { title: '颈线满足级数n', group: '蔡森方法学' },
  abc_wave_detect:           { title: 'ABC波过程识别', group: '蔡森方法学' },
  right_above_left:          { title: '右脚>左脚硬规则', group: '蔡森方法学' },
  ma26w_filter:              { title: '26周线打底过滤', group: '蔡森方法学' },
  ma26w_window:              { title: '26周线计算窗口', group: '蔡森方法学' },
  pattern_tension_ratio:     { title: '幅宽张力比例下限', group: '蔡森方法学' },
  pattern_width_bonus:       { title: '幅宽加分', group: '蔡森方法学' },
  enable_pot_breakout:       { title: '启用破头锅形态', group: '蔡森方法学' },
  enable_bottom_flip:        { title: '启用破底翻形态', group: '蔡森方法学' },
  enable_triangle_bottom:    { title: '启用收敛三角底', group: '蔡森方法学' },
  triangle_max_pattern_depth:{ title: '三角边长比上限', group: '蔡森方法学' },
  triangle_breakout_min:     { title: '三角突破进度下限', group: '蔡森方法学' },
  triangle_breakout_max:     { title: '三角突破进度上限', group: '蔡森方法学' },
  false_breakout_threshold:  { title: '假突破跌破阈值', group: '蔡森方法学' },
  false_breakout_window:     { title: '假突破判定窗口', group: '蔡森方法学' },
}
```

- **详情面板（只读）**：遍历 `schema.properties` → 标题取 `PARAM_META[name].title`，值取 `cfg_override[name] ?? schema.default`，长 `description` 作 tooltip；按 `PARAM_GROUPS` 分组展示。
- **抽屉表单（可编辑）**：同遍历，按分组折叠渲染输入框（int/float→`el-input-number`、bool→`el-switch`），绑定 `cfg_override`，标注 `ge/le` 约束 + 默认值 + 每组「恢复默认」。

一处定义、详情与表单共用；前后端参数名仍同源（schema 反射），杜绝漂移。`PARAM_META` 与 `StrategyConfig` 字段的同步由「config.py 加字段时 paramMeta.ts 同步补条目」守护（测试断言 schema.properties 键集 ⊆ PARAM_META 键集，见 §11）。

## 9. 组件复用 + 老 tab 退役

**抽取**：把 `CaisenScreenView.vue` 现有回放结果渲染（equity ECharts / 统计卡 / 流水表 / 形态分布 / 月度收益）抽成 `components/lab/ReplayReportPanel.vue`，props 接收 `report: ReplayReport`，/lab 引用。无重复 + /caisen 瘦身。

**/caisen 清理**（`web/src/views/CaisenScreenView.vue`）：
- 删「历史回放」tab-pane 模板 + 相关 state（`replayForm` / `replaying` / `replayReport` / `replayRuns` / `currentRunId`）+ 方法（`onReplay` / `loadReplayRuns` / `loadReplayRun` / `deleteReplayRun` / replay 相关 computed 如月度排序/形态分布/equity option）。
- `activeTab`（原 `'review'|'replay'`）随回放 tab 移除而失去意义——**移除整个 tab 结构**，审核区（候选计划表 + approve/reject/activate）直接铺平为底部主区。底部 `el-tabs` 退化为单区，不再需要 tab 切换。

**`caisen.ts` 死代码清理**：移除前端不再调用的老同步函数——`runReplay` / `listReplayRuns` / `getReplayRun` / `deleteReplayRun` 及类型 `ReplayRunSummary` / `ReplayRunDetail` / `ReplayRequestBody`。保留 `getConfigSchema`（/lab 用）、`ReplayReport`/`EquityPoint`/`Trade`（任务详情 report 复用）。

**`caisen.spec.ts` 同步**：移除已删函数的用例，补异步任务新函数的请求/响应用例。

## 10. 轮询策略

- 触发：`listReplayTasks()` 返回含 PENDING 或 RUNNING 时启动；全终态时停止。
- 间隔：3s（平衡进度感知与请求频次；进度每 50 symbol 上报一次，3s 足够平滑）。
- 优化：轮询列表（轻量）检测状态变化；仅当选中任务 status 变化或 progress 增加时才重取其详情（`getReplayTask`）拿 report，避免每 tick 拉 report。
- 生命周期：`onMounted` 启、`onUnmounted` 清（防泄漏）；切走路由清定时器。
- 容错：单次轮询网络失败静默 + `logger.error`，不中断轮询、不崩画布。

## 11. 边界与错误处理（量化风控拷问）

| 场景 | 处置 |
|---|---|
| 轮询网络抖动 | 单次失败静默 + logger，下个 tick 续轮询，画布不崩 |
| 选中任务被删 / 404 | 清空选中 + `ElMessage` 提示 + 刷新列表 |
| 提交 422（参数非法） | 抽屉保留，展示后端字段错误，不关抽屉 |
| FAILED 任务 | 走势区不画图，改展示 `error` + 「重提」入口（开抽屉预填该任务参数） |
| PENDING / RUNNING 选中 | 走势区显示进度占位（"回测中 60%"），日志区空，参数面板照常显示 cfg |
| 取消竞态（Spec1 §7） | 取消后继续轮询到 `status=CANCELLED`，不假设立即生效 |
| worker 崩溃 | 任务转 `FAILED('进程重启中断')`，UI 标红 + error + 提示手动重提 |
| 空态 | 无任务 → `.qt-empty`「点 ＋新建回测 开始第一次实验」 |
| NaN 守护 | 复用既有 `StrictJSONResponse` 全局防线；ECharts option 内裸色值是既定例外 |
| 全市场回测时长 | 异步已破性能墙；UI 不阻塞，靠轮询观测 |

## 12. 测试策略（E2E 为金标准，记忆 `default-e2e-after-ui`）

- **后端单测**（`tests/test_caisen_api.py`）：`DELETE /replay/tasks/{id}` —— 200 `{ok:true}` / 不存在 404。后端薄路由不加状态守卫（delete_task 直删任意行）；「不删 RUNNING」是**前端 UX 约定**（RUNNING 行无删除按钮），非后端硬约束。
- **paramMeta 同步守护**（前端单测）：断言 `Object.keys(schema.properties)` ⊆ `Object.keys(PARAM_META)`，防 config.py 加字段漏补中文标题。
- **前端单测**（`caisen.spec.ts`）：异步任务新函数的请求 URL/body/响应类型化（mock apiClient）；移除已删老函数用例。
- **E2E**（新建 `tests/e2e/lab_param_lab.py`）：`with_server` 起前后端 → 访问 `/lab` → 断言无 `pageerror` + 关键 selector 渲染（参数面板 / 走势容器 / 任务列表 / ＋新建回测）→ 开抽屉填小 universe（如 3 只标的、短区间）+ 改一个参数 → 提交 → 轮询等 `SUCCESS` → 断言走势 / 统计 / 日志渲染 → 截图留证。
- **/caisen 退役 E2E**（改 `tests/e2e/caisen_replay_tab.py`）：断言回放 tab 已移除、扫描 / 审核 / 激活链路仍正常（不回归）。
- **既有 700 pytest**：本次后端仅加 DELETE 路由，不动 service/replay 核心，应保持全绿。

## 13. design token 与样式约束（记忆 `quanter-ui-design-tokens`）

- 新增 CSS 一律 `var(--qt-*)` + utils.css 工具类（`.qt-card` / `.qt-view-shell` / `.qt-section-title` / `.qt-empty`），**禁止裸 hex**。
- A 股红涨绿跌用 `--qt-up`(#ef5350) / `--qt-down`(#26a69a)，独立于 EP `--el-color-success/danger`。
- 例外（保留裸 hex）：ECharts option 内色值（canvas 不解析 CSS var）、`rgba()`、纯黑白。
- ECharts 复用 `web/src/theme/echarts-terminal-dark.ts` 主题。

## 14. 未确认项

无。本 spec 已含全部 brainstorm 决策结论，可进入实现计划。

---

状态：用户已认可（2026-07-14）→ commit → 转 writing-plans 分解实现任务。
