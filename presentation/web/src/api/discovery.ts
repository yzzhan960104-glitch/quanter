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

/* ============ 请求函数（直返姿势 · CR-1 修复）============
 *
 * Why 直返而不解构：client.ts 响应拦截器 `(response) => response.data` 已剥掉
 * axios 包壳，apiClient.get 运行时直接 resolve 业务 payload 本身。旧写法在 await
 * 之后对结果解构出 data 再返回——这是对已剥壳 payload 的二次解构，data 恒为
 * undefined，视图层静默渲染空态，形成 HTTP 200 的「死页」（CR-1；ops/check_contracts.py
 * 的 check_no_double_unwrap 守卫已静态拦截此姿势回潮）。
 *
 * Why 不写 `<T>` 泛型也能过 vue-tsc：axios 的 get 签名 get<T, R = AxiosResponse<T>>
 * 中 R 支持从函数显式返回类型（Promise<XXXResponse>）上下文反推——故直返 +
 * 显式返回类型即得正确类型（与 trading.ts getStatus/getPositions 同款姿势）。
 */

/** GET /research/discovery/sensitivity：敏感性仪表板（边际效应 + 主效应排名） */
export function getSensitivity(): Promise<SensitivityResponse> {
  return apiClient.get('/api/v1/research/discovery/sensitivity')
}

/** GET /research/discovery/heatmap：两维热力图（metric 缺省 calmar） */
export function getHeatmap(x: string, y: string, metric = 'calmar'): Promise<HeatmapResponse> {
  return apiClient.get('/api/v1/research/discovery/heatmap', { params: { x, y, metric } })
}

/** GET /research/discovery/params：参数空间三件套（PARAM_SPACE + 约束） */
export function getParams(): Promise<ParamsResponse> {
  return apiClient.get('/api/v1/research/discovery/params')
}

/** GET /research/discovery/status：搜索进展 digest（试验数 / 最新 run / 冠军） */
export function getDiscoveryStatus(): Promise<DiscoveryStatus> {
  return apiClient.get('/api/v1/research/discovery/status')
}
