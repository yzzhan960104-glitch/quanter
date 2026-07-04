/**
 * Axios 请求封装
 *
 * 职责：
 * 1. 统一配置 baseURL、超时时间、请求/响应拦截器
 * 2. 封装单资产回测与组合回测两个 API 函数
 * 3. 响应拦截器统一处理 HTTP 错误，ElMessage 弹出后端中文错误信息
 *
 * 设计原则：
 * - 不引入复杂的拦截器链，仅做错误提取和 Toast 提示
 * - 超时设置区分单资产（60s）和组合（120s，HMM 训练更耗时）
 * - baseURL 读取环境变量，缺省走 Vite 代理（/api → localhost:8000）
 */
import axios, { type AxiosInstance } from 'axios'
import { ElMessage } from 'element-plus'

// ============ 类型定义（与后端 Pydantic 模型对齐） ============

/** 成本模型参数 */
export interface CostModelParams {
  commission_rate?: number
  stamp_duty?: number
  min_commission?: number
  slippage_model?: 'linear' | 'log'
  slippage_rate?: number
  liquidity_threshold?: number
}

/** 单资产回测请求 */
export interface SingleBacktestParams {
  symbol: string
  start_date: string          // YYYY-MM-DD
  end_date: string
  initial_capital: number
  signal_freq: '1d' | '1h' | '5m' | '1m'
  cost_model?: CostModelParams
  // 策略字段（Task 9：前端驱动调参；tech_weights 已下沉到 strategy_params.tech_weight）
  strategy_name?: string
  strategy_params?: Record<string, unknown>
  /**
   * 回测 K 线频率（Task 18：分钟级回测支持）。
   *
   * Why 与 signal_freq 解耦（独立字段而非复用）：
   * - signal_freq 描述「信号生成」粒度（策略层采样周期），freq 描述「撮合 K 线」
   *   粒度（引擎层回放周期）。日级策略完全可在分钟级 K 线上回测（如 ATR 移动
   *   止损必须分钟级撮合才能刻画盘中穿越），二者解耦才能覆盖宏观 CTA 这种
   *   「日级信号 + 分钟级风控」组合，强行合并会丧失表达力。
   * - 可选字段：缺省时后端按既有日级路径 run() 执行，保证旧调用零回归。
   * - 取值与 signal_freq 保持同词表（'1d'/'1h'/'5m'/'1m'），便于 ParamForm
   *   复用同一组选项，且 brief 仅强约束 '1d'/'5m'/'1m'，这里补 '1h' 与既有
   *   signal_freq 对齐（后端如不支持 '1h' 由其自身校验兜底，前端不收窄）。
   */
  freq?: '1d' | '1h' | '5m' | '1m'
}

/** 组合回测请求 */
export interface PortfolioParams {
  symbols: string[]
  start_date: string
  end_date: string
  initial_capital: number
  n_hmm_states: number
  buffer_threshold: number
  state_weights: Record<string, Record<string, number>>
  // 组合模式 HMM 标量参数（covariance/n_iter/release_lag/max_fill_days）经此通道下发
  strategy_params?: Record<string, unknown>
}

/** 绩效指标 */
export interface Metrics {
  initial_capital: number
  final_nav: number
  total_return: number
  annual_return: number
  annual_volatility: number
  max_drawdown: number
  sharpe_ratio: number
  calmar_ratio: number
  win_rate: number
  profit_loss_ratio: number
  n_trades: number
  n_failed_trades: number
}

/** 净值时序节点 */
export interface NavPoint {
  date: string
  nav: number
  return: number
  cumulative_return: number
}

/** 回撤时序节点 */
export interface DrawdownPoint {
  date: string
  drawdown: number
}

/**
 * 交易记录
 *
 * reason 字段（Task 18 新增，可选）：
 * - 后端引擎 _close() 在风控平仓时回填此字段为「触及止损」/「触及止盈」/「移动止损」
 *   等中文原因；常规信号驱动的买卖不带 reason。
 * - ProChart 据此字段提炼止损/止盈触发点画 markLine 水平线 + 触发标注，
 *   useTerminalState.toLogEntry 据此细化日志级别（error/success/warn）。
 * - 设为可选：日级 run()/run_portfolio() 的 trades 不含 reason，前端容错取值。
 *
 * symbol 字段同理（分钟级 _close/_buy 落库时带 symbol，日级不含），设可选。
 */
