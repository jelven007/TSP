// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, it, vi } from 'vitest'
import { StrategyDailyResults } from './StrategyDailyResults'
import type { ScreenerResult } from '@/lib/api'
import { defaultStrategyResultColumns } from '@/lib/strategy-result-columns'

vi.mock('@/lib/api', () => ({ api: { marketSnapshot: async () => ({
  as_of: '2026-11-05',
  rows: [{ symbol: '600001.SH', turnover_rate: 9.87 }],
}) } }))
vi.mock('@/components/StockPreviewDialog', () => ({ StockPreviewDialog: () => null }))
Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
let root: Root
let host: HTMLDivElement
let client: QueryClient
afterEach(async () => {
  await act(async () => root?.unmount())
  client?.clear()
  host?.remove()
})
async function render(row: Record<string, unknown>) {
  const result: ScreenerResult = {
    strategy: 'custom_mvxxcp88', as_of: '2026-11-05', rows: [], total: 0, elapsed_ms: 0,
    section_mode: 'signals', sections: { sell: [], buy: [], select: [
      { symbol: '600001.SH', reference_date: '2026-11-04', reference_change_pct: .02,
        atr14_previous_close: .12, atr_rank: 1, atr_rank_score: 50, ...row },
    ] },
  }
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await act(async () => {
    root.render(<QueryClientProvider client={client}>
      <StrategyDailyResults result={result} currentDate={result.as_of} activeTab="select"
        signalColumns={defaultStrategyResultColumns('select')} />
    </QueryClientProvider>)
  })
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)) })
}

it('replaces the three S02 selection columns even with old column preferences', async () => {
  await render({ reference_turnover_rate: 2.1, turnover_rate: 90, score: 80, amount_mean_20d: 10 })
  const keys = [...host.querySelectorAll('th')].map(th => th.dataset.column)
  expect(keys).not.toContain('selection_rank')
  expect(keys).not.toContain('amount_mean_20d')
  expect(keys).not.toContain('score')
  expect(keys[keys.indexOf('reference_change_pct') + 1]).toBe('reference_turnover_rate')
  expect(host.querySelector('th[data-column="reference_turnover_rate"]')?.textContent).toBe('参考日换手率')
  expect(host.querySelector('td[data-column="reference_turnover_rate"]')?.textContent).toBe('2.10%')
  expect(host.querySelector('td[data-column="live_turnover"]')?.textContent).toBe('9.87%')
  expect(host.querySelector('td[data-column="atr_rank_score"]')?.textContent).toBe('50.00')
})

it('does not fill a missing previous-session turnover from target-session rows', async () => {
  await render({ date: '2026-11-05', turnover_rate: 90 })
  expect(host.querySelector('td[data-column="reference_turnover_rate"]')?.textContent).toBe('—')
})

it('reads an old frozen source-session turnover without changing its snapshot', async () => {
  const row = { date: '2026-11-04', turnover_rate: 3.1 }
  await render(row)
  expect(host.querySelector('td[data-column="reference_turnover_rate"]')?.textContent).toBe('3.10%')
  expect(row).toEqual({ date: '2026-11-04', turnover_rate: 3.1 })
})
