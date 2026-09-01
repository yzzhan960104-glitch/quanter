/**
 * 首页净值族 + K 线回放数据 facade（可视化重构 P1/P2 · 2026-09-01）。
 *
 * 数据全部来自静态快照（ops/nav_history.py + public_snapshot ohlcv_*）——公开站
 * 物理只读；内网在线态若快照文件在（vite public/ 随包）同样可读，缺文件优雅降级。
 */
import { staticGet } from './static'

/** 单日净值（legs: main/exp → nav 绝对值，元）。 */
export interface NavDay {
  date: string
  legs: Record<string, number>
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
  axis: string[]
  series: BenchSeries[]
  updated_at?: string
}

export function getBenchmarks(): Promise<Benchmarks | null> {
  return staticGet<Benchmarks | null>('benchmarks', null)
}
