/**
 * 蔡森形态学流水线 API 封装（Phase 3 · Task 7）
 *
 * 对应后端 server/api/v1/caisen.py。复用 client.ts 的 apiClient（单例）。
 *
 * 物理定位（CLAUDE.md 极简 + 显式原则）：
 *   本模块是蔡森流水线"前端契约层"——把后端 REST 端点的请求/响应封装为类型化的
 *   TS 函数，供 CaisenScreenView 消费。零业务逻辑，纯 HTTP 通道 + 类型守护。
 *
 *   后端契约（server/schemas/caisen.py）：
 *     POST   /caisen/scan                     → List[CandidatePlan]
 *     GET    /caisen/plans?status=            → List[CandidatePlan]
 *     GET    /caisen/plans/{plan_id}          → CandidatePlan
 *     PATCH  /caisen/plans/{plan_id}          → CandidatePlan（approve/reject + edits）
 *     POST   /caisen/plans/{plan_id}/activate → CandidatePlan（APPROVED → ARMED）
 *     GET    /caisen/plans/{plan_id}/chart    → ChartData（candles/markers/priceLines）
 *     GET    /caisen/positions                → { positions: [] }（占位）
 *     GET    /caisen/config/schema            → Record<string,unknown>（/lab 参数表单反射用）
 *     POST   /caisen/replay/async             → { task_id }（Spec 2 异步回测，/lab 消费）
 *     GET    /caisen/replay/tasks             → List[ReplayTask]
 *     GET    /caisen/replay/tasks/{id}        → ReplayTaskDetail
 *     POST   /caisen/replay/tasks/{id}/cancel → CancelResponse
 *     DELETE /caisen/replay/tasks/{id}        → { ok: true }
 *   注：老同步 POST /caisen/replay + GET/DELETE /caisen/replay/runs 端点后端仍保留（异步
 *   worker + 潜在 CLI 入口复用），但前端在 Spec 2 Task 8 后不再消费（老 /caisen 回放 tab 已下线）。
 *
 * 状态机（与 storage 状态机严格同源，前端只读消费，不本地推断）：
 *   PENDING_APPROVAL → APPROVED → ARMED → FILLED → CLOSED
 *                                ↘ REJECTED
 *
 * lightweight-charts 契约对齐（Task 6 viz_interactive.build_chart_data）：
 *   - candles: {time, open, high, low, close}[]（time 为 ISO 日期字符串或 UNIX 秒）
 *   - markers: {time, position: 'aboveBar'|'belowBar'|'inBar', color, shape, text}[]
 *   - priceLines: {price, color, lineWidth, lineStyle, axisLabelVisible, title}[]
 *   后端已生成 lightweight-charts 原生格式，前端零转换直接 setData/setMarkers/createPriceLine。
 */
import { apiClient } from './client'

// ============ 响应类型（字段对齐 server/schemas/caisen.py） ============

/**
 * 蔡森候选交易计划（字段对齐 server.schemas.caisen.CandidatePlan / caisen.plan.TradePlan）。
 *
 * 物理意图：一个"已生成完成的交易计划快照"，前端表格直接渲染，只读消费不做二次推导。
 * 盈亏比/止损位/满足点等数学内核已在 Phase 2 plan.py 完成，本层仅做契约封装。
 */
export interface CandidatePlan {
  plan_id: string
  symbol: string
  symbol_name: string                        // 企业名（#1，后端 data.symbol_names 填充，降级空串兜底显代号）
  pattern_type: string                       // ∈ {"w_bottom", "head_shoulder"}
  formed_at: string                          // ISO 字符串（形态成立日）
  breakout_price: number                     // 突破价（C 波高点）
  neckline_price: number                     // 颈线（W 底两高点连线）
  bottom_price: number                       // C 波低点（谷底）
  entry_upper: number                        // 回踩挂单区间上界（= breakout_price）
  entry_lower: number                        // 回踩挂单区间下界（= breakout × (1-pullback)）
  stop_loss: number                          // 止损（谷底 - buffer×ATR）
  take_profit: number                        // 第一波满足（颈线 + 1×H）
  take_profit_2x: number                     // 第二波满足（颈线 + 2×H）
  rr_ratio: number                           // 盈亏比（≥ min_rr_ratio 才会出现）
  valid_until: string                        // 回踩触发窗口截止（ISO）
  max_holding_until: string                  // 时间止损截止日（ISO）
  shares: number                             // 分配股数（A 股整手）
  status: string                             // 状态机当前态
}

