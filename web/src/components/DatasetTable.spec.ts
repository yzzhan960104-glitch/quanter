/**
 * DatasetTable 组件渲染单测（第 3 项「前端组件/单测」层 · 组件测试范例）。
 *
 * 物理意图：验证 @vue/test-utils + jsdom + Element Plus 全量注册的装配链路通——
 * 给定 props.datasets 断言「每行渲染同步按钮 / 点击 emit 正确 key / 空态 empty-text /
 * 状态徽章中文」。
 *
 * Why flushPromises：EP el-table 接收 :data 后异步经 store.commit('setData') 渲染行，
 * 同步 mount 立即 findAll 拿不到行内按钮；await flushPromises 等微任务走完才断言。
 *
 * Why 顶部 polyfill：jsdom 不实现 ResizeObserver/IntersectionObserver/matchMedia，EP 的
 * el-table/el-tooltip 依赖它们做定位/响应式测量，不补会在 mount 时抛 TypeError。
 */
import { describe, it, expect, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import DatasetTable from './DatasetTable.vue'
import type { DatasetAsset } from '@/api/data'

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

// ---- 测试夹具（字段对齐 @/api/data 的 DatasetAsset，含 schedule 等全部必填项）----
const healthy: DatasetAsset = {
  key: 'daily',
  name: '日线行情',
  source: 'jqdata',
  market: 'A',
  granularity: '1d',
  schedule: '0 18 * * 1-5',
  status: 'healthy',
  data_start: '2020-01-01',
  data_end: '2026-01-01',
  latest_sync: '2026-01-01 18:00',
  last_error: null,
}
const failed: DatasetAsset = {
  key: 'macro',
  name: '宏观曲线',
  source: 'akshare',
  market: 'CN',
  granularity: '1d',
  schedule: '0 19 * * 1-5',
  status: 'failed',
  data_start: null,
  data_end: null,
  latest_sync: null,
  last_error: '同步异常：连接超时',
}

const mountWith = (datasets: DatasetAsset[]) =>
  mount(DatasetTable, {
    props: { datasets },
    global: { plugins: [ElementPlus] },
  })

describe('DatasetTable 组件渲染', () => {
  it('每条数据集渲染一个「立即同步」按钮（2 条数据集 → 2 个按钮）', async () => {
    const wrapper = mountWith([healthy, failed])
    await flushPromises()
    const syncBtns = wrapper.findAll('button').filter((b) => b.text().includes('立即同步'))
    expect(syncBtns.length).toBe(2)
  })

  it('点击「立即同步」emit sync 事件，携带该行 key', async () => {
    const wrapper = mountWith([healthy, failed])
    await flushPromises()
    const syncBtns = wrapper.findAll('button').filter((b) => b.text().includes('立即同步'))
    await syncBtns[0].trigger('click')
    expect(wrapper.emitted('sync')).toBeTruthy()
    expect(wrapper.emitted('sync')![0]).toEqual(['daily'])
  })

  it('空数据集时渲染 empty-text「暂无数据集」', () => {
    const wrapper = mountWith([])
    expect(wrapper.text()).toContain('暂无数据集')
  })

  it('failed 态渲染中文状态徽章「失败」', async () => {
    const wrapper = mountWith([failed])
    await flushPromises()
    expect(wrapper.text()).toContain('失败')
  })
})
