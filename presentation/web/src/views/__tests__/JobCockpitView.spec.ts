/**
 * JobCockpitView 作业驾驶舱单测（Task 12 · Phase 2 收官）。
 *
 * 物理意图：验证纯观测视图的 4 个核心语义：
 *   ① 渲染 job 表 + gate 拒因 + skipped 状态标签（说明列最有价值的可读字段）
 *   ② catchup=running → 显运行态 + 3s 快轮询触发新请求（自适应节奏）
 *   ③ catchup=done → 展示 result 子任务行（启动补跑结果可观测）
 *   ④ unmount 后定时器已清（防泄漏，与 LiveCockpitView 同纪律）
 *
 * 策略（Why vi.hoisted + vi.mock）：
 *   vi.mock 是 hoist 提升的，工厂内引用的 mock 句柄必须用 vi.hoisted 包裹才能保序，
 *   否则会出现「Cannot access 'getJobsMock' before initialization」运行期错误
 *   （同仓 caisen.spec.ts 的同款坑）。这里严格采用 vi.hoisted 模式。
 *
 * Why polyfill：EP el-table/el-tag 在 jsdom 下走响应式测量，不补
 * ResizeObserver/matchMedia 会抛错；与 CockpitView.spec.ts 同范式。
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import JobCockpitView from '../JobCockpitView.vue'

// vi.hoisted 保证 mock 句柄在 vi.mock 工厂执行时已初始化（hoist 提升后序）。
const getJobsMock = vi.hoisted(() => vi.fn())
vi.mock('@/api/trading', () => ({ getJobs: getJobsMock }))

// ---- jsdom 缺失 API 的最小 polyfill（同 CockpitView.spec 范式） ----
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

// 默认 snapshot：pre_open skipped + gate 拒因（最典型的可读验证形态）
function mockSnapshot(overrides: Record<string, unknown> = {}) {
  return {
    date: '2026-08-02',
    jobs: [
      {
        name: 'pre_open',
        status: 'skipped',
        started_at: 't1',
        finished_at: 't2',
        message: 'gate3 未过：data_ready 未就绪',
      },
    ],
    catchup: { state: 'not_started', result: null },
    ...overrides,
  }
}

beforeEach(() => {
  vi.useFakeTimers()
  getJobsMock.mockReset()
})
afterEach(() => {
  vi.useRealTimers()
})

describe('JobCockpitView', () => {
  it('渲染 job 表 + gate 拒因 + skipped 标签', async () => {
    getJobsMock.mockResolvedValue(mockSnapshot())
    const w = mount(JobCockpitView, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    expect(w.text()).toContain('pre_open')
    expect(w.text()).toContain('gate3 未过：data_ready 未就绪')
    expect(w.text()).toContain('跳过')
  })

  it('catchup=running → 显运行态 + 3s 快轮询触发新请求', async () => {
    getJobsMock.mockResolvedValue(
      mockSnapshot({ catchup: { state: 'running', result: null } }),
    )
    mount(JobCockpitView, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    const n = getJobsMock.mock.calls.length
    await vi.advanceTimersByTimeAsync(3000)
    expect(getJobsMock.mock.calls.length).toBeGreaterThan(n)
  })

  it('catchup=done → 展示 result 子任务行', async () => {
    getJobsMock.mockResolvedValue(
      mockSnapshot({
        catchup: { state: 'done', result: { pipeline: true, error: null } },
      }),
    )
    const w = mount(JobCockpitView, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    expect(w.text()).toContain('pipeline: true')
  })

  it('unmount 后定时器已清（60s 内无新请求）', async () => {
    getJobsMock.mockResolvedValue(mockSnapshot())
    const w = mount(JobCockpitView, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    const n = getJobsMock.mock.calls.length
    w.unmount()
    await vi.advanceTimersByTimeAsync(60000)
    expect(getJobsMock.mock.calls.length).toBe(n)
  })
})
