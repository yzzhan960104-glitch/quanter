/**
 * OrdersPanel 当日委托单测（P5.1 契约）。
 *
 * 物理意图：挂载 → getOrders（静态快照 gm_orders_<leg> / 在线 /gm/orders）→
 * 全状态行渲染：方向 tag、已成交 n/N、状态映射（3=成交 8=已拒+拒因 tooltip
 * 5/6=已撤、有部分成交显部分）；拒因经 tooltip 内容呈现（柜台 ordRejReason）。
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

const { mockOrders } = vi.hoisted(() => ({
  mockOrders: [
    { symbol: 'SZSE.300077', side: 1, status: 3, price: 25.33,
      volume: 500, filled_volume: 500, cl_ord_id: 'fcd36c2b-a66d',
      created_at: '2026-09-02T01:31:14.855Z', ordRejReason: null },
    { symbol: 'SHSE.688808', side: 1, status: 8, price: 28.9,
      volume: 200, filled_volume: 0, cl_ord_id: 'aaa36c2b-a66d',
      created_at: '2026-09-02T01:31:15.000Z',
      ordRejReason: '定尺不足最小申报量（科创板≥200）' },
    { symbol: 'SZSE.300182', side: 1, status: 5, price: 4.5,
      volume: 10000, filled_volume: 0, cl_ord_id: 'bbb36c2b-a66d',
      created_at: '2026-09-02T01:31:16.000Z', ordRejReason: null },
    // J-4 回归：status 6=PendingCancel 待撤（非终态）——绝不可标「已撤」
    { symbol: 'SHSE.688111', side: 2, status: 6, price: 88.0,
      volume: 300, filled_volume: 0, cl_ord_id: 'ccc36c2b-a66d',
      created_at: '2026-09-02T01:31:17.000Z', ordRejReason: null },
  ],
}))

vi.mock('../../../api/gm', () => ({
  getOrders: vi.fn().mockResolvedValue(mockOrders),
}))
vi.mock('../../../api/home', () => ({
  getOhlcv: vi.fn().mockResolvedValue(null),
}))

import OrdersPanel from '../OrdersPanel.vue'

const mountPanel = () => mount(OrdersPanel, {
  global: { plugins: [ElementPlus] },
})

describe('OrdersPanel.vue', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('挂载即拉委托并渲染全状态行（成交/已拒/已撤）', async () => {
    const w = mountPanel()
    await flushPromises()
    expect(w.text()).toContain('300077.SZ')
    expect(w.text()).toContain('买入')
    expect(w.text()).toContain('25.33')
    // 已成交 n/N 读数
    expect(w.text()).toContain('500/500')
    expect(w.text()).toContain('0/200')
    // 状态映射：3=成交 8=已拒 5=已撤 6=待撤（非终态，J-4 单源化回归）
    expect(w.text()).toContain('成交')
    expect(w.text()).toContain('已拒')
    expect(w.text()).toContain('已撤')
    expect(w.text()).toContain('待撤')
    expect(w.text()).not.toContain('已撤 ⚠')
    // 委托号截短 8 位
    expect(w.text()).toContain('fcd36c2b')
  })

  it('拒因经 tooltip 展示（el-tooltip content=ordRejReason）', async () => {
    const w = mountPanel()
    await flushPromises()
    // jsdom 不渲染 teleport 的 tooltip 浮层——断言 ElTooltip 的 content prop
    const tip = w.findAllComponents({ name: 'ElTooltip' })
        .find((t) => String(t.props('content')).includes('定尺不足'))
    expect(tip).toBeTruthy()
    expect(w.text()).toContain('已拒')
  })

  it('空委托渲染空态不报错', async () => {
    const { getOrders } = await import('../../../api/gm')
    ;(getOrders as any).mockResolvedValueOnce([])
    const w = mountPanel()
    await flushPromises()
    expect(w.text()).toContain('当日无委托')
  })
})
