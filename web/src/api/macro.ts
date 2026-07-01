/**
 * 宏观/板块/因子 API 封装（T17 驾驶舱专用）
 *
 * 对应后端 server/api/v1/macro.py 的四个 GET 端点。本文件只做 axios 调用与
 * 类型对齐，不做任何数据加工——加工交给 DashboardView.vue 内的 ECharts option。
 *
 * 设计意图（why 复用 backtest.ts 的 apiClient 而非新建实例）：
 * - 拦截器（中文错误 Toast / 超时降级）对所有 API 通用，复用单一 axios 实例
 *   避免拦截器逻辑漂移；宏观端点都是只读快照，60s 默认超时足够。
 * - 不导出 apiClient：保持「一个域一个 facade」边界，宏观视图不直接触碰
 *   backtest 实例，反之亦然。
 */
import { apiClient } from './backtest'

// ============ 类型定义（与后端 macro.py 响应结构对齐） ============

/**
 * 信贷状态值
 *
 * +1 = 扩张（risk-on，宽松信用环境，倾向加仓高 beta）
 *  0 = 中性（震荡市，仓位中性）
 * -1 = 收缩（risk-off，紧信用，防御为主）
 *
 * 后端 factors/macro_regime.CreditRegime.compute 返回 int，这里联合字面量
 * 便于前端 switch 穷尽分支（缺省走中性灰）。
 */
export type RegimeValue = 1 | 0 | -1

/** 单日信贷状态记录（regime 端点 history 项） */
export interface RegimeHistoryPoint {
  date: string        // YYYY-MM-DD
  regime: RegimeValue
}

/** GET /macro/regime 响应 */
export interface MacroRegimeResponse {
  /** 当日信贷状态（无湖/降级时后端可能返 0 中性兜底） */
  regime: RegimeValue
  /** 近 60 日逐日状态序列（前端绘红黄绿迁移色带） */
  history: RegimeHistoryPoint[]
}

/** 单指标时序节点（credit 端点列项） */
export interface SeriesPoint {
  date: string
  value: number
}

/**
 * GET /macro/credit 响应
 *
 * series 的 key 为 macro 湖列名（社融/M1M2_gap/dr007 等），value 为该列的
 * 时序数组。离线降级时后端返 {series: {}}（空对象），前端需做空态兜底。
 */
export interface MacroCreditResponse {
  series: Record<string, SeriesPoint[]>
}

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

/** GET /macro/factors/{symbol} 响应（atr 可为 null：窗口不足/无湖） */
export interface MacroFactorsResponse {
  atr: number | null
}

// ============ API 函数 ============

/**
 * 拉取当前宏观信贷状态 + 近 60 日历史
 *
 * 路由：GET /api/v1/macro/regime
 */
export function getMacroRegime(): Promise<MacroRegimeResponse> {
  return apiClient.get('/api/v1/macro/regime')
}

/**
 * 拉取信贷三因子时序（社融/M1M2_gap/DR007）
 *
 * 路由：GET /api/v1/macro/credit
 */
export function getMacroCredit(): Promise<MacroCreditResponse> {
  return apiClient.get('/api/v1/macro/credit')
}

/**
 * 拉取板块资金流排名 + 活跃股池
 *
 * 路由：GET /api/v1/macro/sector/flow
 */
export function getSectorFlow(): Promise<SectorFlowResponse> {
  return apiClient.get('/api/v1/macro/sector/flow')
}

/**
 * 拉取单标的近 30 日 ATR 波动率（微观定权用）
 *
 * 路由：GET /api/v1/macro/factors/{symbol}
 *
 * 本期 DashboardView 不强依赖此端点（驾驶舱聚焦全市场宏观/板块），但封装
 * 留口供后续微观面板接入，避免临时 ad-hoc 调用绕过类型边界。
 */
export function getMacroFactors(symbol: string): Promise<MacroFactorsResponse> {
  return apiClient.get(`/api/v1/macro/factors/${encodeURIComponent(symbol)}`)
}
