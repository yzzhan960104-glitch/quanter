/**
 * LoserReviewCard 亏损归因卡单测（2026-09-03 契约）。
 *
 * 挂载 → getLoserReview（loser_review.json 快照）→ 按 leg 过滤行渲染：
 * 头部（符号/浮亏%/持有/距止损/风险状态三档 tag/主因一句）+ 点击展开
 * facts/evidence/markdown 正文；分析失败行显 error；快照缺失/当日未生成
 * （legs=null）→ 空态不炸。
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import ElementPlus from 'element-plus'

class MockObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
  takeRecords() { return [] }
}
;(globalThis as any).ResizeObserver = MockObserver
;(globalThis as any).IntersectionObserver = MockObserver

const { doc } = vi.hoisted(() => ({
  doc: {
    day: '2026-09-03', generated_at: '2026-09-03 16:20:00', model: 'glm-5.3',
    legs: [
      { leg: 'main', label: '主腿', rows: [
        { leg: 'main', symbol: '300433.SZ', name: '蓝思科技', industry: '元器件',
          entry_date: '2026-08-27', entry_price: 38.91, qty: 100, last: 35.88,
          fpnl: -303, fpnl_pct: -7.8, days_held: 5, max_holding: 30,
          expire_date: '2026-10-19', stop: 33.94, dist_stop_pct: 14.6,
          tp1_price: 55.4, tp2_price: 51.3,
          trailing: { neckline: 39.0, atr: 3.37, grace: 10, in_grace: true,
                      steps_taken: 0, step: 0.05 },
          signal: { neckline: 39.0, rr: 2.43, formed_at: '2026-08-26',
                    entry_theory: 47.43, atr: 3.37 },
          kline_summary: { high: 39.5, low: 35.5, dd_pct: -7.9, since_entry_pct: -7.8 },
          market: { sh_pct: -0.38, hs300_pct: null },
          analysis: { primary: '入场时机偏晚，突破日追高后动能衰竭',
            secondary: '市场同期走弱', evidence: '现价距止损 14.6%，回撤 -7.9%',
            confidence: '高', risk_state: '持有观察',
            markdown: '## 复盘\n蓝思科技 08-27 进场…' } },
        { leg: 'main', symbol: '300017.SZ', name: '网宿科技', industry: '软件服务',
          fpnl_pct: -1.0, days_held: 1, max_holding: 30, dist_stop_pct: 11.2,
          trailing: { in_grace: true },
          analysis: { primary: null, risk_state: null, markdown: null,
            error: 'TimeoutError: read timed out' } },
      ] },
      { leg: 'exp', label: '实验腿', rows: [] },
    ],
    note: '浮亏持仓每日盘后 LLM 深度归因',
  },
}))

vi.mock('../../../api/review', () => ({
  getLoserReview: vi.fn().mockResolvedValue(doc),
}))

import LoserReviewCard from '../LoserReviewCard.vue'

const mountCard = (props?: { leg?: string }) =>
  mount(LoserReviewCard, { props, global: { plugins: [ElementPlus] } })

describe('LoserReviewCard.vue', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('按 leg 过滤渲染行：三档状态 tag+主因+持有/距止损读数', async () => {
    const w = mountCard({ leg: 'main' })
    await flushPromises()
    expect(w.text()).toContain('亏损归因')
    expect(w.text()).toContain('300433.SZ')
    expect(w.text()).toContain('-7.8%')
    expect(w.text()).toContain('持有观察')
    expect(w.text()).toContain('grace 内')
    // 分析失败行：主因位显 error、状态=未判定
    expect(w.text()).toContain('未判定')
    expect(w.text()).toContain('TimeoutError')
    // 实验腿行不混入
    expect(w.text()).not.toContain('300418')
  })

  it('点击行头部展开 facts/evidence/markdown 正文', async () => {
    const w = mountCard({ leg: 'main' })
    await flushPromises()
    expect(w.text()).not.toContain('信号颈线 39')       // 未展开
    await w.find('.lv-head').trigger('click')
    expect(w.text()).toContain('信号颈线 39')
    expect(w.text()).toContain('证据：现价距止损 14.6%')
    expect(w.text()).toContain('蓝思科技 08-27 进场')
  })

  it('当日无浮亏/快照缺失 → 空态不炸', async () => {
    const { getLoserReview } = await import('../../../api/review')
    ;(getLoserReview as any).mockResolvedValueOnce(
      { day: '2026-09-03', legs: null, note: '尚未生成' })
    const w = mountCard({ leg: 'main' })
    await flushPromises()
    expect(w.text()).toContain('当日无浮亏持仓（全绿）')
  })
})