/**
 * lightweight-charts K 线数据点。
 *
 * 字段对齐 TradingView lightweight-charts CandlestickData：time 支持 ISO 日期字符串
 * （业务日频）或 UNIX 秒；open/high/low/close 为 OHLC。
 */
export interface Candle {
  time: string | number
  open: number
  high: number
  low: number
  close: number
}

/**
 * lightweight-charts 标记（标注形态关键点：W 底四点/颈线突破/回踩入场/止损触发等）。
 *
 * 字段对齐 SeriesMarker：position 决定标记位于 K 线上方/下方/内部；
 * shape ∈ {circle, square, arrowUp, arrowDown}；color 与 shape 配合标识事件类型。
 */
export interface ChartMarker {
  time: string | number
  position: 'aboveBar' | 'belowBar' | 'inBar'
  color: string
  shape: 'circle' | 'square' | 'arrowUp' | 'arrowDown'
  text: string
}

/**
 * lightweight-charts 价位线（止损/止盈/颈线/突破价/满足点水平虚线）。
 *
 * 字段对齐 CreatePriceLineOptions：lineStyle ∈ {0=Solid,1=Dotted,2=Dashed,3=LargeDashed,4=SparseDotted}。
 * 后端 _fallback_price_lines 与 viz_interactive 均产出此契约。
 */
export interface PriceLine {
  price: number
  color: string
  lineWidth?: number
  lineStyle?: number
  axisLabelVisible?: boolean
  title?: string
}

/**
 * GET /caisen/plans/{plan_id}/chart 响应：lightweight-charts 渲染所需全量数据。
 *
 * 物理意图：
 *   - 顶层附带 plan 基本字段（symbol/pattern_type/关键价位）供前端快速访问；
 *   - candles/markers/priceLines 三段对应 lightweight-charts 三个 API 入参；
 *   - 当 price_data 不可装配（data_lake 未接）时 candles/markers 为空，
 *     仅 priceLines 有值（降级仍能画关键价位，不白屏）。
 */
export interface ChartData {
  // 顶层 plan 基本字段（前端快速渲染用）
  plan_id?: string
  symbol?: string
  pattern_type?: string
  breakout_price?: number
  neckline_price?: number
  bottom_price?: number
  entry_upper?: number
  entry_lower?: number
  stop_loss?: number
  take_profit?: number
  take_profit_2x?: number
  // lightweight-charts 三段契约
  candles: Candle[]
  markers: ChartMarker[]
  priceLines: PriceLine[]
}

/** 资金曲线点（年化收益曲线图数据）。字段对齐 backtest_replay._compute_stats equity_curve 项。 */
export interface EquityPoint {
  date: string                                // exit_date（ISO 或 index 字符串）
  cumulative_rr: number                       // 截至该笔的累计 rr
  equity: number                              // 归一化资金曲线（equity_0=1.0）
}

/** 单笔买卖流水（前端流水表行）。字段对齐 backtest_replay._compute_stats trades 项。 */
export interface Trade {
  symbol: string
  pattern_type: string
  entry_date: string
  entry_price: number
  exit_date: string
  exit_price: number
  exit_reason: string                         // take_profit / stop_loss / timeout / still_open
  rr: number
  holding_bars: number
}

/**
 * 回放报告（字段对齐 server.schemas.caisen.ReplayReportResponse / caisen.ReplayReport）。
 *
 * 物理意图：历史区间滚动回放的聚合统计——前端独立 tab 展示胜率/盈亏比/回撤，
 * 用于策略参数调优（如数据驱动地校准 min_rr_ratio）。
 */