export interface TradeRecord {
  date: string
  direction: string
  shares: number
  price: number
  cost: number
  reason?: string
  symbol?: string
}

/**
 * OHLCV 行情节点（K 线）
 *
 * 用于 ProChart 蜡烛图渲染；后端在单资产回测响应中一并返回完整行情序列，
 * 避免前端再发一次行情请求，降低耦合与首屏延迟。
 */
export interface OhlcvPoint {
  date: string
  open: number
  high: number
  low: number
  close: number
  volume: number
}

/**
 * 末态持仓快照行
 *
 * 仅展示回测结束时刻的持仓（symbol/数量/市值），不包含建仓平仓时序，
 * 时序交易请用 trades。market_value 为按末态收盘价计算的市值。
 */
export interface PositionRow {
  symbol: string
  qty: number
  market_value: number
  // 持仓详情（后端 _extract_positions 从 trades 加权平均算得；可选，向后兼容）
  avg_cost?: number             // 持仓加权平均买入成本（元/股）
  unrealized_pnl?: number       // 浮盈额（市值 - 持仓成本），负=亏损
  unrealized_pnl_pct?: number   // 浮盈百分比
  open_date?: string | null     // 建仓日期（首笔买入，YYYY-MM-DD）
  holding_days?: number         // 持仓自然日数
  cash?: number                 // 末态现金
  nav?: number                  // 末态总资产（AUM）
}

/** 单资产回测响应 */
export interface SingleBacktestResponse {
  metrics: Metrics
  nav_series: NavPoint[]
  drawdown_series: DrawdownPoint[]
  trades: TradeRecord[]
  // 行情序列（ProChart 蜡烛图）+ 末态持仓快照（PositionsTable）—— 后端总会返回，故设为必填
  ohlcv: OhlcvPoint[]
  positions: PositionRow[]
}

/** 权重快照节点 */
export interface WeightPoint {
  date: string
  weights: Record<string, number>
}

/** 组合回测响应 */
export interface PortfolioResponse {
  metrics: Metrics
  nav_series: NavPoint[]
  drawdown_series: DrawdownPoint[]
  weight_series: WeightPoint[]
  trades: TradeRecord[]
}

// ============ Axios 实例 ============

/**
 * 创建 Axios 实例
 *
 * 开发环境下 baseURL 为空字符串，由 Vite proxy 转发 /api 到后端
 * 生产环境下可通过 VITE_API_BASE 环境变量覆盖
 *
 * Why export：api/macro.ts 等同域 facade 复用此实例共享响应拦截器（中文错误
 * Toast / 超时降级），避免每个 facade 各自 create 导致拦截器逻辑漂移。
 */
export const apiClient: AxiosInstance = axios.create({
  baseURL: import.meta.env.VITE_API_BASE || '',
  timeout: 60000,   // 默认 60s 超时
  headers: {
    'Content-Type': 'application/json',
  },
})

// ============ 响应拦截器 ============

apiClient.interceptors.response.use(
  // 正常响应直接返回 data
  (response) => response.data,
  // 异常响应：提取后端中文错误信息，ElMessage 弹出
  (error) => {
    let message = '请求失败，请检查网络连接'

    if (error.response) {
      // 后端返回了 HTTP 错误响应
      const status = error.response.status
      const detail = error.response.data?.detail

      if (status === 422) {
        // Pydantic 校验失败，提取字段级错误
        if (Array.isArray(detail)) {
          const errors = detail.map((e: any) => e.msg).join('；')
          message = `参数校验失败：${errors}`
        } else {
          message = `参数校验失败：${detail}`
        }
      } else if (status === 500) {
        message = detail || '服务器内部错误'
      } else if (status === 504) {
        message = '回测执行超时，请缩小日期范围'
      } else {
        message = detail || `请求失败（HTTP ${status}）`
      }
    } else if (error.code === 'ECONNABORTED') {
      message = '请求超时，请缩小日期范围或重试'
    }

    ElMessage.error(message)
    return Promise.reject(error)
  }
)

