/**
 * 首页净值族 + K 线回放数据 facade（可视化重构 P1/P2 · 2026-09-01）。
 *
 * 数据全部来自静态快照（ops/nav_history.py + public_snapshot ohlcv_*）——公开站
 * 物理只读；内网在线态若快照文件在（vite public/ 随包）同样可读，缺文件优雅降级。
 */
import { staticGet } from './static'

/** 单腿资产构成（P5.2：EOD 日志+实时点双源；缺段=null 前端不画）。 */
export interface LegAssets {
  available?: number | null
  market_value?: number | null
}

/** 单日净值（legs: main/exp → nav 绝对值，元）。 */
export interface NavDay {
  date: string
  legs: Record<string, number>
  assets?: Record<string, LegAssets>
}

export interface NavHistory {
  base: number
  era_start: string
  note?: string
  pre_era_note?: string
  updated_at?: string
  days: NavDay[]
}

export async function getNavHistory(): Promise<NavHistory> {
  return staticGet<NavHistory>('nav_history', { base: 200000, era_start: '', days: [] })
}

/** K 线回放快照：rows=[o,h,l,c,v]；marks=颈线/entry/止损/止盈定身位（缺null不画）。 */
export interface OhlcvMarks {
  entry_date?: string | null
  entry_price?: number | null
  stop?: number | null
  tp1_price?: number | null
  tp2_price?: number | null
  formed_at?: string | null
  neckline?: number | null
  signal_entry?: number | null
  rr?: number | null
  max_holding?: number | null
  expire_date?: string | null
  /** trailing 止损轨迹逐日回放 [[date, stop], ...]（P5.3；末点=当前止损位）。 */
  trailing_path?: Array<[string, number]>
}

export interface OhlcvData {
  symbol: string
  name: string
  dates: string[]
  rows: number[][]
  marks: OhlcvMarks
  asof?: string
}

/** 按标的取 K 线快照（无快照 → null：调用方展示占位，绝不猜数据）。 */
export function getOhlcv(sym: string): Promise<OhlcvData | null> {
  return staticGet<OhlcvData | null>(`ohlcv_${sym}`, null)
}

/** 基准指数（首页收益率同图对比 · 2026-09-01 需求④）：A 股交易日轴 + 三基准
 * 前向填充收盘（美股休市日沿用前收——同期累计收益对比的标准口径）。 */
export interface BenchSeries {
  code: string
  name: string
  points: Array<number | null>
}

export interface Benchmarks {
  era_start: string
  ytd_anchor?: string            // 当年首个 A 股交易日（YTD 归一锚）
  axis: string[]
  series: BenchSeries[]
  updated_at?: string
}

export function getBenchmarks(): Promise<Benchmarks | null> {
  return staticGet<Benchmarks | null>('benchmarks', null)
}

/** 腿策略全量档案（需求②③）：部署产物 §0 常量 + 人工风控文件化身 + 身份。 */
export interface LegRisk {
  risk_block: boolean
  cap_total: number
  note?: string
}

export interface LegDetail {
  leg: { key?: string; label?: string; role?: string; strategy_id?: string | null
         strategy_name?: string | null; account_id?: string | null }
  build_stamp?: string
  id_params?: Record<string, unknown>
  exec_params?: Record<string, unknown>
  trade_cfg?: Record<string, unknown>
  amihud_filter?: Record<string, unknown>
  universe_size?: number
  daily_order_cap?: number
  risk?: LegRisk
  artifact_error?: string
}

export function getLegDetail(leg: string): Promise<LegDetail | null> {
  return staticGet<LegDetail | null>(`leg_detail_${leg}`, null)
}

/** TSB 机会观察（09-02 起四组；P6.4 扩池铜/原油/白银）。 */
export interface TsbSeries {
  dates: string[]
  points: number[]
}

export type TsbKey = 'zijin' | 'gold' | 'dxy' | 'us10y' | 'rubber' | 'rufu'
  | 'jx_copper' | 'cu' | 'petro' | 'crude' | 'shengda' | 'ag'

export interface TsbDoc {
  series: Record<TsbKey, TsbSeries>
  meta?: Record<string, string>
  updated_at?: string
}

export function getTsb(): Promise<TsbDoc | null> {
  return staticGet<TsbDoc | null>('tsb', null)
}

/** 每日计划卡（09-02）：今日实况 + 昨晚预演回看（执行日收盘后公开）。 */
export interface PlanRow {
  sym: string
  name: string
  qty: number | null
  price: number | null
  neckline?: number | null
  rr?: number | null
  formed?: string
  status: 'filled' | 'placed' | 'signal'
}

export interface NextPlanRow {
  sym: string
  name?: string
  qty: number
  entry: number
  clamped?: boolean
  neckline?: number | null
  rr?: number | null
  formed?: string
}

export interface PlanCardDoc {
  today: string
  leg?: string
  leg_label?: string
  rows: PlanRow[]
  /** 漏斗逐级(2026-09-08):total=识别总数(新信号+持有跳过互斥);amihud/blocked/
   *  throttle 作用于其后各级为子集;placed=实挂;filled=成交。旧卡缺 total/amihud/placed/throttle。 */
  summary: { signals: number; filled: number; skip_held: number; blocked: number
             total?: number; amihud?: number; placed?: number; throttle?: number }
  preview: { stamp?: string; equity?: number
    recon: { ok: number; total: number; only_preview: string[]
             only_actual: string[]; drift: string[] } } | null
  next_preview: { plan_date: string; stamp?: string; equity?: number
    rows: NextPlanRow[]
    dropped_amihud: string[]
    skip_held: Array<{ sym: string; why?: string }>
    blocked: Array<{ sym: string; why?: string }>
    /** 每日回顾页附件（2026-09-07）：委员会评审 + 均衡栈守卫预判（旧档案缺省） */
    committee_review?: import('./review').CommitteeReview | null
    guards?: PlanGuards | null } | null
  note?: string
}

/** 均衡栈守卫预判（ops/emquant_plan_preview.balanced_guards 同形）。 */
export interface PlanGuards {
  index?: string
  ma_window?: number
  chase_skip_atr?: number
  t1?: { close?: number; ma?: number; ratio?: number; blocked?: boolean
         as_of?: string; balanced_action?: string } | null
  t1_error?: string
  rows?: Array<{ sym?: string; premium_atr_est?: number | null; chase_skip_note?: string }>
}

export function getPlanCard(leg?: string): Promise<PlanCardDoc | null> {
  // 分腿计划卡(2026-09-08):leg 指定读 plan_card_{leg};缺省=主腿旧键(HomeView 兼容)
  return staticGet<PlanCardDoc | null>(leg ? `plan_card_${leg}` : 'plan_card', null)
}