export interface ReplayReport {
  n_hits: number                              // 命中（成交）交易笔数
  win_rate: number                            // 胜率（盈利笔数 / n_hits）
  avg_rr: number                              // 平均盈亏比
  max_drawdown: number                        // 最大回撤（基于累计 rr，负值）
  pattern_dist: Record<string, number>        // 形态分布 {"w_bottom": x, ...}
  monthly_returns: Record<string, number>     // 月度收益（按 entry 月份聚合）
  avg_holding_bars: number                    // 平均持仓天数
  min_rr_ratio_recommendation: string         // 数据驱动的 min_rr_ratio 建议（中文）
  equity_curve: EquityPoint[]                 // 资金曲线（年化收益曲线图，按 exit_date 排序）
  trades: Trade[]                             // 买卖流水（前端流水表，逐笔 entry/exit/rr）
  annualized_return: number                   // 年化收益 CAGR = equity_end^(252/n_trading_days)-1
  n_trading_days: number                      // 回放区间交易日数（CAGR 时间维度）
  run_id?: string | null                      // 落盘后回填（save=true）；未落盘为 null
}

// ============ 请求体类型 ============

/** POST /caisen/scan 请求体（对齐 ScanRequest）。 */
export interface ScanRequestBody {
  date: string                                // 扫描交易日（YYYY-MM-DD）
  universe: string[]                          // 标的池（symbol 列表）
  cfg_override?: Record<string, unknown>      // 策略参数增量覆盖（空 = 默认配置）
}

/** PATCH /caisen/plans/{plan_id} 请求体（对齐 PlanReview）。 */
export interface PlanReviewBody {
  action: 'approve' | 'reject'
  edits?: Record<string, unknown>             // 字段微调（仅 approve 时生效）
}

// ============ 异步回测任务类型（Spec 1 SQLite 任务表，Spec 2 /lab 消费） ============

/**
 * 异步任务状态机（对齐 server.schemas.caisen.ReplayTaskSummary.status）。
 *
 * 物理意图（CLAUDE.md 风控拷问·状态机边界）：
 *   PENDING  → 任务已落 SQLite，等待 worker 取走（ProcessPoolExecutor 调度槽满时驻留）
 *   RUNNING  → worker 已领任务，last_heartbeat 持续更新（心跳超时由调度器回收）
 *   SUCCESS  → 全 universe 处理完，report 已内嵌（前端取详情一次性拿全）
 *   FAILED   → 异常终止，error 字段填充 Python 堆栈摘要
 *   CANCELLED→ 用户 POST cancel 置 abort flag，worker 下次轮询感知后置此态
 *
 * 前端只读消费，不做状态机推断（不本地猜「RUNNING 超 X 秒就算超时」——以心跳/finished_at 为准）。
 */
export type ReplayTaskStatus = 'PENDING' | 'RUNNING' | 'SUCCESS' | 'FAILED' | 'CANCELLED'

/**
 * GET /caisen/replay/tasks 列表项（对齐 ReplayTaskSummary）。
 *
 * 物理意图：「异步回测任务」面板表格行——只含「何时跑的/什么状态/进度多少」，
 * 不含完整 report（保持列表轻量；详情用 ReplayTaskDetail 拿内嵌 ReplayReport）。
 */
export interface ReplayTask {
  task_id: string
  created_at: string                          // ISO（任务落 SQLite 时刻；排序键）
  status: ReplayTaskStatus
  progress: number                            // 0-100（已处理 symbol 占比）
  start?: string | null                       // 回测区间起（可缺省=全市场默认）
  end?: string | null
  universe_n?: number | null                  // -1 = 全市场（前端显示「全市场」）
  cfg_override?: Record<string, unknown>      // 提交时的参数增量（展示「用了什么 min_rr_ratio」）
}

/**
 * GET /caisen/replay/tasks/{id} 详情（对齐 ReplayTaskDetail）。
 *
 * 物理意图：SUCCESS 时 report 内嵌完整 ReplayReport（走势/统计/日志一次取齐，
 * 避免列表/详情两次往返）；非 SUCCESS 时 report 为 null，error 描述失败原因。
 * 时间戳四元组覆盖任务全生命周期，供前端「耗时/卡死诊断」展示。
 */
export interface ReplayTaskDetail extends ReplayTask {
  report?: ReplayReport | null                // SUCCESS 时填，复用本文件既有 ReplayReport 类型
  error?: string | null                       // FAILED 时填（Python 异常摘要）
  started_at?: string | null                  // worker 领任务时刻
  finished_at?: string | null                 // SUCCESS/FAILED/CANCELLED 终态时刻
  last_heartbeat?: string | null              // 最近心跳（调度器据此判 RUNNING 卡死）
}

