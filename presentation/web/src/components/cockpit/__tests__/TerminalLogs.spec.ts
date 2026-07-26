/**
 * TerminalLogs 实时日志组件单测（Task 10 · 严格 TDD）。
 *
 * 物理意图：验证组件 onMounted → new EventSource('/api/v1/logs/stream') 订阅
 * SSE → message 事件回调把 e.data 追加到 lines → 模板按 ERROR/WARN 着色
 * → onUnmounted close() 整条链路在 jsdom 下能跑通。
 *
 * Why mock EventSource：jsdom 不实现 EventSource，组件 setup 同步 new EventSource
 * 会抛 ReferenceError；用一个最小 mock 捕获 addEventListener 的回调，测试侧
 * 手动派发 message 事件即可驱动组件状态机。
 *
 * Why vi.hoisted：vi.mock/globalThis 赋值的工厂被提升到顶部，普通顶层 const 在
 * 其内部引用会触发 TDZ（Cannot access before initialization）。这里 MockES 不
 * 涉及 vi.mock 工厂闭包，但沿用 TradesTable.spec.ts 的装配骨架统一风格。
 *
 * Why flushPromises：组件 onMounted 是同步的（new EventSource 即时返回），
 * 但 vue/test-utils mount 后需 flush 一次微任务让 setup/onMounted 钩子完整
 * 跑完，再断言 _es 已建立。
 *
 * Why 顶部 polyfill：EP el-card/el-button 在 jsdom 下依赖 ResizeObserver/
 * matchMedia 做响应式测量，不补会在 mount 时抛 TypeError（与 TradesTable 同根）。
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import TerminalLogs from '../TerminalLogs.vue'

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

// ---- EventSource 最小 mock：捕获 message/error 监听器，测试侧手动派发 ----
// 真实 EventSource 会自动连接 URL 并在收到 SSE data: 行时触发 message 事件；
// 这里仅保存回调，断言时手动调用以模拟服务端推流。
class MockES {
  static last: MockES | null = null
  url: string
  listeners: Record<string, ((ev: any) => void) | undefined> = {}
  closed = false
  constructor(url: string) {
    this.url = url
    MockES.last = this
  }
  addEventListener(ev: string, fn: (ev: any) => void) {
    this.listeners[ev] = fn
  }
  removeEventListener(ev: string) {
    delete this.listeners[ev]
  }
  close() {
    this.closed = true
  }
}

const mountLogs = () => mount(TerminalLogs, { global: { plugins: [ElementPlus] } })

describe('TerminalLogs.vue', () => {
  beforeEach(() => {
    MockES.last = null
    ;(globalThis as any).EventSource = MockES
  })
  afterEach(() => {
    vi.clearAllMocks()
  })

  it('订阅 SSE 并追加日志行（brief Step 1 主路径）', async () => {
    const w = mountLogs()
    await flushPromises()
    const es = MockES.last!
    expect(es).toBeTruthy()
    expect(es.url).toBe('/api/v1/logs/stream')
    // 模拟服务端推一条日志。
    es.listeners['message']!({ data: '2026-07-21 10:00:00 INFO test log' })
    await flushPromises()
    // 通过 defineExpose 暴露的 lines 断言（与 brief Step 1 一致）。
    expect((w.vm as any).lines.some((l: string) => l.includes('test log'))).toBe(true)
    // 模板也应渲染该行文本。
    expect(w.text()).toContain('test log')
  })

  it('ERROR 行挂 lvl-error 类、WARN 行挂 lvl-warn 类', async () => {
    const w = mountLogs()
    await flushPromises()
    const es = MockES.last!
    es.listeners['message']!({ data: '2026-07-21 10:01:00 ERROR boom' })
    es.listeners['message']!({ data: '2026-07-21 10:02:00 WARN shaky' })
    es.listeners['message']!({ data: '2026-07-21 10:03:00 INFO ok' })
    await flushPromises()
    const pres = w.findAll('pre')
    expect(pres.length).toBe(3)
    expect(pres[0].classes()).toContain('lvl-error')
    expect(pres[1].classes()).toContain('lvl-warn')
    // INFO 行 levelClass 返回 ''，Vue 视作无 class 绑定，classes() 返回空数组。
    expect(pres[2].classes()).toEqual([])
  })

  it('环缓冲上限 MAX=500：超过则丢弃最旧行', async () => {
    const w = mountLogs()
    await flushPromises()
    const es = MockES.last!
    // 灌入 502 条，保留期望：长度=500，最旧一条被 shift，最新一条在尾部。
    for (let i = 0; i < 502; i++) {
      es.listeners['message']!({ data: `line-${i}` })
    }
    await flushPromises()
    const lines: string[] = (w.vm as any).lines
    expect(lines.length).toBe(500)
    expect(lines[0]).toBe('line-2') // line-0/1 已被 shift
    expect(lines[lines.length - 1]).toBe('line-501')
  })

  it('暂停状态下不追加新日志', async () => {
    const w = mountLogs()
    await flushPromises()
    const es = MockES.last!
    es.listeners['message']!({ data: 'before-pause' })
    await flushPromises()
    // 点击「暂停」按钮切换 paused。
    await w.findAll('button').find((b) => b.text().includes('暂停'))!.trigger('click')
    es.listeners['message']!({ data: 'after-pause' })
    await flushPromises()
    const text = w.text()
    expect(text).toContain('before-pause')
    expect(text).not.toContain('after-pause')
    // 按钮文本已切换为「继续」。
    expect(text).toContain('继续')
  })

  it('onUnmounted 关闭 EventSource', async () => {
    const w = mountLogs()
    await flushPromises()
    const es = MockES.last!
    expect(es.closed).toBe(false)
    w.unmount()
    expect(es.closed).toBe(true)
  })
})
