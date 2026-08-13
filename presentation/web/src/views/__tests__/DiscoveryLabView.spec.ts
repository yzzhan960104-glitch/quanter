/**
 * DiscoveryLabView 冒烟测试（P3 · 2026-08-13）
 *
 * 物理意图：三块布局渲染 + API mock（敏感性/参数/状态/热力图）——空态兜底不白屏
 * （语料为空时显示「语料不足」提示而非崩溃），热力图维度选择器联动存在。
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import DiscoveryLabView from '../DiscoveryLabView.vue'

vi.mock('@/api/discovery', () => ({
  getSensitivity: vi.fn(async () => ({
    n_trials: 0, metric: 'calmar', marginals: {}, ranking: [],
    dead_params: [], blind_spots: {},
  })),
  getHeatmap: vi.fn(async () => ({
    x_axis: [], y_axis: [], grid: [], n_obs: [], fill: false,
  })),
  getParams: vi.fn(async () => ({
    param_space: [{ key: 'window', layer: 'id', candidates: [40, 60, 80] }],
    constraints: ['tp1_h_mult <= tp_h_mult'],
  })),
  getDiscoveryStatus: vi.fn(async () => ({
    n_trials: 0, latest_run: null, champion: null,
  })),
}))

describe('DiscoveryLabView', () => {
  beforeEach(() => vi.clearAllMocks())

  it('渲染三块布局标题（敏感性/热力图/进展）', async () => {
    const wrapper = mount(DiscoveryLabView)
    await flushPromises()
    expect(wrapper.text()).toContain('搜索实验室')
    expect(wrapper.text()).toContain('主效应排名')
    expect(wrapper.text()).toContain('两维热力图')
    expect(wrapper.text()).toContain('搜索进展')
  })

  it('空语料 → 空态提示不白屏', async () => {
    const wrapper = mount(DiscoveryLabView)
    await flushPromises()
    expect(wrapper.text()).toContain('语料不足')
  })
})
