/**
 * 层级三·策略拓扑 API 封装
 *
 * 对应后端 server/api/v1/strategies.py（扩展）。复用 backtest.ts 的 apiClient。
 *
 * 与 backtest.ts 的 getStrategies 区分：
 * - backtest.getStrategies 返回 StrategyMeta（仅 name/label/universe，回测下拉框用）。
 * - 本模块 getStrategies 返回 StrategyTopology（含 composition/rhythm/capital_allocation，
 *   策略架构师视图消费拓扑白盒信息）。
 */
import { apiClient } from './backtest'

/** 策略拓扑（GET /strategies 扩展返回项） */
export interface StrategyTopology {
  name: string
  label: string
  universe: string[]
  composition: { factors?: string[]; datasets?: string[]; [k: string]: unknown }
  rhythm: string
  capital_allocation: string
}

/** 执行计划 DAG 节点 */
export interface ExecutionPlanNode {
  id: string
  label: string
  stage: 'data' | 'factor' | 'signal' | 'order' | string
  detail: string
  depends_on: string[]
}

/** 执行计划 DAG（GET /strategies/{name}/plan） */
export interface ExecutionPlan {
  strategy: string
  label: string
  rhythm: string
  nodes: ExecutionPlanNode[]
}

/** 列出全部策略拓扑（含 composition/rhythm/capital_allocation） */
export function getStrategies(): Promise<StrategyTopology[]> {
  return apiClient.get('/api/v1/strategies', { timeout: 10000 })
}

/** 获取策略执行计划 DAG */
export function getExecutionPlan(name: string): Promise<ExecutionPlan> {
  return apiClient.get(`/api/v1/strategies/${encodeURIComponent(name)}/plan`, { timeout: 10000 })
}
