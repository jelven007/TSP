// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { StrategyDailyResults } from './StrategyDailyResults'
import type { ScreenerResult } from '@/lib/api'
import { defaultStrategyResultColumns } from '@/lib/strategy-result-columns'

const { snapshot } = vi.hoisted(() => ({ snapshot: vi.fn() }))
vi.mock('@/lib/api', () => ({ api: { marketSnapshot: snapshot } }))
vi.mock('@/components/StockPreviewDialog', () => ({ StockPreviewDialog: () => null }))
Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
let root: Root
let host: HTMLDivElement
let client: QueryClient
const symbols = ['600001.SH', '600002.SH', '600003.SH', '600004.SH', '600005.SH']
const rows = symbols.map((symbol, index) => ({ symbol, name: `股票${index}`, close: 100 - index, score: index }))
const result: ScreenerResult = {
  strategy: 'custom_s02', as_of: '2026-11-06', rows: [], total: 0, elapsed_ms: 0,
  section_mode: 'signals', sections: { sell: rows, select: rows, buy: rows },
}
const onPreview = vi.fn()
beforeEach(() => {
  vi.useFakeTimers()
  vi.setSystemTime(new Date('2026-11-06T10:00:00+08:00'))
  onPreview.mockReset()
  snapshot.mockReset().mockResolvedValue({ as_of: '2026-11-06', rows: [
    { symbol: symbols[0], raw_close: 10, change_pct: .1, turnover_rate: 10, vol_ratio_5d: 10 },
    { symbol: symbols[1], raw_close: 2, change_pct: -.02, turnover_rate: 2, vol_ratio_5d: 2 },
    { symbol: symbols[2], raw_close: 0, change_pct: NaN, turnover_rate: Infinity },
    { symbol: symbols[3], raw_close: Infinity },
  ] })
})
afterEach(async () => {
  await act(async () => root?.unmount())
  client?.clear(); host?.remove(); vi.useRealTimers()
})
async function flush() { await act(async () => { await vi.advanceTimersByTimeAsync(10) }) }
async function render(columns?: ReturnType<typeof defaultStrategyResultColumns>) {
  host = document.createElement('div'); document.body.append(host); root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await act(async () => root.render(<QueryClientProvider client={client}>
    <StrategyDailyResults result={result} positionLimit={5} onPreview={onPreview} signalColumns={columns} />
  </QueryClientProvider>))
  await flush()
}
function order() {
  return [...host.querySelectorAll('tbody td[data-column="symbol"]')].map(cell => cell.textContent)
}
async function sort(key: string) {
  const button = host.querySelector<HTMLButtonElement>(`th[data-column="${key}"] button`)
  expect(button, `${key} header supports keyboard and pointer sorting`).not.toBeNull()
  await act(async () => button!.click())
}
it.each([0, 1, 2])('sorts live numeric columns in tab %i in three states without altering archived rows', async index => {
  await render()
  await act(async () => host.querySelectorAll<HTMLButtonElement>('[role="tab"]')[index].click())
  await flush()
  for (const key of ['live_price', 'live_change_pct', 'live_turnover', 'live_vol_ratio']) {
    expect(order()).toEqual(symbols)
    await sort(key)
    expect(order()).toEqual([symbols[1], symbols[0], ...symbols.slice(2)])
    expect(host.querySelector(`th[data-column="${key}"]`)?.getAttribute('aria-sort')).toBe('ascending')
    await sort(key)
    expect(order()).toEqual(symbols)
    expect(host.querySelector(`th[data-column="${key}"]`)?.getAttribute('aria-sort')).toBe('descending')
    await sort(key)
    expect(order()).toEqual(symbols)
    expect(host.querySelector(`th[data-column="${key}"]`)?.getAttribute('aria-sort')).toBe('none')
  }
  expect(result.sections?.sell).toBe(rows)
  expect(rows.map(row => row.symbol)).toEqual(symbols)
})
it('reorders after a live update and uses displayed order for stock navigation', async () => {
  await render(); await sort('live_price')
  snapshot.mockResolvedValue({ as_of: '2026-11-06', rows: [
    { symbol: symbols[0], raw_close: 1 }, { symbol: symbols[1], raw_close: 20 },
  ] })
  await act(async () => { await vi.advanceTimersByTimeAsync(1010) })
  expect(order()).toEqual(symbols)
  await sort('live_price')
  expect(order()).toEqual([symbols[1], symbols[0], ...symbols.slice(2)])
  await act(async () => host.querySelector<HTMLButtonElement>('tbody td[data-column="symbol"] button')!.click())
  expect(onPreview.mock.calls[0][0]).toBe(symbols[1])
  expect(onPreview.mock.calls[0][2].map((item: { symbol: string }) => item.symbol)).toEqual(order())
})
it('preserves live sorting across tabs and keeps the saved selection rank', async () => {
  await render(); await sort('live_price')
  await act(async () => host.querySelectorAll<HTMLButtonElement>('[role="tab"]')[1].click())
  await flush()
  expect(order()).toEqual([symbols[1], symbols[0], ...symbols.slice(2)])
  expect([...host.querySelectorAll('tbody td[data-column="selection_rank"]')].map(cell => cell.textContent))
    .toEqual(['2', '1', '3', '4', '5'])
})
it('sorts saved scalar columns and preserves custom column order', async () => {
  const columns = defaultStrategyResultColumns('sell').reverse()
  await render(columns)
  const headers = [...host.querySelectorAll('th')].map(el => el.getAttribute('data-column'))
  expect(headers).toEqual(columns.filter(col => col.visible && col.source.type === 'builtin')
    .map(col => col.source.type === 'builtin' ? col.source.key : ''))
  await sort('close')
  expect(order()).toEqual([...symbols].reverse())
  expect(host.querySelector('th[data-column="live_signals"] button')).toBeNull()
  expect(host.querySelector('th[data-column="suggested_sell_price"] button')).toBeNull()
})
