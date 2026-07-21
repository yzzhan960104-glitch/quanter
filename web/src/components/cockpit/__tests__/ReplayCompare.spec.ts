/**
 * ReplayCompare 历史回测对比组件单测（Task 11 · 严格 TDD）。
 *
 * 物理意图：验证组件 onMounted → listReplayTasks 拉历史 SUCCESS 任务 → el-table 多选 →
 * onSelect 触发 getReplayTask 取详情 → 对比统计表（win_rate/max_drawdown/annualized_return）
 * 这条链路在 jsdom 下能完整跑通。
 *
 * Why mock listReplayTasks/getReplayTask：组件 setup 即拉任务列表，真实 fetch 在 jsdom 下
 * 无网络/无后端，必须用 vi.mock 替换 api/caisen 模块的两个函数，返回固定结构。
 *
 * Why vi.hoisted：vi.mock 工厂被 vitest 提升到文件顶部执行，普通顶层 const 在工厂内引用
 * 会触发 TDZ（Cannot access before initialization）。vi.hoisted 让夹具与 mock 一起提升，
 * 保证工厂执行时夹具已就绪。
 *
 * Why 顶部 polyfill：jsdom 不实现 ResizeObserver/matchMedia，EP el-table 依赖它们做定位/
 * 响应式测量，不补会在 mount 时抛 TypeError（抄 TradesTable.spec.ts 的装配骨架）。
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import ReplayCompare from '../ReplayCompare.vue'

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

// 固定任务列表：两条 SUCCESS 任务 + 一条非 SUCCESS（PENDING，应被组件过滤掉，验证过滤逻辑）。
// 固定详情：SUCCESS 时内嵌 report（含 win_rate/max_drawdown/annualized_return/equity_curve）。
const { mockTasks, mockDetail } = vi.hoisted(() => ({
  mockTasks: [
    { task_id: 't1-success-aaa', created_at: '2026-07-21', status: 'SUCCESS', progress: 100 },
    { task_id: 't2-success-bbb', created_at: '2026-07-20', status: 'SUCCESS', progress: 100 },
    // 非 SUCCESS：组件 loadList 内过滤，不应出现在渲染结果中。
    { task_id: 't3-pending-ccc', created_at: '2026-07-19', status: 'PENDING', progress: 10 },
  ],
  mockDetail: {
    task_id: 't1-success-aaa',
    created_at: '2026-07-21',
    status: 'SUCCESS',
    progress: 100,
    report: {
      n_hits: 10,
      win_rate: 0.55,
      avg_rr: 1.2,
      max_drawdown: -0.1,
      pattern_dist: {},
      monthly_returns: {},
      avg_holding_bars: 5,
      min_rr_ratio_recommendation: '建议 ≥ 1.5',
      equity_curve: [{ date: 'd1', cumulative_rr: 0.1, equity: 1.1 }],
      trades: [],
      annualized_return: 0.3,
      n_trading_days: 252,
    },
  },
}))

vi.mock('../../../api/caisen', () => ({
  listReplayTasks: vi.fn().mockResolvedValue(mockTasks),
  getReplayTask: vi.fn().mockResolvedValue(mockDetail),
}))

const mountCompare = () => mount(ReplayCompare, { global: { plugins: [ElementPlus] } })

describe('ReplayCompare.vue', () => {
  beforeEach(() => {
    // clearAllMocks 仅清调用计数/调用记录，不动 mockResolvedValue 的实现，
    // 故两个 mock 函数仍按工厂里设的默认值返回，无需在每个 case 重设。
    vi.clearAllMocks()
  })

  it('onMounted 拉历史 SUCCESS 任务并渲染到任务表', async () => {
    const w = mountCompare()
    await flushPromises()
    // 两条 SUCCESS 任务应出现。
    expect(w.text()).toContain('t1-success-aaa')
    expect(w.text()).toContain('t2-success-bbb')
    // 非 SUCCESS 的 PENDING 任务应被过滤掉（loadList 内 filter）。
    expect(w.text()).not.toContain('t3-pending-ccc')
  })

  it('刷新按钮重新拉取任务列表', async () => {
    const { listReplayTasks } = await import('../../../api/caisen')
    const w = mountCompare()
    await flushPromises()
    // onMounted 已调用一次 listReplayTasks。
    expect(listReplayTasks).toHaveBeenCalledTimes(1)
    // 点击「刷新」按钮应再次拉取。
    const refreshBtn = w.findAllComponents({ name: 'ElButton' }).find((b) => b.text().includes('刷新'))
    expect(refreshBtn).toBeTruthy()
    await refreshBtn!.trigger('click')
    await flushPromises()
    expect(listReplayTasks).toHaveBeenCalledTimes(2)
  })

  it('勾选任务后取详情并渲染对比统计表', async () => {
    const w = mountCompare()
    await flushPromises()
    // 模拟 el-table 多选事件：组件用 @selection-change="onSelect" 绑定。
    // 直接触发组件 vm 上的 onSelect（el-table 在 jsdom 下勾选行为不真实）。
    const vm = w.vm as any
    vm.onSelect([
      { task_id: 't1-success-aaa', created_at: '2026-07-21', status: 'SUCCESS', progress: 100 },
    ])
    // onSelect 内串行 await getReplayTask，flushPromises 等微任务走完。
    await flushPromises()
    await flushPromises()
    // 对比统计表应含 win_rate=55.0% / max_drawdown=-10.0% / annualized_return=30.0%。
    expect(w.text()).toContain('55.0%')
    expect(w.text()).toContain('-10.0%')
    expect(w.text()).toContain('30.0%')
  })
})
