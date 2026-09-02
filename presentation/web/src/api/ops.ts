/**
 * 运维健康数据 facade（全读可视化 P5.4 · 2026-09-03）。
 *
 * 数据=ops_health.json 静态快照（ops/public_snapshot._ops_health 生成：
 * job_run 近 14 日台账 + alerts.log 尾部 200 条 + 快照时点进程拓扑）。
 * 公开态无在线端点（方案不变量①：新增服务端端点=最后手段）——在线态同读
 * 随包快照（LAN dist 部署亦带）。
 */
import { staticGet } from './static'

/** job_run 台账行（logs/trading_job_run.db 只读查询镜像）。 */
export interface JobRunRow {
  job: string
  date: string
  status: 'done' | 'failed' | 'skipped' | string
  started_at?: string
  finished_at?: string
  message?: string
}

/** 告警行（alerts.log `ts | LEVEL | msg` 解析）。 */
export interface AlertRow {
  ts: string
  level: string          // CRITICAL / ERROR / WARN / INFO
  msg: string
}

/** 进程拓扑（快照时点探测；null=探测失败≠不存在）。 */
export interface OpsProcesses {
  note?: string
  probe_error?: string
  server?: { running: boolean; pids?: number[] }
  emgm3?: number | null
  gateway?: number | null
  legs?: Array<{ key: string; label: string; stage?: string | null }>
}

export interface OpsHealthDoc {
  generated_at: string
  job_runs: JobRunRow[]
  alerts: AlertRow[]
  processes: OpsProcesses
  note?: string
}

export function getOpsHealth(): Promise<OpsHealthDoc | null> {
  return staticGet<OpsHealthDoc | null>('ops_health', null)
}
