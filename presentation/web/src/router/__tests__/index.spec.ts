/**
 * 路由表增删守护测试（Phase 1 · 前端只读化 Task 6 + G8 caisen 死视图清理）。
 *
 * Why：路由表是「前端只读化 / caisen 退役」的最关键不变量——一旦有人误加回 /review
 * （ReviewView 整删）或 /caisen /lab（caisen 死视图整删，G8）应立即被测试拦截。
 * - 用例 1：/review 不得回归（守 Task 5/6 整删成果）；
 * - 用例 2：/caisen、/lab 不得回归（守 G8 caisen 形态退役 + 契约清理成果——后端
 *   server/api/v1/caisen.py 已删，前端 caisen.ts 调的 6 个端点全 404，CaisenScreenView
 *   + ParamLabView 整删让 check_contracts gate② 绿）；
 * - 用例 3：核心只读路由（研究/发现 + 实盘观测）必须常驻。
 */
import { describe, it, expect } from 'vitest'
import router from '../index'

describe('router 路由表', () => {
  it('已撤除 /review（ReviewView 整删）', () => {
    const paths = router.getRoutes().map((r) => r.path)
    expect(paths).not.toContain('/review')
  })
  it('已撤除 /caisen 与 /lab（G8 · caisen 死视图清理：后端 router 删 → 契约对齐）', () => {
    const paths = router.getRoutes().map((r) => r.path)
    // /caisen /lab 对应的 CaisenScreenView/ParamLabView 调 caisen.ts 死端点（后端
    // /api/v1/caisen/* 全删），整删让 check_contracts gate② 绿。重建需先补后端端点
    // + 配套 facade，此处守护防误加回。
    expect(paths).not.toContain('/caisen')
    expect(paths).not.toContain('/lab')
  })
  it('保留核心只读路由', () => {
    const paths = router.getRoutes().map((r) => r.path)
    for (const p of ['/discovery', '/dashboard', '/data', '/live', '/cockpit', '/jobs']) {
      expect(paths).toContain(p)
    }
  })
})
