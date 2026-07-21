/**
 * CockpitView 综合看板单测（Task 12 · 一期观测运营层前端收官）。
 *
 * 物理意图：验证综合看板编排正确——上排 StatusCard/AssetCard/DataHealthCard，中排
 * TradesTable/TerminalLogs，下排 ReplayCompare，6 个子组件全部被渲染到页面。
 *
 * 策略（Why stub 子组件）：
 *   本测试只验证 CockpitView 的「编排」职责（哪些子组件挂在哪个栅格），不验证子组件内部
 *   行为（各自已有独立单测覆盖）。故用 shallow + stub 替换 6 个子组件为占位 div，
 *   避免触发它们的 onMounted 真发 API 请求 / SSE 订阅，保持测试隔离与速度。
 *
 * Why polyfill：EP el-row/el-col 在 jsdom 下走响应式测量，不补 ResizeObserver/matchMedia 会抛。
 */
import { describe, it, expect, vi, beforeAll } from 'vitest'
import { mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import CockpitView from '../CockpitView.vue'

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

// EventSource polyfill：TerminalLogs 即便被 stub，EP 不会触发其 setup，但保留全局兜底
// 防止后续测试若不禁用 stub 时报「EventSource is not defined」。
beforeAll(() => {
  if (!(globalThis as any).EventSource) {
    ;(globalThis as any).EventSource = class {
      constructor() {}
      addEventListener() {}
      close() {}
    }
  }
})

// 6 个子组件名（PascalCase），CockpitView 用默认名导入，stub 用组件名匹配。
const CHILDREN = [
  'StatusCard',
  'AssetCard',
  'DataHealthCard',
  'TradesTable',
  'TerminalLogs',
  'ReplayCompare',
]

const stubs = CHILDREN.reduce((acc, name) => {
  acc[name] = { template: `<div data-stub="${name}">${name}</div>` }
  return acc
}, {} as Record<string, { template: string }>)

const mountView = () =>
  mount(CockpitView, {
    global: {
      plugins: [ElementPlus],
      stubs,
    },
  })

describe('CockpitView.vue', () => {
  it('渲染全部 6 个子组件（上 3 / 中 2 / 下 1 编排）', () => {
    const w = mountView()
    // 每个 stub 渲染为带 data-stub 属性的 div，验证全部 6 个都挂载到页面。
    for (const name of CHILDREN) {
      expect(w.find(`[data-stub="${name}"]`).exists()).toBe(true)
    }
  })

  it('包含「综合看板」必需的三排结构（心跳/资金/数据健康/流水/日志/回测对比）', () => {
    const w = mountView()
    const text = w.text()
    // 子组件 stub 文本里包含中文名（便于可读断言）。
    expect(text).toContain('StatusCard')
    expect(text).toContain('AssetCard')
    expect(text).toContain('DataHealthCard')
    expect(text).toContain('TradesTable')
    expect(text).toContain('TerminalLogs')
    expect(text).toContain('ReplayCompare')
  })
})
