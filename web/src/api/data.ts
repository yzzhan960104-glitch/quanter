/**
 * 层级一·数据湖 API 封装
 *
 * 对应后端 server/api/v1/data.py。复用 backtest.ts 的 apiClient（共享响应拦截器：
 * 中文错误 Toast / 超时降级，避免每个 facade 各自 create 导致拦截器逻辑漂移）。
 *
 * 反黑盒契约：DatasetAsset.status 五态严格镜像后端 data_service._derive_status，
 * 前端表格徽章按此着色，绝不本地推断（杜绝「虚假健康」）。
 */
import { apiClient } from './backtest'

/** 数据集状态五态（与后端 DatasetStatus Literal 同源） */
export type DatasetStatus = 'syncing' | 'healthy' | 'stale' | 'missing' | 'failed'

/** 单条数据集资产（GET /data/datasets 返回项） */
export interface DatasetAsset {
  key: string
  name: string
  source: string
  market: string
  granularity: string
  schedule: string
  status: DatasetStatus
  data_start: string | null
  data_end: string | null
  latest_sync: string | null
  last_error: string | null
}

/** POST /data/sync/{key} 响应 */
export interface SyncResponse {
  key: string
  status: DatasetStatus
  message: string
}

/** 列出全部数据集资产（前端 DataLakeView 表格数据源） */
export function getDatasets(): Promise<DatasetAsset[]> {
  return apiClient.get('/api/v1/data/datasets', { timeout: 10000 })
}

/** 触发某数据集同步（fire-and-forget：后端起 daemon 子进程，立即返回 syncing） */
export function triggerSync(key: string): Promise<SyncResponse> {
  return apiClient.post(`/api/v1/data/sync/${encodeURIComponent(key)}`, {}, { timeout: 10000 })
}
