/**
 * 实盘交易 API 封装
 *
 * 对应后端 server/api/v1/trading.py 三端点。复用 backtest.ts 的 apiClient。
 *
 * 状态四态严格镜像后端：unavailable / disconnected / live / vetoed_by_risk，
 * 前端心跳灯完全跟随后端返回值，绝不本地推断（杜绝"虚假繁荣"）。
 */
import { apiClient } from './backtest'

/** 网关模式（与后端 get_status().mode 对齐） */
export type GatewayMode = 'unavailable' | 'disconnected' | 'live' | 'vetoed_by_risk'

/** GET /trading/status 响应（前端 Cockpit 每 2s 轮询） */
export interface TradingStatus {
  connected: boolean
  locked: boolean
  mode: GatewayMode
}

/** 单只持仓行（Treemap 叶子） */
export interface PositionRow {
  symbol: string
  qty: number
  market_value: number | null    // 第一版未查行情 → null（中性灰）
  pnl: number | null             // 累计浮盈；未查行情 → null
  // 层级五·持仓归因（trading_service 归因注册表 join；未登记 → null，前端显示 '—'）
  strategy?: string | null         // 所属策略
  entry_rationale?: string | null  // 建仓因子逻辑
}

/** GET /trading/status：心跳四态 */
export function getStatus(): Promise<TradingStatus> {
  return apiClient.get('/api/v1/trading/status', { timeout: 5000 })
}

/** GET /trading/positions：持仓聚合（Treemap 数据源） */
export function getPositions(): Promise<{ positions: PositionRow[] }> {
  return apiClient.get('/api/v1/trading/positions', { timeout: 10000 })
}

/** POST /trading/emergency_halt：一键熔断（幂等；按钮二次确认后调用） */
export function emergencyHalt(): Promise<{ halted: boolean; message: string }> {
  return apiClient.post('/api/v1/trading/emergency_halt', {}, { timeout: 15000 })
}

/**
 * GET /trading/export：导出实盘成交 CSV（按日期），触发浏览器下载。
 *
 * responseType:'blob' 拿原始 CSV 字节流；手动 createObjectURL + a.download 触发下载。
 * Layer 6 LLM 复盘直接消费此 CSV。
 */
export async function exportLiveTrades(start: string, end: string): Promise<void> {
  const blob = await apiClient.get('/api/v1/trading/export', {
    params: { start, end },
    timeout: 30000,
    responseType: 'blob',
  }) as unknown as Blob
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `live_trades_${start}_${end}.csv`
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(url)
}
