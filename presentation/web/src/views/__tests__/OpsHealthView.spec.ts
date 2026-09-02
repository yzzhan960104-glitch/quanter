/**
 * OpsHealthView 运维健康面板单测（P5.4 契约）。
 *
 * 物理意图：挂载 → getOpsHealth（ops_health.json 快照）→ 三段渲染：
 * ①进程存活灯（server/emgm3/gateway/双腿 stage）②台账 pivot（行=日期
 * 列=任务，格=状态，failed/skipped 有 tooltip message）③告警时间线行数
 * =alerts 数组长度（验收红线）。快照缺失 → 全段空态不炸。
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import ElementPlus from 'element-plus'

class MockObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
  takeRecords() { return [] }
}
;(globalThis as any).ResizeObserver = MockObserver
;(globalThis as any).IntersectionObserver = MockObserver

const { doc } = vi.hoisted(() => ({
  doc: {
    generated_at: '2026-09-03 01:20:00',
    job_runs: [
      { job: 'pipeline', date: '2026-09-02', status: 'done',
        started_at: '2026-09-02T18:00:00', finished_at: '2026-09-02T18:01:58', message: '' },
      { job: 'pre_open', date: '2026-08-27', status: 'skipped',
        started_at: '2026-08-27T09:22:00', finished_at: '2026-08-27T09:22:00',
        message: '人工风控开关：拦截增量下单' },
      { job: 'brief_data', date: '2026-09-02', status: 'done',
        started_at: '2026-09-02T18:01:58', finished_at: '2026-09-02T18:01:58', message: '' },
    ],
    alerts: [
      { ts: '2026-09-03T01:16:35', level: 'CRITICAL', msg: '数据未就绪：eod 跳过' },
      { ts: '2026-09-02T14:37:50', level: 'WARN', msg: '盘后对账偏差 max_abs_drift=100' },
    ],
    processes: {
      note: '快照时点探测（非实时）；None=探测失败（≠不存在）',
      server: { running: true, pids: [24320] },
      emgm3: 4, gateway: 1,
      legs: [
        { key: 'main', label: '主腿', stage: '3' },
        { key: 'exp', label: '实验腿', stage: null },
      ],
    },
  },
}))

vi.mock('../../api/ops', () => ({
  getOpsHealth: vi.fn().mockResolvedValue(doc),
}))

import OpsHealthView from '../OpsHealthView.vue'

const mountView = () => mount(OpsHealthView, { global: { plugins: [ElementPlus] } })

describe('OpsHealthView.vue', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('挂载即渲染进程灯/台账 pivot/告警时间线（行数=alerts 长度）', async () => {
    const w = mountView()
    await flushPromises()
    expect(w.text()).toContain('进程拓扑')
    expect(w.text()).toContain('观测 Server')
    expect(w.text()).toContain('掘金网关')
    expect(w.text()).toContain('任务台账')
    // 台账列=任务去重（pipeline/pre_open/brief_data），格=状态
    expect(w.text()).toContain('pipeline')
    expect(w.text()).toContain('brief_data')
    // 告警条数=快照 alerts 长度（验收：告警行数=解析行数）
    expect(w.findAll('.el-timeline-item').length).toBe(doc.alerts.length)
    expect(w.text()).toContain('CRITICAL')
    expect(w.text()).toContain('盘后对账偏差')
  })

  it('skip 格 tooltip 带 message（人工风控拦截可读）', async () => {
    const w = mountView()
    await flushPromises()
    const tip = w.findAllComponents({ name: 'ElTooltip' })
        .find((t) => String(t.props('content') || '').includes('人工风控开关'))
    expect(tip).toBeTruthy()
  })

  it('快照缺失 → 空态不炸', async () => {
    const { getOpsHealth } = await import('../../api/ops')
    ;(getOpsHealth as any).mockResolvedValueOnce(null)
    const w = mountView()
    await flushPromises()
    expect(w.text()).toContain('近段无告警记录')
  })
})
