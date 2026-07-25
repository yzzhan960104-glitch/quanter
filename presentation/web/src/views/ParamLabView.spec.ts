import { describe, it, expect, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import ParamLabView from './ParamLabView.vue'

// mock @/api/caisen：避免真发请求；listReplayTasks 返空 → 触发空态分支
vi.mock('@/api/caisen', () => ({
  getConfigSchema: vi.fn().mockResolvedValue({ properties: { min_rr_ratio: { type: 'number', default: 1.5 } } }),
  listReplayTasks: vi.fn().mockResolvedValue([]),
  getReplayTask: vi.fn().mockResolvedValue(null),
  submitReplayAsync: vi.fn(),
  cancelReplayTask: vi.fn(),
  deleteReplayTask: vi.fn(),
}))

// jsdom 缺失的浏览器 API 桩（ECharts/vue-echarts 依赖 ResizeObserver）
class MockObserver { observe() {} unobserve() {} disconnect() {} takeRecords() { return [] } }
;(globalThis as any).ResizeObserver = MockObserver
;(globalThis as any).IntersectionObserver = MockObserver
;(globalThis as any).matchMedia = (globalThis as any).matchMedia || ((q: string) => ({
  matches: false, media: q, onchange: null, addListener: vi.fn(), removeListener: vi.fn(),
  addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
}))

describe('ParamLabView', () => {
  it('渲染 4 区 + 新建回测按钮 + 空态提示（无任务时）', async () => {
    const wrapper = mount(ParamLabView, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    expect(wrapper.text()).toContain('参数详情')
    expect(wrapper.text()).toContain('收益率走势')
    expect(wrapper.text()).toContain('买卖日志')
    expect(wrapper.text()).toContain('任务列表')
    expect(wrapper.text()).toContain('新建回测')
    // 空态
    expect(wrapper.text()).toContain('点 ＋新建回测')
  })
})
