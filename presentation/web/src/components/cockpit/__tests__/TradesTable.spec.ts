/**
 * TradesTable 成交流水单测（2026-09-01 全字段升级后的契约重建）。
 *
 * 物理意图：onMounted → getTrades（server /gm/trades 代理 7002 execrpts）→
 * 全字段渲染：时间/方向 tag（买红卖绿）/ts 化标的+公司名/价格/数量/金额/委托号；
 * 公司名经 ohlcv 快照富化（缺快照留空不猜）。超 50 条本地分页。
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
    { symbol: 'SZSE.300433', volume: 100, price: 38.91, amount: 3891.0,
      side: 1, created_at: '2026-09-01T01:31:16.576Z', cl_ord_id: 'd26503ed-a5a4' },
    { symbol: 'SHSE.688981', volume: 200, price: 102.5, amount: 20500.0,
      side: 2, created_at: '2026-09-01T02:00:00.000Z', cl_ord_id: 'd26671b9-a5a4' },
  ],
}))

vi.mock('../../../api/gm', () => ({
  getTrades: vi.fn().mockResolvedValue(mockRows),
}))
vi.mock('../../../api/home', () => ({
  getOhlcv: vi.fn().mockResolvedValue(null),      // 无快照：公司名留空不猜
}))

import TradesTable from '../TradesTable.vue'

const mountTable = () => mount(TradesTable, { global: { plugins: [ElementPlus] } })

describe('TradesTable.vue', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('挂载即拉流水并渲染全字段行', async () => {
    const w = mountTable()
    await flushPromises()
    // 标的 ts 化展示（gm 原始符号不直接示人）
    expect(w.text()).toContain('300433.SZ')
    expect(w.text()).toContain('688981.SH')
    expect(w.text()).toContain('38.91')
    // 方向 tag：买/卖（A 股语义）
    expect(w.text()).toContain('买入')
    expect(w.text()).toContain('卖出')
    // 金额（toFixed(0)，无千分位）与委托号（截短 8 位）
    expect(w.text()).toContain('20500')
    expect(w.text()).toContain('d26503ed')
  })

  it('空流水渲染空态不报错', async () => {
    const { getTrades } = await import('../../../api/gm')
    ;(getTrades as any).mockResolvedValueOnce([])
    const w = mountTable()
    await flushPromises()
    expect(w.text()).toContain('交易流水')       // 空态下卡片与表头照常渲染
    expect(w.text()).toContain('No Data')
  })
})
