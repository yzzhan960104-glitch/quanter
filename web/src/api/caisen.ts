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
 *     POST   /caisen/replay                   → ReplayReport
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

/**
 * 回放报告（字段对齐 server.schemas.caisen.ReplayReportResponse / ReplayReport）。
 *
 * 物理意图：历史区间滚动回放的聚合统计——前端独立 tab 展示胜率/盈亏比/回撤，
 * 用于策略参数调优（如数据驱动地校准 min_rr_ratio）。
 */
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

/** POST /caisen/replay 请求体（对齐 ReplayRequest）。 */
export interface ReplayRequestBody {
  start: string
  end: string
  universe?: string[] | null                  // 默认 null = 全市场（当前 Phase 3+ 占位）
  cfg_override?: Record<string, unknown>
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
 * POST /caisen/replay：历史回放（胜率/盈亏比/回撤统计）。
 *
 * 物理意图：对 price_data 滚动跑 screener→plan→离场模拟，统计聚合指标。
 * 超时放宽到 90s（全市场回放计算密集）。
 */
export function runReplay(body: ReplayRequestBody): Promise<ReplayReport> {
  return apiClient.post('/api/v1/caisen/replay', body, { timeout: 90000 })
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
