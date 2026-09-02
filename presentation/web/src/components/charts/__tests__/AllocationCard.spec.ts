/**
 * AllocationCard 资产构成与行业分布单测（P5.2 契约）。
 *
 * 物理意图：①行业环形=双腿持仓 market_value 按 industry 聚合（缺映射归
 * 未分类，缺市值行跳过）；②堆叠面积=nav_history days[].assets 双腿齐日
 * 的现金/市值序列（cash+mv=nav 逐日闭合）。快照缺失 → 两图空态不炸。
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

// jsdom 无 canvas npm 包：zrender 拿不到 2d context 会 unhandled rejection——
// stub 一个全方法 Proxy context（measureText 返回零宽，dpr 等 set 全吞）。
const ctxStub: any = new Proxy({}, {
  get: (_t, p) => {
    if (p === 'measureText') return () => ({ width: 0 })
    if (p === 'canvas') return null
    return () => undefined
  },
  set: () => true,
})
;(globalThis as any).HTMLCanvasElement.prototype.getContext = () => ctxStub

const { navDoc, positions, legs } = vi.hoisted(() => ({
  navDoc: {
    base: 200000, era_start: '2026-08-31',
    days: [
      { date: '2026-09-01', legs: { main: 199743, exp: 200000 },
        assets: { main: { available: 91493, market_value: 108250 },
                  exp: { available: 200000, market_value: 0 } } },
      { date: '2026-09-02', legs: { main: 198014, exp: 199843 },
        assets: { main: { available: 67899, market_value: 130115 },
                  exp: { available: 190967, market_value: 8876 } } },
      { date: '2026-09-03', legs: { main: 198014 }, assets: {} },  // 残日：不入轴
    ],
  },
  positions: [
    { symbol: 'SZSE.300433', volume: 100, vwap: 38.9, market_value: 4431,
      fpnl: 531, industry: '半导体' },
    { symbol: 'SHSE.688808', volume: 200, vwap: 60.1, market_value: 12800,
      fpnl: -1420, industry: '半导体' },
    { symbol: 'SZSE.300077', volume: 500, vwap: 21.3, market_value: 10640,
      fpnl: 0, industry: null },                        // 缺映射 → 未分类
  ],
  legs: [
    { key: 'main', label: '主腿', role: 'incumbent',
      account_id: 'a', strategy_id: 's1', strategy_name: 'n1' },
    { key: 'exp', label: '实验腿', role: 'challenger',
      account_id: 'b', strategy_id: 's2', strategy_name: 'n2' },
  ],
}))

vi.mock('../../../api/home', () => ({
  getNavHistory: vi.fn().mockResolvedValue(navDoc),
}))
vi.mock('../../../api/gm', () => ({
  getPositions: vi.fn().mockImplementation((leg: string) =>
    Promise.resolve(leg === 'main' ? positions : [])),
  getLegs: vi.fn().mockResolvedValue(legs),
}))

import AllocationCard from '../AllocationCard.vue'

const mountCard = () => mount(AllocationCard, { global: { plugins: [ElementPlus] } })

describe('AllocationCard.vue', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('挂载即拉双腿持仓+净值史；行业聚合 2 组（半导体+未分类）、市值合计正确', async () => {
    const w = mountCard()
    await flushPromises()
    expect(w.text()).toContain('资产构成与行业分布')
    // 行业数=去重行业数（半导体+未分类）
    expect(w.text()).toContain('2 个行业')
    // 持仓市值合计 4431+12800+10640=27871 → 2.8 万
    expect(w.text()).toContain('持仓市值 2.8 万')
    // 资产轴只含双腿 assets 齐的日子（2 天）——残日 09-03 剔除
    expect(w.text()).not.toContain('资产构成历史累积中')
  })

  it('快照缺失 → 双图空态不炸', async () => {
    const { getNavHistory } = await import('../../../api/home')
    const { getPositions } = await import('../../../api/gm')
    ;(getNavHistory as any).mockRejectedValueOnce(new Error('快照缺失'))
    ;(getPositions as any).mockRejectedValueOnce(new Error('快照缺失'))
    const w = mountCard()
    await flushPromises()
    expect(w.text()).toContain('资产构成与行业分布')     // 卡壳照常
    expect(w.text()).toContain('空仓（无行业分布）')
    expect(w.text()).toContain('资产构成历史累积中')
  })
})
