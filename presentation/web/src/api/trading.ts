/**
 * 实盘交易 API 封装（Phase 1 · 前端只读化）
 *
 * 对应后端 server/api/v1/trading.py。复用 client.ts 的 apiClient。
 *
 * 状态四态严格镜像后端：unavailable / disconnected / live / vetoed_by_risk，
 * 前端心跳灯完全跟随后端返回值，绝不本地推断（杜绝"虚假繁荣"）。
 *
 * 【只读边界】前端不再保留任何写函数——connect/disconnect/submitOrder/
 * cancelOrder/emergencyHalt 五个写入口已收回。后端写接口保留，仅供
 * scripts/CLI/QMT 客户端调用；前端只做观测：心跳灯/持仓 Treemap/资产卡/
 * 委托回报列表/CSV 导出/流水查询。写需求路径：pre_open cron（09:22 自动挂单）
 * 或 trading/tools/trigger_pre_open_once.py 手动补挂，连接/熔断赴 QMT 客户端。
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

/** 订单回报行（GET /orders 返回，QMT 用 seq-str order_id；只读展示） */
export interface OrderRow {
  kind?: string                   // order / trade / cancel_error / async_response
  order_id?: string | number      // QMT seq-str 订单号
  ticker?: string
  state: string                   // 映射后 OrderState.name
  qty_traded?: number             // 累计成交
  qty_left?: number               // 剩余（撤单时为撤单量）
  price?: number
  side?: number                   // 方向码 1=买 2=卖（预留，QMT 回报暂不返回）
  error_msg?: string
}

/** 资产（GET /asset；cash=可用资金口径） */
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

/** GET /trading/orders：本地订单回报流水（live 态轮询；前端只读展示）。 */
export function getOrders(): Promise<{ orders: OrderRow[] }> {
  return apiClient.get('/api/v1/trading/orders', { timeout: 10000 })
}

/** GET /trading/asset：资金资产（live 态轮询）。未连接→空字段。 */
export function getAsset(): Promise<{ asset: Asset }> {
  return apiClient.get('/api/v1/trading/asset', { timeout: 10000 })
}

// ============ 一期观测运营层：流水查询 ============

/** 单笔实盘流水行（对齐后端 LIVE_TRADE_COLUMNS + query_trades 返回）。 */
export interface TradeRecord {
  timestamp: string
  symbol: string
  direction: string             // buy / sell / 其他状态字
  shares: number | string
  price: number | string
  strategy?: string
  rationale?: string
}

/** GET /trades 响应（分页）。 */
export interface TradesPage {
  trades: TradeRecord[]
  total: number
  limit: number
  offset: number
}

/** GET /trading/trades：分页查询实盘流水（按日期/标的/方向过滤）。 */
export function queryTrades(params: {
  start: string
  end: string
  symbol?: string
  direction?: string
  limit?: number
  offset?: number
}): Promise<TradesPage> {
  return apiClient.get('/api/v1/trading/trades', { params, timeout: 15000 })
}

// ============ Phase 2 · 作业驾驶舱（GET /trading/jobs，只读） ============

/** 单个 job 台账行（对齐后端 job_ledger.snapshot_for_date 返回项）。 */
export interface JobRow {
  name: string                            // pipeline / pre_open / ...
  status: 'running' | 'done' | 'skipped' | 'failed'
  started_at: string
  finished_at: string | null              // running 时为 null
  message: string                         // gate 拒因（pre_open skipped 时最有价值）
}

/** 启动补跑四态（对齐后端 _resolve_catchup_state）。 */
export type CatchupState = 'running' | 'done' | 'failed' | 'not_started'

/** GET /trading/jobs 响应（作业驾驶舱数据源）。 */
export interface JobsSnapshot {
  date: string
  jobs: JobRow[]
  catchup: { state: CatchupState; result: Record<string, unknown> | null }
  warning?: string                        // 台账读失败时填，前端可折叠提示
}

/** GET /trading/jobs?date=：当天 job 台账 + 启动补跑状态（作业驾驶舱数据源，只读）。 */
export function getJobs(date: string): Promise<JobsSnapshot> {
  return apiClient.get('/api/v1/trading/jobs', { params: { date }, timeout: 10000 })
}
