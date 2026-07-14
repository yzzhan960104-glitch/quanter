/**
 * NewReplayDrawer 组件单测：分组表单渲染 + prefill + 提交 body 契约 + 形态核心/高级分层。
 * 复用 DatasetTable.spec.ts 的 jsdom polyfill + ElementPlus 全量注册模式。
 *
 * el-switch jsdom 渲染探测结论（task2-brief「selector 兜底」实证）：
 *   el-switch 渲染为 <input class="el-switch__input" type="checkbox" role="switch">，
 *   `input[type="checkbox"]` / `input[role="switch"]` / `.el-switch input` 三个 selector 均命中。
 *   本文件统一用 `.el-switch input`（语义最精准，不与可能的 checkbox 冲突）。
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

// 最小 schema fixture：覆盖三类（含核心组 confirm_bars∈蔡森方法学 + 高级组 min_rr_ratio∈交易执行 +
// max_holding_bars∈时间止损）。分层用例需核心+高级各至少一字段才能断言「默认核心在/高级不在」。
const SCHEMA = {
  properties: {
    confirm_bars:     { type: 'integer', default: 3, description: 'ZigZag确认窗' },  // 蔡森方法学=核心
    min_rr_ratio:     { type: 'number', default: 1.5, description: '盈亏比下限' },    // 交易执行=高级
    max_holding_bars: { type: 'integer', default: 15, description: '最大持仓周期' },  // 时间止损=高级
  },
}

describe('NewReplayDrawer', () => {
  it('visible 时默认仅渲染形态核心组标题 + 区间/标的输入（高级组折叠在开关后）', async () => {
    const wrapper = mount(NewReplayDrawer, {
      props: { visible: true, configSchema: SCHEMA, prefill: null },
      global: { plugins: [ElementPlus] },
    })
    await flushPromises()
    // 形态核心组「蔡森方法学」默认在（confirm_bars 触发）
    expect(wrapper.text()).toContain('蔡森方法学')
    // 高级组默认折叠在「显示高级参数」开关后——未开开关时不可见（分层 Task 2 契约）
    expect(wrapper.text()).not.toContain('交易执行')
    expect(wrapper.text()).not.toContain('时间止损')
    // 区间输入真实验证：断言两个 el-date-picker（start/end）真渲染到 DOM。
    //
    // 为何用 .el-date-editor 计数=2 而非 [data-testid]：el-date-picker 在当前 EP 版本下
    // 会吞掉非 prop attr（data-testid 不透传到根节点，jsdom 实测 testid 命中数=0）。
    // .el-date-editor 是 el-date-picker 渲染出的稳定根 class，count===2 精确对应模板里两个
    // 日期选择器——既不依赖文案（placeholder），又真实验证区间输入存在（失败时 count≠2 真抛错）。
    // 原 `find(input[placeholder]).exists() || wrapper.text()` 后半永为真理值，恒真，名存实亡。
    expect(wrapper.findAll('.el-date-editor')).toHaveLength(2)
  })

  it('默认仅渲染形态核心组（高级组隐藏）；开 showAdvanced 后高级组出现', async () => {
    // schema 含核心组(confirm_bars∈蔡森方法学) + 高级组(min_rr_ratio∈交易执行 +
    //   max_holding_bars∈时间止损)。构造一个核心两个高级字段。
    const wrapper = mount(NewReplayDrawer, {
      props: { visible: true, configSchema: SCHEMA, prefill: null },
      global: { plugins: [ElementPlus] },
    })
    await flushPromises()
    // 默认：核心组「蔡森方法学」在；高级组「交易执行」「时间止损」不在（分层 Task 2 契约）
    expect(wrapper.text()).toContain('蔡森方法学')
    expect(wrapper.text()).not.toContain('交易执行')
    expect(wrapper.text()).not.toContain('时间止损')
    // 开 showAdvanced（el-switch jsdom 渲染为 checkbox，见文件头探测结论）
    await wrapper.get('.el-switch input').setValue(true)
    await flushPromises()
    // 开关打开后：高级组「交易执行」「时间止损」可见（开关仅控可见性，非恒真断言）
    expect(wrapper.text()).toContain('交易执行')
    expect(wrapper.text()).toContain('时间止损')
  })

  it('prefill 灌入：min_rr_ratio 显示 prefill 值而非 schema 默认（需开 showAdvanced 才能看到高级组字段）', async () => {
    const wrapper = mount(NewReplayDrawer, {
      props: { visible: true, configSchema: SCHEMA, prefill: { min_rr_ratio: 2.0 } },
      global: { plugins: [ElementPlus] },
    })
    await flushPromises()
    // min_rr_ratio∈交易执行=高级组，默认折叠。开 showAdvanced 让该字段渲染出来，再断言 prefill 灌入。
    await wrapper.get('.el-switch input').setValue(true)
    await flushPromises()
    // el-input-number 的输入框值 = 2.0（prefill 覆盖默认 1.5）
    const input = wrapper.find('input[role="spinbutton"]')
    expect((input.element as HTMLInputElement).value).toContain('2')
  })

  it('点提交 emit submit，payload 含 start/end/universe/cfg_override（含 prefill 改值，高级参数仍收集）', async () => {
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
    // 高级组字段 min_rr_ratio 仍进 cfg_override 提交（开关仅控可见性，不丢值、不影响提交）
    expect(body.cfg_override.min_rr_ratio).toBe(2.0)
  })
})
