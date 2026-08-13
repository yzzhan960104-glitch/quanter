/**
 * 路由表增删守护测试（Phase 1 · 前端只读化 Task 6）
 *
 * Why：ReviewView 整删 + /review 路由撤除后，路由表是「前端只读化」的最关键不变量——
 * 一旦有人误加回 /review（或撤掉核心只读入口）应立即被测试拦截。
 * - 用例 1：/review 不得回归（守 Task 5/6 整删成果）；
 * - 用例 2：核心只读路由（研究/配置 + 实盘观测）必须常驻。
 */
import { describe, it, expect } from 'vitest'
import router from '../index'

describe('router 路由表', () => {
  it('已撤除 /review（ReviewView 整删）', () => {
    const paths = router.getRoutes().map((r) => r.path)
    expect(paths).not.toContain('/review')
  })
  it('保留核心只读路由', () => {
    const paths = router.getRoutes().map((r) => r.path)
    for (const p of ['/caisen', '/lab', '/discovery', '/dashboard', '/data', '/live', '/cockpit', '/jobs']) {
      expect(paths).toContain(p)
    }
  })
})
