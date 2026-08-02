import { describe, it, expect, vi, beforeEach } from 'vitest'

vi.mock('./client', () => ({
  apiClient: { get: vi.fn(), post: vi.fn() },
}))

import { apiClient } from './client'
import { getJobs } from './trading'

const mockGet = vi.mocked(apiClient.get)

beforeEach(() => {
  mockGet.mockReset()
  mockGet.mockResolvedValue({} as any)
})

describe('trading facade getJobs 契约', () => {
  it('getJobs: GET /api/v1/trading/jobs，params 含 date，timeout 10000', async () => {
    await getJobs('2026-08-02')
    expect(mockGet).toHaveBeenCalledWith(
      '/api/v1/trading/jobs',
      { params: { date: '2026-08-02' }, timeout: 10000 },
    )
  })
})
