// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'
import { api } from '@/lib/api'

vi.mock('@/components/Toast', () => ({ toast: vi.fn() }))
afterEach(() => vi.unstubAllGlobals())

it('omits an empty index start date and preserves an explicit one', async () => {
  const fetch = vi.fn(async (_path: string) => new Response(JSON.stringify({
    symbol: '000001.SH', period: '1d', rows: [], source: 'none',
  })))
  vi.stubGlobal('fetch', fetch)

  await api.indexKline('000001.SH', '1d', { start: '', end: '2026-10-29' })
  await api.indexKline('000001.SH', '1d', { start: '2020-11-02', end: '2026-10-29' })

  const first = new URL(fetch.mock.calls[0][0] as string, 'http://localhost')
  const second = new URL(fetch.mock.calls[1][0] as string, 'http://localhost')
  expect(first.searchParams.has('start_date')).toBe(false)
  expect(second.searchParams.get('start_date')).toBe('2020-11-02')
})
