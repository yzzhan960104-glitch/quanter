/**
 * 研究线数据 facade（全读可视化 P6 · 2026-09-03）。
 *
 * 三快照全由 ops/public_snapshot._research_files 生成：
 * - proposals.json：研究提案流（logs/research_proposals.db 只读镜像）
 * - experiment_versions.json：版本演进（experiment.store.list_versions）
 * - backtest_queue.json：回测队列 + digest 漂移对照
 */
import { staticGet } from './static'

/** 研究提案行（假设→参数→预期→验证 verdict 全链）。 */
export interface ProposalRow {
  id: string
  created_at: string
  change_type?: string
  hypothesis?: string
  params?: Record<string, unknown> | { raw?: string } | null
  expected?: string
  risk?: string
  status: string                  // PUBLISHED / REJECTED / ...
  verification?: Record<string, unknown> | { raw?: string } | null
  experiment_id?: string | null
  note?: string
}

export interface ProposalsDoc {
  generated_at: string
  pipeline: Record<string, number>
  rows: ProposalRow[]
  note?: string
}

export function getProposals(): Promise<ProposalsDoc | null> {
  return staticGet<ProposalsDoc | null>('proposals', null)
}

/** 版本演进行（best_annual/calmar 从 note 抽取，无指标时 null）。 */
export interface VersionRow {
  experiment_id: string
  strategy_name?: string
  version?: number
  status: string                  // ACTIVE / ARCHIVED / DRAFT / DISCARDED...
  weight?: number
  best_annual?: number | null
  calmar?: number | null
  note?: string
  created_at?: string
  params?: Record<string, unknown>
}

export interface VersionsDoc {
  generated_at: string
  rows: VersionRow[]
  note?: string
}

export function getVersions(): Promise<VersionsDoc | null> {
  return staticGet<VersionsDoc | null>('experiment_versions', null)
}

/** 回测队列任务行（近 20）。 */
export interface ReplayTask {
  task: string
  created_at: string
  status: string                  // SUCCESS / FAILED / PENDING / RUNNING...
  progress?: number
  window?: string
  cfg?: Record<string, unknown> | null
  error?: string | null
  finished_at?: string | null
}

export interface DigestSummary {
  day?: string | null
  live?: { trades?: string | null; win_rate?: string | null; avg_rr?: string | null }
  expect?: { trades?: string | null; win_rate?: string | null; avg_rr?: string | null }
  drift?: string | null
}

export interface BacktestQueueDoc {
  generated_at: string
  tasks: ReplayTask[]
  digest?: DigestSummary
  note?: string
}

export function getBacktestQueue(): Promise<BacktestQueueDoc | null> {
  return staticGet<BacktestQueueDoc | null>('backtest_queue', null)
}
