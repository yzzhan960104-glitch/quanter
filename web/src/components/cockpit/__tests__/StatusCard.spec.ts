/**
 * StatusCard 心跳小部件单测（Task 12 · 一期观测运营层）。
 *
 * 物理意图：验证组件 onMounted → getStatus 拉心跳 → 按四态映射渲染中文标签 + mode 字面量。
 *
 * Why mock getStatus：组件 setup 即发心跳请求，jsdom 下无后端必须 vi.mock 替换。
 *
 * Why fake timers + clearInterval 守卫：组件 setInterval(fetchStatus, 2000) 在测试结束后
 * 仍会跑，vi.useFakeTimers + wrapper.unmount 后 clearInterval 才能保证不会污染后续用例。
 *
 * Why polyfill：同 TradesTable.spec.ts，EP el-card 在 jsdom 下可能依赖 matchMedia 等兜底。
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import ElementPlus from 'element-plus'

// ---- jsdom 缺失 API 的最小 polyfill（满足 EP 不抛，不模拟真实行为） ----
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

// 固定心跳响应：mode=live（验证「已连接」标签 + 绿色点分支）。
// Why vi.hoisted：vi.mock 工厂提升到文件顶部，普通顶层 const 引用会触发 TDZ。
const { mockStatus } = vi.hoisted(() => ({
  mockStatus: { connected: true, locked: false, mode: 'live' },
}))

vi.mock('../../../api/trading', () => ({
  getStatus: vi.fn().mockResolvedValue(mockStatus),
}))

// 延迟 import：vi.mock 必须先注册，import 才能拿到替换后的模块。
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

  it('onMounted 拉 status 并渲染 live 态中文标签 + mode', async () => {
    const w = mountCard()
    // 推进 onMounted 内 fetchStatus 的微任务。
    await flushPromises()
    // 心跳态 mode=live → 标签「已连接」 + mode 字面量 live。
    expect(w.text()).toContain('已连接')
    expect(w.text()).toContain('mode=live')
  })

  it('每 2s 轮询再次调用 getStatus', async () => {
    const { getStatus } = await import('../../../api/trading')
    const w = mountCard()
    await flushPromises()
    // onMounted 已调用 1 次。
    expect(getStatus).toHaveBeenCalledTimes(1)
    // 推进 2s，setInterval 应触发第二次 fetchStatus。
    vi.advanceTimersByTime(2000)
    await flushPromises()
    expect(getStatus).toHaveBeenCalledTimes(2)
    // 卸载后定时器应被清掉，再推进不应再触发。
    w.unmount()
    vi.advanceTimersByTime(4000)
    await flushPromises()
    expect(getStatus).toHaveBeenCalledTimes(2)
  })
})
