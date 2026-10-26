// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'

vi.mock('@/components/Toast', () => ({ toast: vi.fn() }))
afterEach(() => vi.unstubAllGlobals())

it('passes date, asset and timeframe to the same dimensions used in query keys', async () => {
  const fetch = vi.fn(async (_path: string) => new Response(JSON.stringify({ results: {} })))
  vi.stubGlobal('fetch', fetch)
  await api.screenerCachedSummary('2026-10-22', 'etf', 'all')
  await api.screenerCachedResult('sample', 'concept.name', '2026-10-22', 'etf', '1m')
  await api.screenerCached(undefined, '2026-10-22', 'etf', 'all')
  for (const [path] of fetch.mock.calls) {
    const params = new URL(path, 'http://test.local').searchParams
    expect(params.get('as_of')).toBe('2026-10-22')
    expect(params.get('asset_type')).toBe('etf')
    expect(params.has('timeframe')).toBe(true)
  }
  expect(QK.screenerCachedSummary('2026-10-22', 'stock')).not.toEqual(
    QK.screenerCachedSummary('2026-10-23', 'stock'),
  )
  expect(QK.screenerCachedResult('sample', '2026-10-22', '', 'stock', '1d')).not.toEqual(
    QK.screenerCachedResult('sample', '2026-10-22', '', 'etf', '1d'),
  )
})

it('reads explicit historical snapshots and encodes paging and record identifiers', async () => {
  const fetch = vi.fn(async (_path: string) => new Response(JSON.stringify({ records: [] })))
  vi.stubGlobal('fetch', fetch)
  await api.screenerResultHistory('sample', 'id:cursor')
  await api.screenerResultHistoryDetail('sample', 'id:record')
  expect(fetch.mock.calls[0][0]).toContain('cursor=id%3Acursor')
  expect(fetch.mock.calls[1][0]).toContain('id%3Arecord')
})

it('encodes literal history filters and deletes only the selected record', async () => {
  const fetch = vi.fn(async (_path: string, _init?: RequestInit) => new Response(JSON.stringify({ ok: true })))
  vi.stubGlobal('fetch', fetch)
  const filters = { q: '旧名称_100%', start_date: '2026-10-20', end_date: '2026-10-22',
    asset_type: 'etf' as const, timeframe: '1m' as const }
  await api.screenerResultHistory('sample/one', 'id:cursor', filters)
  const url = new URL(fetch.mock.calls[0][0], 'http://test.local')
  expect(url.pathname).toBe('/api/screener/result-history/sample%2Fone')
  for (const [name, value] of Object.entries(filters)) expect(url.searchParams.get(name)).toBe(value)
  expect(QK.screenerHistory('sample', undefined, filters)).not.toEqual(QK.screenerHistory('sample'))
  expect(QK.screenerHistory('sample')).toEqual(['screener-history', 'sample', ''])
  await api.screenerResultHistoryDelete('sample/one', 'id:record')
  expect(fetch.mock.calls[1][0]).toBe('/api/screener/result-history/sample%2Fone/id%3Arecord')
  expect(fetch.mock.calls[1][1]?.method).toBe('DELETE')
})
