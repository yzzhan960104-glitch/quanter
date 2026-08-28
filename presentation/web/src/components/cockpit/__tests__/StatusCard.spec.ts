/**
 * StatusCard 心跳小部件单测（W4-B 改接掘金后的契约重建，2026-08-28）。
 *
 * 物理意图：onMounted → getOverview（server /gm/overview 代理 7002 strategies）→
 * 按 stage 映射三态（running/stopped/unreachable）；失败显式转「终端不可达」。
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import ElementPlus from 'element-plus'

// ---- jsdom 缺失 API 的最小 polyfill ----
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

const { mockOverview } = vi.hoisted(() => ({
  mockOverview: {
    strategies: [{ id: 'd9324346', name: 'neckline_pilot', stage: 'running' }],
    account_statuses: [],
  },
}))

vi.mock('../../../api/gm', () => ({
  getOverview: vi.fn().mockResolvedValue(mockOverview),
}))

import StatusCard from '../StatusCard.vue'

const mountCard = () => mount(StatusCard, { global: { plugins: [ElementPlus] } })

describe('StatusCard.vue', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.clearAllMocks()
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('策略 running → 「策略运行中」+ 策略行', async () => {
    const w = mountCard()
    await flushPromises()
    expect(w.text()).toContain('策略运行中')
    expect(w.text()).toContain('neckline_pilot:stagerunning')
  })

  it('无 running 策略 → 「策略已停止」', async () => {
    const { getOverview } = await import('../../../api/gm')
    ;(getOverview as any).mockResolvedValueOnce({
      strategies: [{ id: 'x', name: 's', stage: 'stopped' }], account_statuses: null })
    const w = mountCard()
    await flushPromises()
    expect(w.text()).toContain('策略已停止')
  })

  it('代理 502（终端断连）→ 显式「终端不可达」（不静默保持假绿）', async () => {
    const { getOverview } = await import('../../../api/gm')
    ;(getOverview as any).mockRejectedValueOnce(new Error('502'))
    const w = mountCard()
    await flushPromises()
    expect(w.text()).toContain('终端不可达')
  })

  it('5s 轮询持续调用，卸载后停止', async () => {
    const { getOverview } = await import('../../../api/gm')
    const w = mountCard()
    await flushPromises()
    expect(getOverview).toHaveBeenCalledTimes(1)
    vi.advanceTimersByTime(5000)
    await flushPromises()
    expect(getOverview).toHaveBeenCalledTimes(2)
    w.unmount()
    vi.advanceTimersByTime(10000)
    await flushPromises()
    expect(getOverview).toHaveBeenCalledTimes(2)
  })
})
