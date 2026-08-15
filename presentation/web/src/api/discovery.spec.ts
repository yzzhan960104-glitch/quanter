/**
 * discovery facade 解包语义回归钉（CR-1 死页根因 · 2026-08-15 技术债波次）
 *
 * Why 本测试存在：client.ts 响应拦截器 `(response) => response.data` 已在运行时
 * 剥掉 axios 包壳——apiClient.get 直接 resolve 业务 payload 本身。而本 facade 四函数
 * 曾误写为「await 之后再解构出 data」的姿势，对已剥壳的结果二次解构：data 恒为
 * undefined，视图层拿到空数据静默渲染空态——HTTP 200 的「死页」（CR-1），
 * 无任何报错可循。此测试把「直返 payload」语义钉死，防止姿势回潮。
 *
 * mock 边界：只 mock ./client 的 get（对齐拦截器剥壳后的运行时真值），被测对象是
 * discovery.ts 本身的解包姿势——这正是死页 bug 所在层，不能 mock 掉。
 */
import { describe, it, expect, vi } from 'vitest'

// 对齐 client.ts 拦截器运行时语义：apiClient.get 已直接 resolve 业务 payload
const payload = { n_trials: 12, marginals: [], ranking: [], dead_params: [], blind_spots: [] }

vi.mock('./client', () => ({ apiClient: { get: vi.fn(async () => payload) } }))

import { getSensitivity } from './discovery'

describe('discovery facade 解包语义（CR-1 回归钉）', () => {
  it('getSensitivity 直返 payload，不做二次解构', async () => {
    expect(await getSensitivity()).toBe(payload) // 二次解包会得 undefined → 红
  })
})
