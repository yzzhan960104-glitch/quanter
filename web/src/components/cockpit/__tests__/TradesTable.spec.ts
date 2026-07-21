/**
 * TradesTable 流水表组件单测（Task 9 · 严格 TDD）。
 *
 * 物理意图：验证组件 onMounted → queryTrades 当天流水 → el-table 渲染行 + 方向徽章
 * （buy=danger / sell=success）整条装配链路在 jsdom 下能跑通。
 *
 * Why mock queryTrades：组件 setup 即拉当天流水，真实 fetch 在 jsdom 下无网络/无后端，
 * 必须用 vi.mock 替换 api/trading 模块的 queryTrades，返回固定 TradesPage 结构。
 *
 * Why flushPromises：EP el-table 接收 :data 后异步经 store.commit('setData') 渲染行，
 * 同步 mount 立即断言拿不到行；await flushPromises 等微任务走完才断言。
 *
 * Why 顶部 polyfill：jsdom 不实现 ResizeObserver/IntersectionObserver/matchMedia，EP 的
 * el-table/el-tag/el-pagination 依赖它们做定位/响应式测量，不补会在 mount 时抛 TypeError。
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import TradesTable from '../TradesTable.vue'

// ---- jsdom 缺失 API 的最小 polyfill（满足 EP 不抛，不模拟真实行为）----
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
;(globalThis as any).matchMedia =
  (globalThis as any).matchMedia ||
  ((q: string) => ({
    matches: false,
    media: q,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  }))

// 固定分页响应：两条相反方向的流水，覆盖 buy(danger)/sell(success) 徽章分支。
// 注意 shares/price 用 number（对齐 TradeRecord 联合类型 number|string 的数值分支）。
//
// Why vi.hoisted：vi.mock 工厂被 vitest 提升到文件顶部执行，普通顶层 const 在工厂内
// 引用会触发 TDZ（Cannot access before initialization）。vi.hoisted 让夹具与 mock
// 一起提升，保证工厂执行时夹具已就绪。
const { mockPage } = vi.hoisted(() => ({
  mockPage: {
    trades: [
      { timestamp: '2026-07-21 09:35:00', symbol: '510300.SH', direction: 'buy', shares: 100, price: 4.0 },
      { timestamp: '2026-07-21 10:00:00', symbol: '159915.SZ', direction: 'sell', shares: 100, price: 5.0 },
    ],
    total: 2,
    limit: 100,
    offset: 0,
  },
}))

vi.mock('../../../api/trading', () => ({
  queryTrades: vi.fn().mockResolvedValue(mockPage),
}))

const mountTable = () => mount(TradesTable, { global: { plugins: [ElementPlus] } })

describe('TradesTable.vue', () => {
  beforeEach(() => {
    // clearAllMocks 仅清调用计数/调用记录，不动 mockResolvedValue 的实现，
    // 故 queryTrades 仍按工厂里设的默认值返回 mockPage，无需在每个 case 重设。
    vi.clearAllMocks()
  })

  it('渲染流水行 + 方向徽章', async () => {
    const w = mountTable()
    await flushPromises()
    // 两条标的均应出现在表格内。
    expect(w.text()).toContain('510300.SH')
    expect(w.text()).toContain('159915.SZ')
    // 方向徽章文本：buy / sell。
    expect(w.text()).toContain('buy')
    expect(w.text()).toContain('sell')
  })

  it('buy 方向徽章为 danger、sell 方向徽章为 success', async () => {
    const w = mountTable()
    await flushPromises()
    const tags = w.findAllComponents({ name: 'ElTag' })
    expect(tags.length).toBeGreaterThanOrEqual(2)
    // 按行序：第一行 buy → danger；第二行 sell → success。
    expect(tags[0].props('type')).toBe('danger')
    expect(tags[1].props('type')).toBe('success')
  })
})
