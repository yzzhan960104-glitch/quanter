/**
 * 因子探索沙盒 API 封装
 *
 * 对应后端 server/api/v1/explorer.py（POST /explorer/grid + GET /explorer/result/{task_id}）。
 * 复用 backtest.ts 的 apiClient（共享响应拦截器：中文错误 Toast / 超时降级）。
 *
 * 提交流程：submitGrid → 拿 task_id → 轮询 getResult 直到 ready=true → 消费 result。
 * degraded=true（Redis 宕机降级线程池）时 submitGrid 直接返 result，无需轮询。
 */
import { apiClient } from './backtest'

/** 因子网格计算规格（与后端 FactorGridSpec 对齐） */
export interface FactorGridSpec {
  factor: string
  universe: string[]
  start: string
  end: string
}

/** 分层累计净值：Q1-Q5 + LS（多空 Alpha） */
export interface QuantileNav {
  Q1: number[]
  Q2: number[]
  Q3: number[]
  Q4: number[]
  Q5: number[]
  LS: number[]
}

/** IC 直方图 */
export interface IcHistogram {
  bin_edges: number[]
  counts: number[]
}

/** 因子网格产物（ready=true 时 result 字段；ok=false 时带 reason） */
export interface FactorGridResult {
  ok: boolean
  factor?: string
  dates: string[]
  ic_series: number[]
  ic_mean: number
  ic_ir: number
  t_stat: number
  quantile_nav: QuantileNav
  ic_hist: IcHistogram
  /** 离线/universe 空时 ok=false，带 reason */
  reason?: string
}

/** GET /explorer/result/{task_id} 响应 */
export interface FactorGridPoll {
  status: string         // PENDING/STARTED/SUCCESS/FAILURE
  ready: boolean
  result: FactorGridResult | null
}

/**
 * POST /explorer/grid 提交因子网格
 *
 * 返回联合类型：
 * - 正常：{ task_id, degraded: false }（Celery 异步派发，前端轮询 /result）
 * - 降级：{ result, degraded: true }（Redis 宕机，线程池同步执行完直接返结果）
 */
export function submitGrid(spec: FactorGridSpec): Promise<{ task_id: string; degraded: boolean } | { result: FactorGridResult; degraded: true }> {
  return apiClient.post('/api/v1/explorer/grid', spec, { timeout: 30000 })
}

/** GET /explorer/result/{task_id} 轮询因子网格结果 */
export function getResult(task_id: string): Promise<FactorGridPoll> {
  return apiClient.get(`/api/v1/explorer/result/${task_id}`, { timeout: 10000 })
}
