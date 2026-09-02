/**
 * P6 研究线两组件契约（ProposalsView + VersionHistoryCard）。
 *
 * ①提案流：状态 tag（PUBLISHED 已发布/REJECTED 已否决）+ 参数 diff 高亮
 *（相邻提案同键值变→drift 类）；②版本演进：v 序号时间线 + best_annual
 * 条形（无指标留空）+ 状态 tag 中文化。
 */
import { describe, it, expect, vi } from 'vitest'
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

const { proposals, versions } = vi.hoisted(() => ({
  proposals: {
    generated_at: '2026-09-03 01:30:00',
    pipeline: { PUBLISHED: 1, REJECTED: 1 },
    rows: [
      { id: 'p_1', created_at: '2026-09-02T18:30:11', status: 'PUBLISHED',
        hypothesis: '收紧止损降低深亏', params: { stop_atr_mult: 2.0, min_rr: 2.5 },
        expected: '减少止损累计亏损', risk: '止损过紧',
        verification: { verdict: 'published', reason: 'inner 改善达标' } },
      { id: 'p_0', created_at: '2026-09-01T18:30:08', status: 'REJECTED',
        hypothesis: '收紧止损', params: { stop_atr_mult: 2.0, min_rr: 1.5 },
        expected: '降低深亏', risk: '同上',
        verification: { verdict: 'rejected', reason: 'inner 无改善' } },
    ],
  },
  versions: {
    generated_at: '2026-09-03 01:30:00',
    rows: [
      { experiment_id: 'v_a', version: 1, status: 'ARCHIVED', weight: 1.0,
        best_annual: 18.4, calmar: 7.24, note: 'outer ann=18.4% calmar=7.24',
        created_at: '2026-07-25T08:21:37', params: {} },
      { experiment_id: 'v_b', version: 2, status: 'DRAFT', weight: 0.0,
        best_annual: null, calmar: null, note: '无指标版本',
        created_at: '2026-08-16T13:25:55', params: {} },
    ],
  },
}))

vi.mock('../../../api/research', () => ({
  getProposals: vi.fn().mockResolvedValue(proposals),
  getVersions: vi.fn().mockResolvedValue(versions),
}))

import ProposalsView from '../../../views/ProposalsView.vue'
import VersionHistoryCard from '../VersionHistoryCard.vue'

describe('P6 研究线组件', () => {
  it('ProposalsView：状态 tag+管道计数+参数 diff 高亮（min_rr 1.5→2.5）', async () => {
    const w = mount(ProposalsView, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    expect(w.text()).toContain('已发布')
    expect(w.text()).toContain('已否决')
    expect(w.text()).toContain('PUBLISHED 1')
    expect(w.text()).toContain('收紧止损降低深亏')
    // diff 高亮：相邻提案 min_rr 变更（1.5→2.5）着色；stop_atr_mult 不变不亮
    const drifted = w.findAll('.param.drift').map((x) => x.text())
    expect(drifted.some((t) => t.includes('min_rr'))).toBe(true)
    expect(drifted.some((t) => t.includes('stop_atr_mult'))).toBe(false)
  })

  it('VersionHistoryCard：v 序号+状态中文化+年化条形（null 留空）', async () => {
    const w = mount(VersionHistoryCard, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    expect(w.text()).toContain('v1')
    expect(w.text()).toContain('归档')
    expect(w.text()).toContain('草稿')
    expect(w.text()).toContain('18.4%')
    // 有指标的版本画条形，无指标的显 —
    expect(w.findAll('.vbar').length).toBe(1)
  })
})
