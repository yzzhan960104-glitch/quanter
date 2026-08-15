/**
 * 活跃股池 API 封装（驾驶舱专用）
 *
 * 对应后端 server/api/v1/macro.py 的单个 GET 端点（/macro/pool）。本文件
 * 只做 axios 调用与类型对齐，不做任何数据加工——加工交给 DashboardView.vue。
 *
 * 设计意图（why 复用 client.ts 的 apiClient 而非新建实例）：
 * - 拦截器（中文错误 Toast / 超时降级）对所有 API 通用，复用单一 axios 实例
 *   避免拦截器逻辑漂移；本端点是只读快照，60s 默认超时足够。
 * - 不导出 apiClient：保持「一个域一个 facade」边界，本视图不直接触碰
 *   其它域 facade，反之亦然。
 *
 * 历史：原 regime/credit/factors 三个端点已分别随 CreditRegime 下线与
 * T7 架构治理批 2 删除；getSectorFlow（/macro/sector/flow，板块资金流 +
 * 活跃股池复合端点）于 2026-08-15 CR-8 处置删除——sector 湖 2026-07-27
 * 退役后 sectors 字段结构性恒空，端点收缩为纯活跃股池 /macro/pool。
 */
import { apiClient } from './client'

// ============ 类型定义（与后端 macro.py 响应结构对齐） ============

/** GET /macro/pool 响应 */
export interface ActivePoolResponse {
  /** 活跃股池（daily 内存湖前 50 只，离线降级返 []） */
  pool: { symbol: string }[]
}

// ============ API 函数 ============

/**
 * 拉取活跃股池
 *
 * 路由：GET /api/v1/macro/pool
 */
export function getActivePool(): Promise<ActivePoolResponse> {
  return apiClient.get('/api/v1/macro/pool')
}
