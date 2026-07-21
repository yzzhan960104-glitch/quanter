/**
 * DataHealthCard 数据健康小部件单测（Task 12 · 一期观测运营层）。
 *
 * 物理意图：验证组件 onMounted → getDatasets 拉数据集 → reduce 统计 healthy/total/健康率。
 *
 * 边界 case：
 *   - 含 stale/missing/failed 数据集 → healthy 计数严格只算 status='healthy'。
 *   - 健康率 < 100% → rate-warn class 触发（标黄提示）。
 *   - total=0 → 健康率显示 0% 而非 NaN（除零守护）。
 *
 * Why vi.hoisted + vi.mock：组件 setup 即拉 /data/datasets，jsdom 下必须替换 getDatasets。
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
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

// 固定数据集列表：5 条中 3 healthy / 1 stale / 1 missing → healthy=3, total=5, 健康率 60%。
// 只填 status 字段（其他字段组件不读），保持夹具最小化。
const { mockDatasets } = vi.hoisted(() => ({
  mockDatasets: [
    { status: 'healthy' },
    { status: 'healthy' },
    { status: 'healthy' },
    { status: 'stale' },
    { status: 'missing' },
  ],
}))

vi.mock('../../../api/data', () => ({
  getDatasets: vi.fn().mockResolvedValue(mockDatasets),
}))

import DataHealthCard from '../DataHealthCard.vue'

const mountCard = () => mount(DataHealthCard, { global: { plugins: [ElementPlus] } })

describe('DataHealthCard.vue', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('onMounted 拉数据集并渲染 healthy/total/健康率', async () => {
    const w = mountCard()
    await flushPromises()
    // healthy=3, total=5 → 「3/5」 + 「60%」。
    expect(w.text()).toContain('3/5')
    expect(w.text()).toContain('60%')
  })

  it('健康率 < 100% 时 rate-warn 类生效（标黄提示）', async () => {
    const w = mountCard()
    await flushPromises()
    // 健康率值元素应带 rate-warn class。
    expect(w.find('.rate-warn').exists()).toBe(true)
  })

  it('刷新按钮重新拉取数据集', async () => {
    const { getDatasets } = await import('../../../api/data')
    const w = mountCard()
    await flushPromises()
    expect(getDatasets).toHaveBeenCalledTimes(1)
    const refreshBtn = w.findAllComponents({ name: 'ElButton' }).find((b) => b.text().includes('刷新'))
    expect(refreshBtn).toBeTruthy()
    await refreshBtn!.trigger('click')
    await flushPromises()
    expect(getDatasets).toHaveBeenCalledTimes(2)
  })

  it('total=0 时健康率为 0%（除零守护，非 NaN）', async () => {
    const { getDatasets } = await import('../../../api/data')
    ;(getDatasets as any).mockResolvedValueOnce([])
    const w = mountCard()
    await flushPromises()
    // 空列表 → healthy=0, total=0 → 「0/0」 + 「0%」（不应出现 NaN）。
    expect(w.text()).toContain('0/0')
    expect(w.text()).toContain('0%')
    expect(w.text()).not.toContain('NaN')
  })
})
