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
  listPlans, getPlan, getChart,
  listReplayTasks, getReplayTask,
} from './caisen'
// Phase 1 · 前端只读化：scan/reviewPlan/activatePlan 三写函数已从 caisen.ts 撤除，
// 对应契约用例同步移除（候选→EOD 自动产出 / 审核→veto_plan.py / 激活→pre_open cron）。
// Phase 1 · Task 3：submitReplayAsync/cancelReplayTask/deleteReplayTask 三写函数已从
// caisen.ts 撤除，对应契约用例同步移除（回测提交/取消/删除走 backtest 域脚本/CLI）。

const mockGet = vi.mocked(apiClient.get)
// mockPost/mockPatch/mockDelete 已随 replay/scan 写函数用例全部移除（Phase 1 · 前端只读化）；
// apiClient.post/patch/delete 仍保留在 mock 工厂里以如实反映 client.ts 的真实结构，避免测试侧结构漂移。

beforeEach(() => {
  mockGet.mockReset()
  // facade 期望拿到 response.data（client.ts 响应拦截器剥壳），但 client 被 mock 绕过拦截器，
  // 这里直接 resolve 一个占位值，让 await 不抛；断言只关心「如何调用」而非「返回什么」。
  mockGet.mockResolvedValue([] as any)
})

describe('caisen facade 契约 —— URL / method / payload / timeout', () => {
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

  it('getChart：GET .../chart，timeout 15000（图表数据装配放宽）', async () => {
    await getChart('p1')
    expect(mockGet).toHaveBeenCalledWith('/api/v1/caisen/plans/p1/chart', { timeout: 15000 })
  })

  // 注：老同步 runReplay（POST /caisen/replay）用例随 Spec 2 Task 8 /caisen 回放 tab 下线移除。
  // Phase 1 · 前端只读化：scan / reviewPlan / activatePlan 三写函数用例同步移除——
  // 候选→EOD 自动产出 / 审核→veto_plan.py / 激活→pre_open cron。
  // 回测能力由下方异步任务 5 端点承接（/lab 消费）。

  // ============ 异步回测任务（Spec 2 Task 2；Phase 1 Task 3 撤写后仅余 list/get 观测） ============

  // Phase 1 · Task 3：submitReplayAsync / cancelReplayTask / deleteReplayTask 三写函数用例
  // 已随 caisen.ts 对应函数撤除同步移除——回测提交/取消/删除走 backtest 域脚本/CLI。

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
})
