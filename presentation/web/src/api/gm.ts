/**
 * 掘金终端（7002 网关）只读观测 API（W4-B，2026-08-28 全库评审清偿）。
 *
 * 数据链路：前端 → server /api/v1/gm/*（cookie 只读鉴权）→ 7002 Bearer 透传。
 * 终端 token 只活在 server 进程，绝不入前端 bundle（client.ts 的 VITE_API_TOKEN
 * 内网假设不扩散到终端凭证）。端点语义与 .agents/skills/goldminer-terminal
 * 实测面对齐；全部只读——交易动作族是 skill 纪律禁区，永不进本文件。
 */
import { apiClient } from './client'
import { STATIC_MODE, staticGet, staticToday } from './static'

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

/** 持仓行（/v3/account-trade/positions；industry=快照管道富化列 P5.2）。 */
export interface GmPositionRow {
  symbol?: string         // gm 格式 SZSE.300433
  volume?: number
  vwap?: number
  fpnl?: number           // 浮动盈亏
  available?: number
  market_value?: number   // 持仓市值（环形图聚合源）
  industry?: string | null  // 行业（stock_basic.parquet 富化；缺映射 null）
  [k: string]: unknown
}

/** 委托行（/v3/account-trade/orders，含状态/拒因）。
 *  键名=7002 实测 snake_case（code-review J-4：此前声明 camelCase 系接口撒谎，
 *  靠索引签名掩盖）。状态值语义对齐部署产物 _GM_STATUS_TO_LOCAL：
 *  3=成交 8=拒 5/12=撤/过期 6=待撤（非终态）2=部成 其余=在途/待报。 */
export interface GmOrderRow {
  cl_ord_id?: string
  symbol?: string
  side?: number           // 1=买 2=卖
  status?: number
  price?: number
  volume?: number
  filled_volume?: number
  ordRejReason?: string | null
  created_at?: string     // UTC（Z 后缀）——展示前必须转本地时区
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
  if (STATIC_MODE) return staticGet<GmOverview>('gm_overview')
  return apiClient.get('/api/v1/gm/overview', { timeout: 5000 })
}

/** 腿条目（/gm/legs）：LegSelector/DualAssetCard 的数据源（LEGS 注册表单源）。 */
export interface GmLeg {
  key: string              // "main" | "exp"
  label: string            // 主腿 / 实验腿
  role: string             // incumbent | challenger
  account_id: string | null
  strategy_id: string | null
  strategy_name: string | null
}

export async function getLegs(): Promise<GmLeg[]> {
  if (STATIC_MODE) return staticGet<GmLeg[]>('gm_legs', [])
  const payload = await apiClient.get('/api/v1/gm/legs', { timeout: 5000 })
  return (payload as { legs?: GmLeg[] }).legs ?? []
}

/**
 * 按腿取资产（缺省 main——未迁移组件零改动）。腿账户查询失败（未部署/网关断）
 * 由 server 返 502，调用方 catch 后展示「—」降级（虚假繁荣防线）。
 */
export async function getAssetByLeg(leg: string = 'main'): Promise<GmAsset> {
  if (STATIC_MODE) return staticGet<GmAsset>(`gm_asset_${leg}`)
  const payload: unknown = await apiClient.get('/api/v1/gm/asset', { params: { leg }, timeout: 5000 })
  if (payload && typeof payload === 'object' && (payload as { data?: unknown }).data) {
    return (payload as { data: GmAsset }).data
  }
  return (payload ?? {}) as GmAsset
}

export async function getPositions(leg: string = 'main'): Promise<GmPositionRow[]> {
  if (STATIC_MODE) return staticGet<GmPositionRow[]>(`gm_positions_${leg}`, [])
  return unwrap<GmPositionRow>(
    await apiClient.get('/api/v1/gm/positions', { params: { leg }, timeout: 10000 }))
}

export async function getOrders(leg: string = 'main'): Promise<GmOrderRow[]> {
  if (STATIC_MODE) return staticGet<GmOrderRow[]>(`gm_orders_${leg}`, [])
  return unwrap<GmOrderRow>(
    await apiClient.get('/api/v1/gm/orders', { params: { leg }, timeout: 10000 }))
}

export async function getTrades(params: { leg?: string; limit?: number } = {}): Promise<GmTradeRow[]> {
  // 静态态读跨日成交史（gm_trades_history：state 订单史+今日柜台校准）——
  // 柜台 execrpts 按自然日滚动只给当日，会"流水比持仓还少"（09-02 用户反馈）
  if (STATIC_MODE) return staticGet<GmTradeRow[]>(`gm_trades_history_${params.leg ?? 'main'}`, [])
  return unwrap<GmTradeRow>(
    await apiClient.get('/api/v1/gm/trades', { params, timeout: 10000 }))
}

// ─────────────────────── 双腿对照（/gm/ab，2026-08-29 多腿方案）───────────────────────

/** 工程闸单腿统计（compare 负载的腿字段；counts 键为 audit 事件名）。 */
export interface AbGate {
  init: { account?: string; build_stamp?: string } | null
  counts: Record<string, number>
  warn_types: Record<string, number>
}

export interface AbDiff {
  added: string[]
  removed: string[]
  drifted: Array<{ symbol: string; incumbent: number; challenger: number }>
}

export interface AbOrderDiff extends AbDiff {
  qty_diff: Array<{ symbol: string; incumbent: number; challenger: number }>
}

/** /gm/ab 负载：ok=false 时 flags 非空（红旗）。single_leg=true 表示实验腿未部署。 */
export interface AbDaily {
  day: string
  ok?: boolean
  single_leg?: boolean
  detail?: string
  main?: AbGate
  exp?: AbGate
  signals?: AbDiff
  orders?: AbOrderDiff
  flags?: string[]
}

export async function getAbDaily(date?: string): Promise<AbDaily> {
  if (STATIC_MODE) return staticGet<AbDaily>(`gm_ab_${date ?? staticToday()}`)
  return apiClient.get('/api/v1/gm/ab', { params: date ? { date } : {}, timeout: 8000 })
}

/** /gm/audit 事件流下钻行（detail 已是解析后的对象；失败行包 {raw}）。 */
export interface AuditRow {
  ts: string
  event: string
  detail: Record<string, unknown> & { raw?: string }
}

export async function getAudit(params: {
  leg?: string; date?: string; event?: string; limit?: number
}): Promise<AuditRow[]> {
  if (STATIC_MODE) {
    // 静态快照仅含近 N 日（public_snapshot 生成窗口）；缺日 → 空数组降级。
    // event 过滤在快照数据不变的前提下无法服务端做，前端原样返回（生成端已
    // 全量落盘，行数=当日 audit 规模，可控）。
    return staticGet<AuditRow[]>(
      `gm_audit_${params.leg ?? 'main'}_${params.date ?? staticToday()}`, [])
  }
  const payload = await apiClient.get('/api/v1/gm/audit', { params, timeout: 8000 })
  return (payload as { rows?: AuditRow[] }).rows ?? []
}

/** 轮次档案（/gm/round 透传 ab_round.json；404 时返回 null 由调用方展示占位）。 */
export interface GmRound {
  round_id: string
  type: string              // calibration | candidate
  candidate: string
  start: string
  expect_diff?: string
  promote_gate?: string
  notes?: string
}

export async function getRound(): Promise<GmRound | null> {
  if (STATIC_MODE) return staticGet<GmRound | null>('gm_round', null)
  try {
    return await apiClient.get('/api/v1/gm/round', { timeout: 5000 })
  } catch {
    return null
  }
}
