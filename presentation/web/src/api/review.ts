/**
 * 亏损持仓 LLM 深度归因 facade（2026-09-03 · ops/loser_review.py 产物）。
 *
 * 数据=loser_review.json 快照（16:15 盘后 cron 逐只 glm-5.3 归因；
 * 快照层透传当日产物，16:15 前发布=legs null 降级）。只读分析红线：
 * risk_state 三档是状态描述，绝不构成交易指令。
 */
import { staticGet } from './static'

/** 单持仓归因结果（LLM 结构化块；解析失败全 null+error）。 */
export interface LoserAnalysis {
  primary?: string | null        // 主因
  secondary?: string | null      // 次因
  evidence?: string | null       // 量化证据
  confidence?: string | null     // 高/中/低
  risk_state?: string | null     // 持有观察/收紧关注/临近风控线（词表外 null）
  markdown?: string | null       // 深度分析正文
  error?: string | null
}

/** 单持仓行（事实块来自 state/audit/湖；_ 前缀内部键已在生成端剔除）。 */
export interface LoserRow {
  leg: string
  leg_label?: string
  symbol: string
  name?: string
  industry?: string
  entry_date?: string | null
  entry_price?: number | null
  qty?: number
  last?: number | null
  fpnl?: number
  fpnl_pct?: number
  days_held?: number | null
  max_holding?: number
  expire_date?: string | null
  stop?: number | null
  dist_stop_pct?: number | null
  tp1_price?: number | null
  tp2_price?: number | null
  trailing?: { neckline?: number | null; atr?: number | null; grace?: number
               in_grace?: boolean; steps_taken?: number; step?: number | null }
  signal?: { neckline?: number | null; rr?: number | null
             formed_at?: string | null; entry_theory?: number | null
             atr?: number | null } | null
  kline_summary?: { high?: number | null; low?: number | null
                    dd_pct?: number | null; since_entry_pct?: number | null }
  market?: { sh_pct?: number | null; hs300_pct?: number | null }
  analysis?: LoserAnalysis
}

export interface LoserLeg {
  leg: string
  label?: string
  rows: LoserRow[]
}

export interface LoserReviewDoc {
  day: string
  generated_at?: string
  model?: string
  legs: LoserLeg[] | null       // null=当日未生成（16:15 前/生成失败）
  note?: string
}

export function getLoserReview(): Promise<LoserReviewDoc | null> {
  return staticGet<LoserReviewDoc | null>('loser_review', null)
}
