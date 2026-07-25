/**
 * AssetCard 资金小部件单测（Task 12 · 一期观测运营层）。
 *
 * 物理意图：验证组件 onMounted → getAsset 拉资金 → 渲染总资产/可用资金（.toFixed(0) 整数）。
 *
 * 边界 case：
 *   - 字段为 0（未连网关）→ 显示「—」而非 0（杜绝虚假归零误导）。
 *   - 字段为非零数值 → 显示 toFixed(0) 整数。
 *
 * Why vi.hoisted + vi.mock：组件 setup 即拉 /trading/asset，jsdom 下必须替换 getAsset。
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

// 固定资产响应：总资产 123456.78、可用资金 50000.5 → 期望渲染 toFixed(0) 整数「123457」「50001」。
const { mockAsset } = vi.hoisted(() => ({
  mockAsset: { asset: { cash: 50000.5, total_asset: 123456.78, market_value: 73456.28 } },
}))

vi.mock('../../../api/trading', () => ({
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

  it('onMounted 拉资金并渲染整数化的总资产/可用资金', async () => {
    const w = mountCard()
    await flushPromises()
    // toFixed(0) 四舍六入：123456.78 → 123457；50000.5 → 50001（JS Math.round 半数向上）。
    expect(w.text()).toContain('123457')
    expect(w.text()).toContain('50001')
  })

  it('未连接（cash/total_asset 为 0）时显示「—」而非 0', async () => {
    // 动态切 mock 返回零值（模拟未连网关后端返回空字段）。
    const { getAsset } = await import('../../../api/trading')
    ;(getAsset as any).mockResolvedValueOnce({ asset: { cash: 0, total_asset: 0, market_value: 0 } })
    const w = mountCard()
    await flushPromises()
    // 零值应显示「—」两个（总资产 / 可用资金 各一个）。
    const dashCount = (w.text().match(/—/g) || []).length
    expect(dashCount).toBeGreaterThanOrEqual(2)
  })

  it('5s 轮询再次调用 getAsset，卸载后停止', async () => {
    const { getAsset } = await import('../../../api/trading')
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
