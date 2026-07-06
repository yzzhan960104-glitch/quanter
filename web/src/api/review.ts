/**
 * 层级六·AI 复盘 API 封装
 *
 * 对应后端 server/api/v1/review.py。复用 backtest.ts 的 apiClient。
 * 超时 90s：LLM 推理耗时较长，前端配合 loading 态。
 */
import { apiClient } from './backtest'

export interface ReviewRequest {
  csv_text?: string                    // 直接上传的日志文本（优先）
  start?: string                       // 或按日期读 logs/live_trades.csv
  end?: string
  strategy_name?: string
  strategy_params?: Record<string, unknown>
  metrics?: Record<string, unknown>    // 关键指标（max_drawdown 等）
}

export interface ReviewReport {
  ok: boolean
  report: string                       // Markdown 报告（或降级摘要）
  model?: string | null                // 实际模型；降级时 null
  degraded: boolean                    // LLM 不可用 → true
  reason?: string | null
}

/** 生成 AI 复盘报告（GLM；缺凭证时后端降级返回上下文摘要） */
export function diagnose(req: ReviewRequest): Promise<ReviewReport> {
  return apiClient.post('/api/v1/review/diagnose', req, { timeout: 90000 })
}
