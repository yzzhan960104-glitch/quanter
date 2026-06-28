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

/** 交易记录 */
export interface TradeRecord {
  date: string
  direction: string
  shares: number
  price: number
  cost: number
}

/** 单资产回测响应 */
export interface SingleBacktestResponse {
  metrics: Metrics
  nav_series: NavPoint[]
  drawdown_series: DrawdownPoint[]
  trades: TradeRecord[]
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
 */
const apiClient: AxiosInstance = axios.create({
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