// ============ API 函数 ============

/**
 * 执行单资产回测
 *
 * @param params 回测参数
 * @returns 回测结果（净值时序、回撤时序、绩效指标、交易记录）
 */
export function runSingleBacktest(params: SingleBacktestParams): Promise<SingleBacktestResponse> {
  return apiClient.post('/api/v1/backtest/run', params, {
    timeout: 60000,
  })
}

/**
 * 创建 per-run 异步回测（Epic 4：建 run + SSE 流式推送）
 *
 * 设计意图（为何单独拆一个建 run 接口）：
 * - 阻塞式 axios.post 会把整个回测计算周期压在一个 HTTP 连接里，浏览器
 *   侧无任何中间反馈，长回测易触发 502/网关超时；用户体验差。
 * - 改为两段式：先本接口拿到 run_id（毫秒级返回），再由前端开原生
 *   EventSource(/run/stream/{run_id}) 接收 progress/trade/risk/result 帧。
 *   后端可边算边推，前端可边收边渲染买卖点。
 * - 仅"建 run"用 axios（短连接 + JSON body），SSE 接收走原生 EventSource
 *   （EventSource 不支持自定义 header/body，只支持 GET）。
 *
 * 超时 30s：建 run 本身只做参数校验 + 注册，正常 <1s；留余量防慢启动。
 *
 * @param params 回测参数（形状与 SingleBacktestParams 对齐，原样透传给后端）
 * @returns { run_id } —— 用于拼接 SSE 端点 URL
 */
export function createBacktestRun(params: SingleBacktestParams): Promise<{ run_id: string }> {
  return apiClient.post('/api/v1/backtest/run/async', params, { timeout: 30000 })
}

/**
 * 执行组合回测
 *
 * 组合回测包含 HMM 训练，耗时更长，超时设为 120 秒
 *
 * @param params 组合回测参数
 * @returns 组合回测结果（含权重时序）
 */
export function runPortfolioBacktest(params: PortfolioParams): Promise<PortfolioResponse> {
  return apiClient.post('/api/v1/portfolio/run', params, {
    timeout: 120000,
  })
}

// ============ 策略元数据 / JSON Schema（Task 9） ============

/**
 * 策略元数据（GET /strategies 返回项）
 *
 * - name：策略唯一标识（提交时作为 strategy_name）
 * - label：中文展示名（下拉框用）
 * - universe：策略可交易的标的域（用于前端校验提示，不强约束）
 */
export interface StrategyMeta {
  name: string
  label: string
  universe: string[]
}

/**
 * JSON Schema 单字段描述（含 ui 渲染提示）
 *
 * 设计意图（反黑盒）：控件的约束（minimum/maximum/step/enum）全部取自后端 params_model
 * 生成的 JSON Schema，前端不重复定义，确保单一真相源。
 */
export interface JsonSchemaProperty {
  type?: string
  description?: string
  minimum?: number
  maximum?: number
  default?: unknown
  enum?: string[]
  ui?: {
    control?: 'slider' | 'input-number' | 'select'
    group?: string
    step?: number
    options?: Array<{ label: string; value: string }>
  }
}

/** 策略参数 JSON Schema 整体（properties 为字段字典） */
export interface StrategyParamSchema {
  type: string
  properties: Record<string, JsonSchemaProperty>
  order?: string[]
}

/**
 * 列出已注册策略
 *
 * 路由：GET /api/v1/strategies（Task 7 实现）
 */
export function getStrategies(): Promise<StrategyMeta[]> {
  return apiClient.get('/api/v1/strategies')
}

/**
 * 获取策略参数 JSON Schema
 *
 * 路由：GET /api/v1/strategies/{name}/schema（Task 7 实现）
 * 前端按返回的 properties[*].ui.control 选择控件类型渲染。
 */
export function getStrategySchema(name: string): Promise<StrategyParamSchema> {
  return apiClient.get(`/api/v1/strategies/${name}/schema`)
}
