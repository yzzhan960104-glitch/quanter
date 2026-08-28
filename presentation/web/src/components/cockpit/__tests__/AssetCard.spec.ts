/**
 * AssetCard 资金小部件单测（W4-B 改接掘金后的契约重建，2026-08-28）。
 *
 * 物理意图：onMounted → getAsset（server /gm/asset 代理掘金 cash 端点）→ 渲染
 * 总权益(nav)/可用资金(available) 的 toFixed(0) 整数；字段缺失/零 → 「—」防误读归零。
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

const { mockAsset } = vi.hoisted(() => ({
  mockAsset: { nav: 123456.78, available: 50000.5, frozen: 0, market_value: 73456.28 },
}))

vi.mock('../../../api/gm', () => ({
  getAsset: vi.fn().mockResolvedValue(mockAsset),
}))

import AssetCard from '../AssetCard.vue'

const mountCard = () => mount(AssetCard, { global: { plugins: [ElementPlus] } })

describe('AssetCard.vue', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.clearAllMocks()
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('onMounted 拉资金并渲染整数化的总权益/可用资金', async () => {
    const w = mountCard()
    await flushPromises()
    expect(w.text()).toContain('123457')
    expect(w.text()).toContain('50001')
  })

  it('字段缺失（掘金 cash 返空对象）时显示「—」而非 0', async () => {
    const { getAsset } = await import('../../../api/gm')
    ;(getAsset as any).mockResolvedValueOnce({})
    const w = mountCard()
    await flushPromises()
    const dashCount = (w.text().match(/—/g) || []).length
    expect(dashCount).toBeGreaterThanOrEqual(2)
  })

  it('5s 轮询再次调用 getAsset，卸载后停止', async () => {
    const { getAsset } = await import('../../../api/gm')
    const w = mountCard()
    await flushPromises()
    expect(getAsset).toHaveBeenCalledTimes(1)
    vi.advanceTimersByTime(5000)
    await flushPromises()
    expect(getAsset).toHaveBeenCalledTimes(2)
    w.unmount()
    vi.advanceTimersByTime(10000)
    await flushPromises()
    expect(getAsset).toHaveBeenCalledTimes(2)
  })
})
