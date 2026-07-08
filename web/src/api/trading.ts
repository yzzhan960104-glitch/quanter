/**
 * 实盘交易 API 封装
 *
 * 对应后端 server/api/v1/trading.py。复用 client.ts 的 apiClient。
 *
 * 状态四态严格镜像后端：unavailable / disconnected / live / vetoed_by_risk，
 * 前端心跳灯完全跟随后端返回值，绝不本地推断（杜绝"虚假繁荣"）。
 *
 * Phase 2 新增（EMT 适配，前端不感知券商）：connect/disconnect/submitOrder/
 * cancelOrder/getOrders/getAsset。dry_run 由前端按单控制（双开关语义见后端 risk_shield）。
 */
import { apiClient } from './client'

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
  market_value: number | null    // 未查行情 → null（中性灰）
  pnl: number | null             // 累计浮盈；未查行情 → null
  strategy?: string | null         // 所属策略
  entry_rationale?: string | null  // 建仓因子逻辑
}

/** 下单请求体（dry_run 默认 true=模拟；confirm 默认 false=需二次确认） */
export interface SubmitOrderBody {
  symbol: string
  qty: number
  side: 'buy' | 'sell'
  price: number | null            // null=市价（EMT 第一版仅限价，故通常有值）
  dry_run: boolean                // 前端控制：true=模拟（不真下单）
  confirm: boolean                // 二次确认开关
}

/** 下单/撤单结果（state 取 OrderState.name 或 'DRY_RUN'） */
export interface OrderResultRow {
  order_id: string
  state: string                   // SUBMITTED / DRY_RUN / FILLED / CANCELLED / REJECTED / FAILED / ...
  message: string
}

/** 订单回报行（GET /orders 返回；EMT 字段 order_emt_id，QMT 用 seq-str） */
export interface OrderRow {
  kind?: string                   // order / trade / cancel_error / async_response
  order_emt_id?: string | number  // EMT 真实订单号
  order_id?: string | number      // 兼容 QMT seq-str
  ticker?: string
  order_status?: number           // EMT 原始状态码
  state: string                   // 映射后 OrderState.name
  qty_traded?: number             // 累计成交
  qty_left?: number               // 剩余（撤单时为撤单量）
  price?: number
  side?: number                   // EMT: 1=买 2=卖
  error_msg?: string
}

/** 资产（GET /asset；EMT buying_power=cash, QMT 同口径） */
export interface Asset {
  account_id?: string
  cash: number                    // 可用资金
  total_asset: number             // 总资产
  market_value: number            // 证券市值（预扣/持仓市值）
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

// ============ Phase 2 新增：连接 / 下单 / 撤单 / 查询 ============

/** POST /trading/connect：触发网关连接（后端 get_gateway 选 EMT/QMT）。失败→503。 */
export function connect(): Promise<{ connected: boolean; mode: string }> {
  return apiClient.post('/api/v1/trading/connect', {}, { timeout: 30000 })
}

/** POST /trading/disconnect：断开网关。 */
export function disconnect(): Promise<{ connected: boolean }> {
  return apiClient.post('/api/v1/trading/disconnect', {}, { timeout: 10000 })
}

/** POST /trading/submit_order：下单（dry_run 前端可控）。挡板命中→409。 */
export function submitOrder(body: SubmitOrderBody): Promise<OrderResultRow> {
  return apiClient.post('/api/v1/trading/submit_order', body, { timeout: 15000 })
}

/** POST /trading/cancel_order/{orderId}：撤单。 */
export function cancelOrder(orderId: string): Promise<OrderResultRow> {
  return apiClient.post(`/api/v1/trading/cancel_order/${encodeURIComponent(orderId)}`, {}, { timeout: 10000 })
}

/** GET /trading/orders：本地订单回报流水（live 态轮询）。 */
export function getOrders(): Promise<{ orders: OrderRow[] }> {
  return apiClient.get('/api/v1/trading/orders', { timeout: 10000 })
}

/** GET /trading/asset：资金资产（live 态轮询）。未连接→空字段。 */
export function getAsset(): Promise<{ asset: Asset }> {
  return apiClient.get('/api/v1/trading/asset', { timeout: 10000 })
}
