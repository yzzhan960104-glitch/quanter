/**
 * TradesTable 成交流水单测（W4-B 改接掘金后的契约重建，2026-08-28）。
 *
 * 物理意图：onMounted → getTrades（server /gm/trades 代理 7002 execrpts）→
 * 渲染标的/数量/价格/成交编号；超 50 条本地分页。
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import ElementPlus from 'element-plus'

// ---- jsdom 缺失 API 的最小 polyfill（同族 spec 共用范式） ----
class MockObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
  takeRecords() {
    return []
  }
}
;(globalThis as any).ResizeObserver = MockObserver
;(globalThis as any).IntersectionObserver = MockObserver

const { mockRows } = vi.hoisted(() => ({
  mockRows: [
    { symbol: 'SZSE.300433', volume: 100, price: 38.91, execId: 'EX-001' },
    { symbol: 'SHSE.688981', volume: 200, price: 102.5, execId: 'EX-002' },
  ],
}))

vi.mock('../../../api/gm', () => ({
  getTrades: vi.fn().mockResolvedValue(mockRows),
}))

import TradesTable from '../TradesTable.vue'

const mountTable = () => mount(TradesTable, { global: { plugins: [ElementPlus] } })

describe('TradesTable.vue', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('挂载即拉流水并渲染行', async () => {
    const w = mountTable()
    await flushPromises()
    expect(w.text()).toContain('SZSE.300433')
    expect(w.text()).toContain('SHSE.688981')
    expect(w.text()).toContain('38.91')
    expect(w.text()).toContain('EX-001')
  })

  it('空流水渲染空态不报错', async () => {
    const { getTrades } = await import('../../../api/gm')
    ;(getTrades as any).mockResolvedValueOnce([])
    const w = mountTable()
    await flushPromises()
    expect(w.text()).toContain('成交')
  })
})
