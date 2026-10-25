// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'

vi.mock('@/components/Toast', () => ({ toast: vi.fn() }))

afterEach(() => vi.unstubAllGlobals())

it('sends search, exchange, board and page to the catalog API', async () => {
  const fetch = vi.fn(async (_path: string) => new Response(JSON.stringify({ items: [], total: 0, page: 3, page_size: 50 })))
  vi.stubGlobal('fetch', fetch)
  await api.stockCatalog('银行', 'SH', 3, 50, '沪主板')
  const params = new URL(fetch.mock.calls[0][0], 'http://test.local').searchParams
  expect(Object.fromEntries(params)).toEqual({
    q: '银行', exchange: 'SH', board: '沪主板', page: '3', page_size: '50',
  })
  expect(QK.stockCatalog('', '', 1, '沪主板')).not.toEqual(QK.stockCatalog('', '', 1, '科创板'))
})

it('retains unfiltered results for callers omitting the optional board', async () => {
  const fetch = vi.fn(async (_path: string) => new Response(JSON.stringify({ items: [], total: 0, page: 1, page_size: 50 })))
  vi.stubGlobal('fetch', fetch)
  await api.stockCatalog()
  const params = new URL(fetch.mock.calls[0][0], 'http://test.local').searchParams
  expect(params.get('board')).toBe('')
  expect(params.get('page')).toBe('1')
})
