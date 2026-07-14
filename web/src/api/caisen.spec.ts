/**
 * caisen.ts facade 契约单测（第 3 项「前端组件/单测」层）。
 *
 * 物理意图：caisen.ts 是蔡森流水线的「前端契约层」，把后端 REST 端点封装为类型化函数。
 * 本测试 mock 掉 HTTP 通道（./client 的 apiClient），纯粹断言每个 facade 函数调用 apiClient
 * 的【URL / method / payload / timeout】正确——抓「facade 字段映射错误 / 超时配置漂移 /
 * 路径参数未 encode」这类后端测试覆盖不到的前端侧契约回归（比 E2E 快 ~100 倍）。
 *
 * Why mock apiClient 而非真发请求：facade 层零业务逻辑（纯 HTTP 通道 + 类型守护），契约的
 * 正确性 = 调用姿势的正确性，无需真实后端。与 scripts/check_contracts.py（端点存在性）互补：
 * 那个查「端点在后端 openapi 存不存在」，这个查「前端调用姿势对不对」。
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'

// vi.mock 被 hoist 到文件顶部（vitest 静态分析提升）；工厂内调用 vi.fn() 合法，引用外部变量非法。
// 替换 ./client 整模块 → client.ts 的 ElMessage / 响应拦截器代码不执行，测试环境无需 element-plus。
vi.mock('./client', () => ({
  apiClient: {
    post: vi.fn(),
    get: vi.fn(),
    patch: vi.fn(),
    delete: vi.fn(),   // 新增：DELETE /replay/tasks/{id} 用（Spec 2 Task 2）
  },
}))

import { apiClient } from './client'
import {
  scan, listPlans, getPlan, reviewPlan, activatePlan, getChart, runReplay,
  submitReplayAsync, listReplayTasks, getReplayTask, cancelReplayTask, deleteReplayTask,
} from './caisen'

const mockPost = vi.mocked(apiClient.post)
const mockGet = vi.mocked(apiClient.get)
const mockPatch = vi.mocked(apiClient.patch)
const mockDelete = vi.mocked(apiClient.delete)

beforeEach(() => {
  mockPost.mockReset()
  mockGet.mockReset()
  mockPatch.mockReset()
  mockDelete.mockReset()
  // facade 期望拿到 response.data（client.ts 响应拦截器剥壳），但 client 被 mock 绕过拦截器，
  // 这里直接 resolve 一个占位值，让 await 不抛；断言只关心「如何调用」而非「返回什么」。
  mockPost.mockResolvedValue([] as any)
  mockGet.mockResolvedValue([] as any)
  mockPatch.mockResolvedValue({} as any)
  mockDelete.mockResolvedValue({} as any)
})

describe('caisen facade 契约 —— URL / method / payload / timeout', () => {
  it('scan: POST /api/v1/caisen/scan，body 透传，timeout 30000', async () => {
    await scan({ date: '2026-01-01', universe: ['510300.SH'] })
    expect(mockPost).toHaveBeenCalledWith(
      '/api/v1/caisen/scan',
      { date: '2026-01-01', universe: ['510300.SH'] },
      { timeout: 30000 },
    )
  })

  it('listPlans() 无 status：GET /api/v1/caisen/plans，params 空，timeout 10000', async () => {
    await listPlans()
    expect(mockGet).toHaveBeenCalledWith('/api/v1/caisen/plans', { params: {}, timeout: 10000 })
  })

  it('listPlans(status)：params 含 status', async () => {
    await listPlans('APPROVED')
    expect(mockGet).toHaveBeenCalledWith(
      '/api/v1/caisen/plans',
      { params: { status: 'APPROVED' }, timeout: 10000 },
    )
  })

  it('getPlan：planId 经 encodeURIComponent（含 / 编码为 %2F，防路径穿越端点错配）', async () => {
    await getPlan('plan/1')
    expect(mockGet).toHaveBeenCalledWith('/api/v1/caisen/plans/plan%2F1', { timeout: 10000 })
  })

  it('reviewPlan：PATCH /api/v1/caisen/plans/{id}，body 透传', async () => {
    await reviewPlan('p1', { action: 'approve', edits: { stop_loss: 10 } })
    expect(mockPatch).toHaveBeenCalledWith(
      '/api/v1/caisen/plans/p1',
      { action: 'approve', edits: { stop_loss: 10 } },
      { timeout: 10000 },
    )
  })

  it('activatePlan：POST .../activate，空 body {}', async () => {
    await activatePlan('p1')
    expect(mockPost).toHaveBeenCalledWith('/api/v1/caisen/plans/p1/activate', {}, { timeout: 10000 })
  })

  it('getChart：GET .../chart，timeout 15000（图表数据装配放宽）', async () => {
    await getChart('p1')
    expect(mockGet).toHaveBeenCalledWith('/api/v1/caisen/plans/p1/chart', { timeout: 15000 })
  })

  it('runReplay：POST /api/v1/caisen/replay，timeout 90000（全市场回放计算密集）', async () => {
    await runReplay({ start: '2026-01-01', end: '2026-02-01' })
    expect(mockPost).toHaveBeenCalledWith(
      '/api/v1/caisen/replay',
      { start: '2026-01-01', end: '2026-02-01' },
      { timeout: 90000 },
    )
  })

  // ============ 异步回测任务（Spec 2 Task 2；对应 Spec 1 后端 5 端点） ============

  it('submitReplayAsync: POST /replay/async，body 透传，timeout 10000', async () => {
    await submitReplayAsync({ start: '2024-01-01', end: '2024-06-01', cfg_override: { min_rr_ratio: 1.5 } })
    expect(mockPost).toHaveBeenCalledWith(
      '/api/v1/caisen/replay/async',
      { start: '2024-01-01', end: '2024-06-01', cfg_override: { min_rr_ratio: 1.5 } },
      { timeout: 10000 },
    )
  })

  it('listReplayTasks() 无 status：GET /replay/tasks，params 空', async () => {
    await listReplayTasks()
    expect(mockGet).toHaveBeenCalledWith('/api/v1/caisen/replay/tasks', { params: {}, timeout: 10000 })
  })

  it('listReplayTasks(status)：params 含 status', async () => {
    await listReplayTasks('RUNNING')
    expect(mockGet).toHaveBeenCalledWith(
      '/api/v1/caisen/replay/tasks', { params: { status: 'RUNNING' }, timeout: 10000 })
  })

  it('getReplayTask：task_id 经 encodeURIComponent', async () => {
    await getReplayTask('abc 123')
    expect(mockGet).toHaveBeenCalledWith('/api/v1/caisen/replay/tasks/abc%20123', { timeout: 10000 })
  })

  it('cancelReplayTask：POST .../cancel，空 body {}', async () => {
    await cancelReplayTask('t1')
    expect(mockPost).toHaveBeenCalledWith('/api/v1/caisen/replay/tasks/t1/cancel', {}, { timeout: 10000 })
  })

  it('deleteReplayTask：DELETE .../tasks/{id}', async () => {
    await deleteReplayTask('t1')
    expect(mockDelete).toHaveBeenCalledWith('/api/v1/caisen/replay/tasks/t1', { timeout: 10000 })
  })
})
