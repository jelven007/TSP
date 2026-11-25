// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, it, vi } from 'vitest'
import { StrategyDailyResults } from './StrategyDailyResults'
import type { ScreenerResult } from '@/lib/api'

vi.mock('@/lib/api', () => ({ api: {
  strategy: vi.fn().mockResolvedValue({ exit_signals: [] }),
  strategyGet: vi.fn().mockResolvedValue({ exit_signals: [] }),
  customSignalsList: vi.fn().mockResolvedValue({ signals: [] }),
} }))
vi.mock('@/components/StockPreviewDialog', () => ({ StockPreviewDialog: () => null }))
Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
let root: Root
let host: HTMLDivElement
let client: QueryClient
afterEach(async () => { await act(async () => root?.unmount()); client?.clear(); host?.remove() })
const rows = [
  { symbol: '600001.SH', selection_rank: 1, score: 90, atr14_previous_close: .01, entry_group: 'A', entry_priority: 1 },
  { symbol: '600002.SH', selection_rank: 8, score: 80, atr14_previous_close: .04, entry_group: 'A', entry_priority: 3 },
  { symbol: '600003.SH', selection_rank: 3, score: 85, atr14_previous_close: .02, entry_group: 'B', entry_priority: 4 },
  { symbol: '600004.SH', selection_rank: 2, score: 89, atr14_previous_close: null, entry_group: 'A', entry_priority: 2 },
]
const data = {
  strategy: 'custom_mvxxcs03', as_of: '2026-11-20', rows: [], total: 0, elapsed_ms: 0,
  section_mode: 'signals', sections: { sell: rows, select: rows, buy: rows },
  selection_snapshot: { scoring_policy: 's03_five_dimensions_v1', rows,
    as_of: '2026-11-20', source_date: '2026-11-19', generated_at: '2026-11-19T22:10:00+08:00' },
} as ScreenerResult
const expected = ['600001.SH', '600004.SH', '600003.SH', '600002.SH']
const buyExpected = ['600001.SH', '600004.SH', '600002.SH', '600003.SH']
async function render() {
  host = document.createElement('div'); document.body.append(host); root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await act(async () => root.render(<QueryClientProvider client={client}>
    <StrategyDailyResults result={data} currentDate="2026-11-24" positionLimit={5} onPreview={vi.fn()} />
  </QueryClientProvider>))
}
function order() {
  return [...host.querySelectorAll('tbody td[data-column="symbol"]')].map(cell => cell.textContent)
}
async function tab(index: number) {
  await act(async () => host.querySelectorAll<HTMLButtonElement>('[role="tab"]')[index].click())
}
it('restores five-factor selection rank and saved buy priority without changing sell', async () => {
  const original = structuredClone(rows)
  await render()
  for (const index of [1, 2]) {
    await tab(index)
    expect(order()).toEqual(index === 1 ? expected : buyExpected)
    expect(host.querySelector('th[data-column="atr14_previous_close"]')?.getAttribute('aria-sort')).not.toBe('descending')
  }
  await tab(0)
  expect(order()).toEqual(rows.map(row => row.symbol))
  expect(host.querySelector('th[data-column="atr14_previous_close"]')).toBeNull()
  expect(rows).toEqual(original)
})
it('restores five-factor default after three manual sort clicks without changing saved ranks', async () => {
  await render(); await tab(1)
  const button = host.querySelector<HTMLButtonElement>('th[data-column="score"] button')!
  await act(async () => button.click())
  expect(order()).toEqual(['600002.SH', '600003.SH', '600004.SH', '600001.SH'])
  await act(async () => button.click())
  expect(order()).toEqual(['600001.SH', '600004.SH', '600003.SH', '600002.SH'])
  await act(async () => button.click())
  expect(order()).toEqual(expected)
  expect(rows.map(row => row.selection_rank)).toEqual([1, 8, 3, 2])
})
