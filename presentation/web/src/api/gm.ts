/**
 * 掘金终端（7002 网关）只读观测 API（W4-B，2026-08-28 全库评审清偿）。
 *
 * 数据链路：前端 → server /api/v1/gm/*（cookie 只读鉴权）→ 7002 Bearer 透传。
 * 终端 token 只活在 server 进程，绝不入前端 bundle（client.ts 的 VITE_API_TOKEN
 * 内网假设不扩散到终端凭证）。端点语义与 .agents/skills/goldminer-terminal
 * 实测面对齐；全部只读——交易动作族是 skill 纪律禁区，永不进本文件。
 */
import { apiClient } from './client'

/** 策略条目（/v3/strategies[].data）：stage=运行态。 */
export interface GmStrategy {
  id: string
  name?: string
  stage?: string          // 实测枚举以终端为准（running/stopped/...）
  [k: string]: unknown
}

export interface GmOverview {
  strategies: GmStrategy[]
  account_statuses: unknown[] | null   // null=账户状态通道不可用（降级不阻断）
}

/** 资金（/v3/account-trade/cash）：nav=总权益，available=可用，frozen=冻结。 */
export interface GmAsset {
  nav?: number
  available?: number
  frozen?: number
  market_value?: number
  [k: string]: unknown
}

/** 持仓行（/v3/account-trade/positions）。 */
export interface GmPositionRow {
  symbol?: string         // gm 格式 SZSE.300433
  volume?: number
  vwap?: number
  fpnl?: number           // 浮动盈亏
  available?: number
  [k: string]: unknown
}

/** 委托行（/v3/account-trade/orders，含状态/拒因）。 */
export interface GmOrderRow {
  clOrdId?: string
  symbol?: string
  side?: number           // 1=买 2=卖
  status?: number         // int 状态词汇见终端 skill
  price?: number
  volume?: number
  filledVolume?: number
  ordRejReason?: string | null
  [k: string]: unknown
}

/** 成交流水行（/v3/account-trade/execrpts——终端拼写就是 execrpts）。 */
export interface GmTradeRow {
  execId?: string
  symbol?: string
  price?: number
  volume?: number
  [k: string]: unknown
}

/** 7002 响应壳：{ code, data } 形态（与三件套 gc.api_get 消费口径一致）。 */
function unwrap<T>(payload: unknown): T[] {
  if (payload && typeof payload === 'object' && Array.isArray((payload as { data?: unknown }).data)) {
    return (payload as { data: T[] }).data
  }
  return []
}

export async function getOverview(): Promise<GmOverview> {
  return apiClient.get('/api/v1/gm/overview', { timeout: 5000 })
}

export async function getAsset(): Promise<GmAsset> {
  const payload = await apiClient.get('/api/v1/gm/asset', { timeout: 5000 })
  // cash 端点返单对象（或 {data:{}} 壳）——兼容两种形态取首
  if (payload && typeof payload === 'object' && (payload as { data?: unknown }).data) {
    return (payload as { data: GmAsset }).data
  }
  return (payload ?? {}) as GmAsset
}

export async function getPositions(): Promise<GmPositionRow[]> {
  return unwrap<GmPositionRow>(await apiClient.get('/api/v1/gm/positions', { timeout: 10000 }))
}

export async function getOrders(): Promise<GmOrderRow[]> {
  return unwrap<GmOrderRow>(await apiClient.get('/api/v1/gm/orders', { timeout: 10000 }))
}

export async function getTrades(params: { limit?: number } = {}): Promise<GmTradeRow[]> {
  return unwrap<GmTradeRow>(
    await apiClient.get('/api/v1/gm/trades', { params, timeout: 10000 }))
}
