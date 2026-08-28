/**
 * DualAssetCard 双腿资金并排卡（2026-08-29 多腿方案）。
 *
 * 物理意图：验证双腿并排渲染 + 单腿降级不拖瞎另一腿（虚假繁荣防线沿用——
 * 字段缺失显示「—」而非 0）。getLegs/getAssetByLeg 全 mock，零网络依赖。
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import DualAssetCard from '../DualAssetCard.vue'
import { getLegs, getAssetByLeg } from '../../../api/gm'

;(globalThis as any).ResizeObserver =
  (globalThis as any).ResizeObserver || class { observe() {} unobserve() {} disconnect() {} }

vi.mock('../../../api/gm', () => ({
  getLegs: vi.fn(),
  getAssetByLeg: vi.fn(),
}))
const mockLegs = vi.mocked(getLegs)
const mockAsset = vi.mocked(getAssetByLeg)

const mountCard = () =>
  mount(DualAssetCard, { global: { plugins: [ElementPlus] } })

describe('DualAssetCard.vue', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('双腿并排：两列各自渲染 nav/available', async () => {
    mockLegs.mockResolvedValue([
      { key: 'main', label: '主腿', role: 'incumbent', account_id: 'a1', strategy_id: 's1', strategy_name: 'NECK' },
      { key: 'exp', label: '实验腿', role: 'challenger', account_id: 'a2', strategy_id: 's2', strategy_name: 'NECK-EXP' },
    ])
    mockAsset.mockImplementation(async (leg?: string) =>
      leg === 'main' ? { nav: 100007, available: 96104 } : { nav: 100000, available: 99800 })
    const w = mountCard()
    await new Promise((r) => setTimeout(r, 0))
    const text = w.text()
    expect(text).toContain('主腿')
    expect(text).toContain('实验腿')
    expect(text).toContain('100007')          // 主腿 nav
    expect(text).toContain('100000')          // 实验腿 nav
  })

  it('字段缺失显示「—」而非 0（防误读归零）', async () => {
    mockLegs.mockResolvedValue([
      { key: 'main', label: '主腿', role: 'incumbent', account_id: 'a1', strategy_id: 's1', strategy_name: null },
    ])
    mockAsset.mockResolvedValue({})   // 网关空字段形态
    const w = mountCard()
    await new Promise((r) => setTimeout(r, 0))
    expect(w.text()).toContain('—')
    expect(w.text()).not.toContain('0')
  })

  it('单腿查询失败保持上次值（抖动不闪跳）', async () => {
    mockLegs.mockResolvedValue([
      { key: 'main', label: '主腿', role: 'incumbent', account_id: 'a1', strategy_id: 's1', strategy_name: null },
      { key: 'exp', label: '实验腿', role: 'challenger', account_id: 'a2', strategy_id: 's2', strategy_name: null },
    ])
    let failExp = false
    mockAsset.mockImplementation(async (leg?: string) => {
      if (leg === 'exp' && failExp) throw new Error('502')
      return leg === 'main' ? { nav: 1 } : { nav: 2 }
    })
    const w = mountCard()
    await new Promise((r) => setTimeout(r, 0))
    expect(w.text()).toContain('2')            // 实验腿首拉成功
    failExp = true
    await (w.vm as any).fetchAll()            // 二轮：实验腿 502
    expect(w.text()).toContain('2')            // 保持上次值
    expect(w.text()).toContain('1')            // 主腿不受拖累
  })

  it('实验腿未部署（/legs 单腿）→ 无第二列不报错', async () => {
    mockLegs.mockResolvedValue([
      { key: 'main', label: '主腿', role: 'incumbent', account_id: 'a1', strategy_id: 's1', strategy_name: null },
    ])
    mockAsset.mockResolvedValue({ nav: 5 })
    const w = mountCard()
    await new Promise((r) => setTimeout(r, 0))
    expect(w.text()).toContain('主腿')
    expect(w.text()).not.toContain('实验腿')
  })
})