/** POST /caisen/replay/async 请求体（对齐 ReplayAsyncRequest）。
 *  无 save 字段——异步任务落 SQLite 是本职，不像同步 replay 需显式 save 开关。 */
export interface ReplayAsyncRequestBody {
  start: string
  end: string
  universe?: string[] | null                  // null/缺省 = 全市场
  cfg_override?: Record<string, unknown>
}

/** POST /caisen/replay/tasks/{id}/cancel 响应（对齐 CancelResponse）。 */
export interface CancelResponse {
  task_id: string
  cancelled: boolean                          // true=已置 abort flag（终态任务返 false）
  message: string                             // 中文说明（「已取消」/「任务已结束，无法取消」）
}

// ============ API 封装（仿 trading.ts 风格，超时按端点特性覆写） ============

/**
 * POST /caisen/scan：触发当日扫描（screener → plan.generate → 落盘）。
 *
 * 物理意图：蔡森流水线起点。前端输入扫描日 + 标的池 → 后端串接算法 → 返回候选列表。
 * 扫描含 DataFrame 重计算，超时放宽到 30s（默认 60s 也够，但显式标注可观察）。
 */
export function scan(body: ScanRequestBody): Promise<CandidatePlan[]> {
  return apiClient.post('/api/v1/caisen/scan', body, { timeout: 30000 })
}

/**
 * GET /caisen/plans：读盘 + 可选 status 过滤。
 *
 * 物理意图：前端审核面板全量浏览候选计划，按状态机当前态筛选展示。
 * 无 plans 文件时返 200 + []（后端容错，前端不抛）。
 */
export function listPlans(status?: string): Promise<CandidatePlan[]> {
  return apiClient.get('/api/v1/caisen/plans', { params: status ? { status } : {}, timeout: 10000 })
}

/** GET /caisen/plans/{plan_id}：单计划查询（plan_id 不存在后端返 404）。 */
export function getPlan(planId: string): Promise<CandidatePlan> {
  return apiClient.get(`/api/v1/caisen/plans/${encodeURIComponent(planId)}`, { timeout: 10000 })
}

/**
 * PATCH /caisen/plans/{plan_id}：人工审核（approve/reject + edits 微调）。
 *
 * 物理意图：蔡森流水线审核节点——风控官基于经验判断推进/驳回，或微调止损止盈。
 * approve → APPROVED（可继续 activate）；reject → REJECTED（不进挂单流程）。
 */
export function reviewPlan(planId: string, body: PlanReviewBody): Promise<CandidatePlan> {
  return apiClient.patch(`/api/v1/caisen/plans/${encodeURIComponent(planId)}`, body, { timeout: 10000 })
}

/**
 * POST /caisen/plans/{plan_id}/activate：激活（APPROVED → ARMED）。
 *
 * 物理意图：把审核通过的计划置为 ARMED 态，同步写入 active.json 供执行器读取挂单。
 * 红线：仅 APPROVED 态可激活（后端状态机守护，非法转换抛 ValueError → 422）。
 */
export function activatePlan(planId: string): Promise<CandidatePlan> {
  return apiClient.post(`/api/v1/caisen/plans/${encodeURIComponent(planId)}/activate`, {}, { timeout: 10000 })
}

/**
 * GET /caisen/plans/{plan_id}/chart：lightweight-charts 渲染数据（candles + markers + priceLines）。
 *
 * 物理意图：Task 6 viz 层入口——price_data 可装配返完整 K 线 + 形态点 + 价位线；
 * 不可装配降级仅返 priceLines（前端画关键价位，不白屏）。
 */
export function getChart(planId: string): Promise<ChartData> {
  return apiClient.get(`/api/v1/caisen/plans/${encodeURIComponent(planId)}/chart`, { timeout: 15000 })
}

/**
 * GET /caisen/config/schema：策略参数 JSON Schema（前端反射渲染参数表单 + 规则清单）。
 *
 * 物理意图：返回 StrategyConfig.model_json_schema()，前端按 Field 的 type/description/约束
 * 动态渲染参数表单（绑定 cfg_override 随 replay/scan 提交），同时作为「规则清单」展示
 * （#2 规则列举 + #4 参数可调 同源解决，一处定义前后端不漂移）。
 */
