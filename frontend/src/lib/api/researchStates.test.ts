// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'
import { api } from '../api'

vi.mock('@/components/Toast', () => ({ toast: vi.fn() }))

afterEach(() => vi.unstubAllGlobals())

function respond(status: unknown) {
  const row = { id: 'isolated-job', status, error: null, config: { initial_capital: 0 } }
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ items: [row] }))))
  return row
}

it.each(['future_status', '', null, 0])('rejects unknown research status %s before rendering actions', async status => {
  respond(status)
  await expect(api.researchJobs()).rejects.toThrow('任务状态')
})

it('normalizes legacy pending without changing null errors or zero capital', async () => {
  const row = respond('pending')
  await expect(api.researchJobs()).resolves.toEqual({ items: [{ ...row, status: 'queued' }] })
})

it.each([
  'queued', 'running', 'cancelling', 'succeeded', 'succeeded_with_budget_exhausted',
  'failed', 'cancelled', 'interrupted', 'skipped_prerequisite',
])('preserves the distinct backend lifecycle state %s', async status => {
  const row = respond(status)
  await expect(api.researchJobs()).resolves.toEqual({ items: [row] })
})
