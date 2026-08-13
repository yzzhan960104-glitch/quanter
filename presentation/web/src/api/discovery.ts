/**
 * 参数发现敏感性分析 API facade（P3 · spec §4）
 *
 * 职责：DiscoveryLab 视图（搜索实验室）的数据通道——敏感性仪表板 / 热力图 /
 * 参数空间元数据 / 搜索进展。全部只读 GET，走共享 apiClient（token 注入 +
 * 错误 Toast 拦截器）。
 *
 * 对应后端：presentation/server/api/v1/discovery.py（不挂 require_write 的只读 router）
 */
import { apiClient } from './client'

/** 边际效应单档：mean=该档 inner calmar 均值，n=样本量（n<1 时 mean 为 null） */
export interface MarginalLevel {
  mean: number | null
  n: number
}

/** 主效应排名项：spread=档间均值极差（大=主效应强），n_levels=已采样档数 */
export interface RankingItem {
  key: string
  spread: number
  n_levels: number
}

/** 敏感性响应（GET /research/discovery/sensitivity） */
export interface SensitivityResponse {
  n_trials: number
  metric: string
  marginals: Record<string, Record<string, MarginalLevel>>
  ranking: RankingItem[]
  dead_params: string[]
  blind_spots: Record<string, string[]>
}

/** 热力图响应：grid/n_obs 同行（n_obs 防单点热区误导） */
export interface HeatmapResponse {
  x_axis: string[]
  y_axis: string[]
  grid: (number | null)[][]
  n_obs: number[][]
  fill: boolean
}

/** 参数空间项（PARAM_SPACE 三件套） */
export interface ParamSpaceItem {
  key: string
  layer: string
  candidates: (string | number | null)[]
}

/** 参数空间响应（GET /research/discovery/params） */
export interface ParamsResponse {
  param_space: ParamSpaceItem[]
  constraints: string[]
}

/** 搜索进展（复用既有 /research/discovery/status 的 digest 形状） */
export interface DiscoveryStatus {
  n_trials: number
  latest_run: Record<string, unknown> | null
  champion: Record<string, unknown> | null
}

export async function getSensitivity(): Promise<SensitivityResponse> {
  const { data } = await apiClient.get<SensitivityResponse>('/api/v1/research/discovery/sensitivity')
  return data
}

export async function getHeatmap(
  x: string, y: string, metric = 'calmar',
): Promise<HeatmapResponse> {
  const { data } = await apiClient.get<HeatmapResponse>('/api/v1/research/discovery/heatmap', {
    params: { x, y, metric },
  })
  return data
}

export async function getParams(): Promise<ParamsResponse> {
  const { data } = await apiClient.get<ParamsResponse>('/api/v1/research/discovery/params')
  return data
}

export async function getDiscoveryStatus(): Promise<DiscoveryStatus> {
  const { data } = await apiClient.get<DiscoveryStatus>('/api/v1/research/discovery/status')
  return data
}