export function getConfigSchema(): Promise<Record<string, unknown>> {
  return apiClient.get('/api/v1/caisen/config/schema', { timeout: 10000 })
}

// ============ 异步回测任务 API（Spec 1：SQLite 任务表，/lab 消费） ============

/**
 * POST /caisen/replay/async：提交异步回测（立即返 task_id，不阻塞）。
 *
 * 物理意图：Spec 1 把同步 replay（90s 阻塞）拆为「提交→轮询」——前端提交后立即拿
 * task_id，后台 ProcessPoolExecutor 串行消化 universe，前端轮询 list/get 看进度。
 * 超时 10s 仅约束「提交落 SQLite」动作本身（不等待回测完成）。
 */
export function submitReplayAsync(body: ReplayAsyncRequestBody): Promise<{ task_id: string }> {
  return apiClient.post('/api/v1/caisen/replay/async', body, { timeout: 10000 })
}

/**
 * GET /caisen/replay/tasks：异步任务列表（降序，可按 status 过滤）。
 *
 * 物理意图：任务面板数据源——前端 1-2s 轮询一次（不带 status=全量）刷新进度条；
 * 也可带 status='RUNNING' 只看在跑的。无任务时返 []（后端容错，前端不抛）。
 */
export function listReplayTasks(status?: ReplayTaskStatus): Promise<ReplayTask[]> {
  return apiClient.get('/api/v1/caisen/replay/tasks', { params: status ? { status } : {}, timeout: 10000 })
}

/**
 * GET /caisen/replay/tasks/{id}：单任务详情（status/progress/report/error/时间戳）。
 *
 * 物理意图：任务 SUCCESS 后取此详情 → report 字段内嵌完整 ReplayReport，
 * /lab 路由直接回填到 ReplayReportPanel 渲染（统计卡/资金曲线/买卖流水）。
 * taskId 经 encodeURIComponent 防特殊字符（如空格/斜杠）破坏路由。
 */
export function getReplayTask(taskId: string): Promise<ReplayTaskDetail> {
  return apiClient.get(`/api/v1/caisen/replay/tasks/${encodeURIComponent(taskId)}`, { timeout: 10000 })
}

/**
 * POST /caisen/replay/tasks/{id}/cancel：取消（置 abort flag，轮询到 CANCELLED 生效）。
 *
 * 物理意图：用户点「取消」→ 后端置 abort flag → worker 下次 universe 轮询感知后置
 * CANCELLED 终态（非立即杀进程，避免 SQLite 写半截）。空 body {} 因后端路由签名无入参。
 * 终态任务（SUCCESS/FAILED）后端返 cancelled=false + message 说明「已结束无法取消」。
 */
export function cancelReplayTask(taskId: string): Promise<CancelResponse> {
  return apiClient.post(`/api/v1/caisen/replay/tasks/${encodeURIComponent(taskId)}/cancel`, {}, { timeout: 10000 })
}

/**
 * DELETE /caisen/replay/tasks/{id}：删除任务记录（清理历史）。
 *
 * 物理意图：异步任务跑完看够了就删，防 SQLite 无限膨胀。前端仅对非 RUNNING 行调用
 * （RUNNING 删除由后端守护，但前端按钮置灰更友好）。返 {ok:true}；task_id 不存在后端返 404。
 */
export function deleteReplayTask(taskId: string): Promise<{ ok: boolean }> {
  return apiClient.delete(`/api/v1/caisen/replay/tasks/${encodeURIComponent(taskId)}`, { timeout: 10000 })
}

// Spec 2 Task 8：同步回放老 API（runReplay / listReplayRuns / getReplayRun / deleteReplayRun）
// 及其专属类型（ReplayRequestBody / ReplayRunSummary / ReplayRunDetail）已随 /caisen 老
// 「历史回放」tab 下线一并移除。回测能力由异步任务 5 函数 + /lab 路由承接。后端端点暂留，
// 供异步任务 worker 与潜在的 CLI 调试入口复用。
