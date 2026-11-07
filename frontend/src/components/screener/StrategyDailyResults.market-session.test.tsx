// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, it, vi } from 'vitest'
import { StrategyDailyResults } from './StrategyDailyResults'
import type { ScreenerResult } from '@/lib/api'
import { defaultStrategyResultColumns, strategyResultGroups } from '@/lib/strategy-result-columns'

const { snapshot } = vi.hoisted(() => ({ snapshot: vi.fn() }))
vi.mock('@/lib/api', () => ({ api: { marketSnapshot: snapshot } }))
vi.mock('@/components/StockPreviewDialog', () => ({ StockPreviewDialog: () => null }))
Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
let root: Root
let host: HTMLDivElement
let client: QueryClient
const row = { symbol: '600001.SH', atr14_previous_close: .05, atr_rank: 1, atr_rank_score: 100, score: 100 }
const result: ScreenerResult = {
  strategy: 'custom_mvxxcp88', as_of: '2026-11-06', rows: [], total: 0, elapsed_ms: 0,
  section_mode: 'signals', sections: { sell: [row], select: [row], buy: [row] },
}
const quotes = { as_of: '2026-11-06', requested_as_of: '2026-11-07', source: 'previous_close',
  rows: [{ symbol: row.symbol, raw_close: 12.5, close: 6.25, change_pct: .02,
    turnover_rate: 3.45, vol_ratio_5d: 1.8 }] }
afterEach(async () => {
  await act(async () => root?.unmount())
  client?.clear(); host?.remove(); vi.useRealTimers()
})
async function render(tab: 'sell' | 'select' | 'buy', data: unknown = quotes) {
  vi.useFakeTimers()
  vi.setSystemTime(new Date('2026-11-07T12:00:00+08:00'))
  snapshot.mockReset().mockResolvedValue(data)
  host = document.createElement('div'); document.body.append(host); root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await act(async () => root.render(<QueryClientProvider client={client}>
    <StrategyDailyResults result={result} activeTab={tab} signalColumns={defaultStrategyResultColumns(tab)} />
  </QueryClientProvider>))
  await act(async () => { await vi.advanceTimersByTimeAsync(20) })
}

it.each(['sell', 'select', 'buy'] as const)('removes ATR rank only for S02 %s without losing metrics', async tab => {
  await render(tab)
  expect(host.querySelector('th[data-column="atr_rank"]')).toBeNull()
  expect(host.querySelector('td[data-column="atr14_previous_close"]')?.textContent).toBe('5.00%')
  expect(host.querySelector(`td[data-column="${tab === 'select' ? 'atr_rank_score' : 'score'}"]`)?.textContent).toBe('100.00')
  expect(strategyResultGroups(tab, 'custom_mvxxcp88')[0].keys).not.toContain('atr_rank')
  expect(defaultStrategyResultColumns(tab, 'custom_other').map(col => col.id)).toContain('builtin:atr_rank')
})

it('shows officially closed-session quotes with their date and correct units', async () => {
  await render('select')
  expect(host.querySelector('td[data-column="live_price"]')?.textContent).toBe('12.50')
  expect(host.querySelector('td[data-column="live_change_pct"]')?.textContent).toBe('2.00%')
  expect(host.querySelector('td[data-column="live_turnover"]')?.textContent).toBe('3.45%')
  expect(host.querySelector('td[data-column="live_vol_ratio"]')?.textContent).toBe('1.80')
  expect(host.textContent).toContain('行情日期：2026-11-06')
  expect(host.textContent).toContain('收盘数据')
})

it('rejects unmarked stale quotes during the weekend', async () => {
  await render('select', { as_of: quotes.as_of, rows: quotes.rows })
  expect(host.querySelector('td[data-column="live_price"]')?.textContent).toBe('—')
})

it('rejects a previous-close response belonging to another requested day', async () => {
  await render('select', { ...quotes, requested_as_of: '2026-11-06' })
  expect(host.querySelector('td[data-column="live_price"]')?.textContent).toBe('—')
})
