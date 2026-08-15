/**
 * TerminalLogs 实时日志组件单测（Task 10 · 严格 TDD）。
 *
 * 物理意图：验证组件 onMounted → POST /api/v1/auth/read-cookie 换只读 cookie
 * → new EventSource('/api/v1/logs/stream') 订阅 SSE → message 事件回调把
 * e.data 追加到 lines → 模板按 ERROR/WARN 着色 → onUnmounted close() 整条
 * 链路在 jsdom 下能跑通。
 *
 * Why mock EventSource：jsdom 不实现 EventSource，组件 setup 同步 new EventSource
 * 会抛 ReferenceError；用一个最小 mock 捕获 addEventListener 的回调，测试侧
 * 手动派发 message 事件即可驱动组件状态机。
 *
 * Why mock @/api/client（N5 · Low ⑨）：onMounted 先经 axios POST 换 cookie——
 * 真实 axios 在 jsdom 下发网络请求（404/超时噪声且拖慢）；组件只依赖「post 的
 * promise 落定」这一时序，不消费响应体，最小 mock post 即可（discovery.spec 同款）。
 *
 * Why vi.hoisted：vi.mock/globalThis 赋值的工厂被提升到顶部，普通顶层 const 在
 * 其内部引用会触发 TDZ（Cannot access before initialization）。这里 MockES 不
 * 涉及 vi.mock 工厂闭包，但沿用 TradesTable.spec.ts 的装配骨架统一风格。
 *
 * Why flushPromises：onMounted 前半段 await apiClient.post（异步）——mount 返回
 * 时 post 已发出但 EventSource 尚未建立，flush 一次微任务让 await 落定、钩子
 * 跑完，再断言 _es 已建立（时序契约由 read-cookie 先行用例单独钉死）。
 *
 * Why 顶部 polyfill：EP el-card/el-button 在 jsdom 下依赖 ResizeObserver/
 * matchMedia 做响应式测量，不补会在 mount 时抛 TypeError（与 TradesTable 同根）。
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import TerminalLogs from '../TerminalLogs.vue'

// ---- apiClient 最小 mock（N5 · Low ⑨）：只 mock 组件消费的 post 通道 ----
// 默认 resolve（dry_run 无 token 的 200 形态）；失败容错用例用 mockRejectedValueOnce
// 逐次覆写。组件不读响应体，只吃「promise 落定」时序。
vi.mock('@/api/client', () => ({
  apiClient: { post: vi.fn().mockResolvedValue({ ok: true }) },
}))

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

  it('SSE 订阅前先 POST /api/v1/auth/read-cookie（live cookie 换取时序）', async () => {
    // N5 · Low ⑨：EventSource 无法带 Authorization 头——必须先经 axios 换 HttpOnly
    // cookie，SSE 同源自动携带。时序红线：post 未落定前不得建 EventSource（否则 live
    // 配 token 时首个 SSE 请求裸奔 401）。
    const { apiClient } = await import('@/api/client')
    const w = mountLogs()
    // mount 同步段：post 已发出、await 未落定 → EventSource 必然尚未建立。
    expect(apiClient.post).toHaveBeenCalledWith('/api/v1/auth/read-cookie')
    expect(MockES.last).toBeNull()
    await flushPromises()
    expect(MockES.last).toBeTruthy()
    expect(MockES.last!.url).toBe('/api/v1/logs/stream')
    w.unmount()
  })

  it('read-cookie 失败吞错照常直连（dry_run/离线容错）', async () => {
    // 换 cookie 是「尽力而为」的前置增强而非面板可用性前提：后端不在/token 失配时
    // post reject，组件不得抛错、不得阻断 SSE 建立（直连交服务端裁决）。
    const { apiClient } = await import('@/api/client')
    vi.mocked(apiClient.post).mockRejectedValueOnce(new Error('offline'))
    const w = mountLogs()
    await flushPromises()
    expect(MockES.last).toBeTruthy()
    expect(MockES.last!.url).toBe('/api/v1/logs/stream')
    w.unmount()
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
