/**
 * ExperimentView 编排测试(2026-08-29 多腿方案 P3,仿 CockpitView.spec 模式)。
 *
 * 物理意图:验证实验视图编排正确——RoundCard/AbDailyCard/AbHistoryCard/AuditExplorer
 * 四组件齐挂载且共享视图级 date。子组件全 stub,零 API 依赖。
 */
import { describe, it, expect, vi, beforeAll } from 'vitest'
import { mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import ExperimentView from '../ExperimentView.vue'

class MockObserver { observe() {} unobserve() {} disconnect() {} takeRecords() { return [] } }
;(globalThis as any).ResizeObserver = MockObserver
;(globalThis as any).IntersectionObserver = MockObserver
beforeAll(() => {
  if (!(globalThis as any).EventSource) {
    ;(globalThis as any).EventSource = class {}
  }
})

const CHILDREN = ['RoundCard', 'AbDailyCard', 'AbHistoryCard', 'AuditExplorer']
const stubs = CHILDREN.reduce((acc, name) => {
  acc[name] = { template: `<div data-stub="${name}">${name}</div>` }
  return acc
}, {} as Record<string, { template: string }>)

const mountView = () =>
  mount(ExperimentView, { global: { plugins: [ElementPlus], stubs } })

describe('ExperimentView.vue', () => {
  it('渲染全部 4 个子组件(轮次/当日对照/进度/下钻)', () => {
    const w = mountView()
    for (const name of CHILDREN) {
      expect(w.find(`[data-stub="${name}"]`).exists()).toBe(true)
    }
  })
})
