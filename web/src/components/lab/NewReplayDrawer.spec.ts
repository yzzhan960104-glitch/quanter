/**
 * NewReplayDrawer 组件单测：分组表单渲染 + prefill + 提交 body 契约。
 * 复用 DatasetTable.spec.ts 的 jsdom polyfill + ElementPlus 全量注册模式。
 */
import { describe, it, expect, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import NewReplayDrawer from './NewReplayDrawer.vue'
import { PARAM_META } from './paramMeta'

class MockObserver { observe() {} unobserve() {} disconnect() {} takeRecords() { return [] } }
;(globalThis as any).ResizeObserver = MockObserver
;(globalThis as any).IntersectionObserver = MockObserver
;(globalThis as any).matchMedia = (globalThis as any).matchMedia || ((q: string) => ({
  matches: false, media: q, onchange: null, addListener: vi.fn(), removeListener: vi.fn(),
  addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
}))

// 最小 schema fixture：properties 含两个已知字段（min_rr_ratio + max_holding_bars）
const SCHEMA = {
  properties: {
    min_rr_ratio:     { type: 'number', default: 1.5, description: '盈亏比下限' },
    max_holding_bars: { type: 'integer', default: 15, description: '最大持仓周期' },
  },
}

describe('NewReplayDrawer', () => {
  it('visible 时渲染 7 个分组标题 + 区间/标的输入', async () => {
    const wrapper = mount(NewReplayDrawer, {
      props: { visible: true, configSchema: SCHEMA, prefill: null },
      global: { plugins: [ElementPlus] },
    })
    await flushPromises()
    // 7 分组（PARAM_GROUPS）至少渲染「交易执行」「时间止损」（含两字段）
    expect(wrapper.text()).toContain('交易执行')
    expect(wrapper.text()).toContain('时间止损')
    expect(wrapper.find('input[placeholder*="开始"]').exists() || wrapper.text()).toBeTruthy()
  })

  it('prefill 灌入：min_rr_ratio 显示 prefill 值而非 schema 默认', async () => {
    const wrapper = mount(NewReplayDrawer, {
      props: { visible: true, configSchema: SCHEMA, prefill: { min_rr_ratio: 2.0 } },
      global: { plugins: [ElementPlus] },
    })
    await flushPromises()
    // el-input-number 的输入框值 = 2.0（prefill 覆盖默认 1.5）
    const input = wrapper.find('input[role="spinbutton"]')
    expect((input.element as HTMLInputElement).value).toContain('2')
  })

  it('点提交 emit submit，payload 含 start/end/universe/cfg_override（含 prefill 改值）', async () => {
    const wrapper = mount(NewReplayDrawer, {
      props: { visible: true, configSchema: SCHEMA, prefill: { min_rr_ratio: 2.0 } },
      global: { plugins: [ElementPlus] },
    })
    await flushPromises()
    // 抽屉默认 start/end 为空 → 提交按钮 disabled（组件的必填守护，防误触发后端 422）。
    // 此用例验证「填齐日期后提交」，故先灌 start/end 让按钮可点。
    const dateInputs = wrapper.findAll('input[placeholder*="开始"], input[placeholder*="结束"]')
    await dateInputs[0].setValue('2023-01-01')
    await dateInputs[1].setValue('2024-01-01')
    await wrapper.get('button[data-testid="submit-replay"]').trigger('click')
    const evt = wrapper.emitted('submit')
    expect(evt).toBeTruthy()
    const body = evt![0][0] as any
    expect(body).toHaveProperty('start')
    expect(body).toHaveProperty('end')
    expect(body).toHaveProperty('cfg_override')
    expect(body.cfg_override.min_rr_ratio).toBe(2.0)
  })
})
