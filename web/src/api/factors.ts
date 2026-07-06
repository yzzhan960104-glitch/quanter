/**
 * 层级二·因子注册表 API 封装
 *
 * 对应后端 server/api/v1/factors.py。复用 backtest.ts 的 apiClient。
 *
 * 反黑盒：FactorSummary.status/input_kind/grid_computable 严格镜像后端 FactorMeta，
 * 前端矩阵按 status 分类、按 grid_computable 决定是否展示 IC 衰减入口。
 */
import { apiClient } from './backtest'

export type FactorStatus = 'training' | 'live' | 'deprecated'
export type FactorInputKind = 'returns_panel' | 'ohlcv_panel' | 'lake_series' | 'cross_section' | 'set'

/** 因子摘要（GET /factors/registry 返回项） */
export interface FactorSummary {
  name: string
  label: string
  category: string
  author: string
  status: FactorStatus
  input_kind: FactorInputKind
  dataset: string
  description: string
  grid_computable: boolean
  default_params: Record<string, unknown>
}

/** 引用某因子的策略（drill-down） */
export interface StrategyRef { name: string; label: string }

/** 单因子 drill-down（GET /factors/{name}） */
export interface FactorDetail {
  summary: FactorSummary
  datasets: string[]
  referenced_by: StrategyRef[]
}

/** IC 衰减曲线节点 */
export interface ICDecayPoint {
  horizon: number
  ic_mean: number
  ic_ir: number
  t_stat: number
}

/** 月度 × horizon IC 热力图（ECharts heatmap 直消费：[month_idx, horizon_idx, ic]） */
export interface ICHeatmap {
  months: string[]
  horizons: number[]
  data: Array<[number, number, number]>
}

/** GET /factors/{name}/ic_decay 响应 */
export interface ICDecayResult {
  ok: boolean
  name: string
  label?: string | null
  reason?: string | null
  n_symbols?: number | null
  decay?: ICDecayPoint[]
  heatmap?: ICHeatmap | null
}

/** 列出全部因子摘要（因子矩阵数据源） */
export function getFactors(): Promise<FactorSummary[]> {
  return apiClient.get('/api/v1/factors/registry', { timeout: 10000 })
}

/** 单因子 drill-down（元数据 + 数据集 + 引用策略） */
export function getFactorDetail(name: string): Promise<FactorDetail> {
  return apiClient.get(`/api/v1/factors/${encodeURIComponent(name)}`, { timeout: 10000 })
}

/** IC/IR 衰减分析（仅面板型因子；CPU 密集，超时 60s） */
export function getFactorICDecay(
  name: string,
  params: { start: string; end: string; universe?: string[]; horizons?: number[] },
): Promise<ICDecayResult> {
  const qs = new URLSearchParams({ start: params.start, end: params.end })
  if (params.universe) params.universe.forEach((u) => qs.append('universe', u))
  if (params.horizons) params.horizons.forEach((h) => qs.append('horizons', String(h)))
  return apiClient.get(`/api/v1/factors/${encodeURIComponent(name)}/ic_decay?${qs.toString()}`, { timeout: 60000 })
}
