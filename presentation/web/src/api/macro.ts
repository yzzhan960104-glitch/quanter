/**
 * 板块 API 封装（T17 驾驶舱专用）
 *
 * 对应后端 server/api/v1/macro.py 的单个 GET 端点（/macro/sector/flow）。本文件
 * 只做 axios 调用与类型对齐，不做任何数据加工——加工交给 DashboardView.vue
 * 内的 ECharts option。
 *
 * 设计意图（why 复用 client.ts 的 apiClient 而非新建实例）：
 * - 拦截器（中文错误 Toast / 超时降级）对所有 API 通用，复用单一 axios 实例
 *   避免拦截器逻辑漂移；板块端点是只读快照，60s 默认超时足够。
 * - 不导出 apiClient：保持「一个域一个 facade」边界，板块视图不直接触碰
 *   其它域 facade，反之亦然。
 *
 * 历史：原 regime/credit/factors 三个端点已分别随 CreditRegime 下线与
 * T7 架构治理批 2 删除，对应前端函数与类型同步移除。
 */
import { apiClient } from './client'

// ============ 类型定义（与后端 macro.py 响应结构对齐） ============

/**
 * 板块资金流记录（sector 湖 head(20) 行）
 *
 * 后端 to_dict('records') 直出，字段随 sector 湖落盘 schema 而定（典型含
 * 板块名/融资余额增速/主力净流入等）。前端按需 pick 字段，不在此强约束。
 */
export type SectorRecord = Record<string, unknown>

/** GET /macro/sector/flow 响应 */
export interface SectorFlowResponse {
  /** Top 20 板块资金流排名 */
  sectors: SectorRecord[]
  /** 活跃股池（当前后端占位返 []，下期接入活跃股池湖后填充） */
  pool: string[]
}

// ============ API 函数 ============

/**
 * 拉取板块资金流排名 + 活跃股池
 *
 * 路由：GET /api/v1/macro/sector/flow
 */
export function getSectorFlow(): Promise<SectorFlowResponse> {
  return apiClient.get('/api/v1/macro/sector/flow')
}
