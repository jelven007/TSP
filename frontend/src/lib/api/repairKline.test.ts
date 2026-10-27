// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'
import { api } from '@/lib/api'

vi.mock('@/components/Toast', () => ({ toast: vi.fn() }))
afterEach(() => vi.unstubAllGlobals())

it.each(['full', 'missing'] as const)('sends the selected end date for %s repair', async mode => {
  const fetch = vi.fn(async (_path: string, _options?: RequestInit) =>
    new Response(JSON.stringify({ status: 'started', job_id: 'repair-1' })))
  vi.stubGlobal('fetch', fetch)
  await api.repairDaily('2026-10-22', '1m', mode, '2026-10-23')
  expect(fetch.mock.calls[0][0]).toBe('/api/kline/repair_daily')
  expect(JSON.parse(fetch.mock.calls[0][1]!.body as string)).toEqual({
    start_date: '2026-10-22', end_date: '2026-10-23', period: '1m', mode,
  })
})

it('preserves the default daily request for old callers omitting end date', async () => {
  const fetch = vi.fn(async (_path: string, _options?: RequestInit) =>
    new Response(JSON.stringify({ status: 'started', job_id: 'repair-1' })))
  vi.stubGlobal('fetch', fetch)
  await api.repairDaily('2026-10-22')
  expect(JSON.parse(fetch.mock.calls[0][1]!.body as string)).toEqual({
    start_date: '2026-10-22', period: '1d', mode: 'full',
  })
})
