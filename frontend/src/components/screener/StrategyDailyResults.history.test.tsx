// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { StrategyDailyResults } from './StrategyDailyResults'
import type { ScreenerResult } from '@/lib/api'

const { snapshot, preview } = vi.hoisted(() => ({ snapshot: vi.fn(), preview: vi.fn() }))
vi.mock('@/lib/api', () => ({ api: { marketSnapshot: snapshot } }))
vi.mock('@/components/StockPreviewDialog', () => ({ StockPreviewDialog: () => null }))
Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
let root: Root
let host: HTMLDivElement
let client: QueryClient
beforeEach(() => {
  vi.useFakeTimers()
  vi.setSystemTime(new Date('2026-11-08T10:00:00+08:00'))
  snapshot.mockReset().mockResolvedValue({ as_of: '2026-11-08', rows: [] })
  preview.mockReset()
})
afterEach(async () => {
  await act(async () => root?.unmount())
  client?.clear(); host?.remove(); vi.useRealTimers(); vi.restoreAllMocks()
})
async function render(count: number) {
  const rows = Array.from({ length: count }, (_, index) => ({
    symbol: `${600000 + index}.SH`, name: `股票${index}`, score: index,
  }))
  const result: ScreenerResult = { as_of: '2026-06-22', strategy: 'custom_mvxxcp88',
    rows, total: count, elapsed_ms: 0, section_mode: 'signals',
    sections: { sell: rows, select: rows, buy: rows } }
  vi.spyOn(HTMLElement.prototype, 'offsetHeight', 'get').mockReturnValue(480)
  vi.spyOn(HTMLElement.prototype, 'offsetWidth', 'get').mockReturnValue(1024)
  host = document.createElement('div'); document.body.append(host); root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await act(async () => root.render(<QueryClientProvider client={client}>
    <StrategyDailyResults result={result} onPreview={preview} />
  </QueryClientProvider>))
  await act(async () => { await vi.advanceTimersByTimeAsync(20) })
}

it('reads historical saved rows without requesting or polling current quotes', async () => {
  await render(1)
  await act(async () => { await vi.advanceTimersByTimeAsync(3010) })
  expect(snapshot).not.toHaveBeenCalled()
  expect(host.textContent).toContain('历史保存结果')
  expect(host.querySelector('td[data-column="live_price"]')?.textContent).toBe('—')
})

it('renders a bounded viewport while keeping every member in counts and preview navigation', async () => {
  await render(1200)
  expect(host.querySelectorAll('tbody tr[data-row-index]').length).toBeGreaterThan(0)
  expect(host.querySelectorAll('tbody tr').length).toBeLessThan(60)
  expect(host.querySelector('[role="tablist"]')?.textContent).toContain('卖 · 1200')
  await act(async () => host.querySelector<HTMLButtonElement>('tbody button')!.click())
  expect(preview.mock.calls[0][2]).toHaveLength(1200)
  expect(preview.mock.calls[0][2][1199].symbol).toBe('601199.SH')
  const panel = host.querySelector<HTMLElement>('[role="tabpanel"]')!
  await act(async () => {
    panel.scrollTop = 32000
    panel.dispatchEvent(new Event('scroll'))
    await vi.advanceTimersByTimeAsync(20)
  })
  expect(Number(host.querySelector('tr[data-row-index]')?.getAttribute('data-row-index'))).toBeGreaterThan(900)
})
