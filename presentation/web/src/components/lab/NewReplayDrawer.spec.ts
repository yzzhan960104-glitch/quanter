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

// 最小 schema fixture：覆盖两类分组（识别层=核心默认展开 + 执行层/trailing层=高级折叠）。
// 键集用颈线法真实参数（paramMeta.ts 同步守护的 21 维键），分层用例需核心+高级各至少
// 一字段才能断言「默认核心在/高级不在」。
const SCHEMA = {
  properties: {
    window:        { type: 'integer', default: 60, description: '颈线识别窗口' },     // 识别层=核心
    max_holding:   { type: 'integer', default: 15, description: '超时持仓日' },       // 执行层=高级
    trailing_step: { type: 'number', default: 0.1, description: 'trailing收紧速度' }, // trailing层=高级
  },
}

describe('NewReplayDrawer', () => {
  it('visible 时默认仅渲染识别层标题 + 区间/标的输入（高级组折叠在开关后）', async () => {
    const wrapper = mount(NewReplayDrawer, {
      props: { visible: true, configSchema: SCHEMA, prefill: null },
      global: { plugins: [ElementPlus] },
    })
    await flushPromises()
    // 形态核心组「识别层」默认在（window 触发）
    expect(wrapper.text()).toContain('识别层')
    // 高级组默认折叠在「显示高级参数」开关后——未开开关时不可见（分层契约）
    expect(wrapper.text()).not.toContain('执行层')
    expect(wrapper.text()).not.toContain('trailing层')
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
    // schema 含核心组(window∈识别层) + 高级组(max_holding∈执行层 + trailing_step∈trailing层)。
    const wrapper = mount(NewReplayDrawer, {
      props: { visible: true, configSchema: SCHEMA, prefill: null },
      global: { plugins: [ElementPlus] },
    })
    await flushPromises()
    // 默认：核心组「识别层」在；高级组「执行层」「trailing层」不在（分层契约）
    expect(wrapper.text()).toContain('识别层')
    expect(wrapper.text()).not.toContain('执行层')
    expect(wrapper.text()).not.toContain('trailing层')
    // 开 showAdvanced（el-switch jsdom 渲染为 checkbox，见文件头探测结论）
    await wrapper.get('.el-switch input').setValue(true)
    await flushPromises()
    // 开关打开后：高级组「执行层」「trailing层」可见（开关仅控可见性，非恒真断言）
    expect(wrapper.text()).toContain('执行层')
    expect(wrapper.text()).toContain('trailing层')
  })

  it('prefill 灌入：max_holding 显示 prefill 值而非 schema 默认（需开 showAdvanced 才能看到高级组字段）', async () => {
    const wrapper = mount(NewReplayDrawer, {
      props: { visible: true, configSchema: SCHEMA, prefill: { max_holding: 20 } },
      global: { plugins: [ElementPlus] },
    })
    await flushPromises()
    // max_holding∈执行层=高级组，默认折叠。开 showAdvanced 让该字段渲染出来，再断言 prefill 灌入。
    await wrapper.get('.el-switch input').setValue(true)
    await flushPromises()
    // 执行层字段渲染顺序在识别层之后——从全部 spinbutton 中定位 max_holding 的值 20
    const inputs = wrapper.findAll('input[role="spinbutton"]')
    const holding = inputs.find((i) => (i.element as HTMLInputElement).value.includes('20'))
    expect(holding).toBeTruthy()
  })

  it('点提交 emit submit，payload 含 start/end/universe/cfg_override（含 prefill 改值，高级参数仍收集）', async () => {
    const wrapper = mount(NewReplayDrawer, {
      props: { visible: true, configSchema: SCHEMA, prefill: { max_holding: 20 } },
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
    // 高级组字段 max_holding 仍进 cfg_override 提交（开关仅控可见性，不丢值、不影响提交）
    expect(body.cfg_override.max_holding).toBe(20)
  })
})
