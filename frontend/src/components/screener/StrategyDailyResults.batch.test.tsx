// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, it, vi } from 'vitest'
import { StrategyDailyResults } from './StrategyDailyResults'

const calls = vi.hoisted(() => ({ preview: {} as any }))
vi.mock('@/components/StockPreviewDialog', () => ({
  StockPreviewDialog: (props: any) => { calls.preview = props; return null },
}))
vi.mock('@/lib/api', () => ({ api: {
  screenerResultHistoryDetail: async () => ({ summary: { params: { position_limit: 2 }, overrides: {} } }),
} }))
Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
let root: Root
let host: HTMLDivElement
let qc: QueryClient
afterEach(async () => { await act(async () => root.unmount()); host.remove(); qc.clear() })
async function render(extra: any = {}, props: any = {}) {
  host = document.createElement('div'); document.body.append(host)
  root = createRoot(host); qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const rows = [
    { symbol: '600003.SH', name: '三', return_1430: .02, prev_d_amount20: 3e8 },
    { symbol: '600001.SH', name: '一', return_1430: .02, prev_d_amount20: 5e8 },
    { symbol: '600002.SH', name: '二', return_1430: .03, prev_d_amount20: 4e8 },
  ]
  await act(async () => root.render(<QueryClientProvider client={qc}>
    <StrategyDailyResults result={{ as_of: '2026-07-15', strategy: 's02',
      rows, total: 3, elapsed_ms: 0, sections: { select: rows, buy: rows, sell: rows }, ...extra }} {...props} />
  </QueryClientProvider>))
}
function tab(label: string) { return [...document.querySelectorAll<HTMLButtonElement>('[role="tab"]')].find(b => b.textContent?.startsWith(label))! }
function symbols() { return [...document.querySelectorAll('tbody tr')].map(r => r.querySelector('td')?.textContent) }
it('sorts all selected stocks using the buy limit and preserves code/name detail actions', async () => {
  const onPreview = vi.fn()
  await render({}, { positionLimit: 2, onPreview })
  await act(async () => tab('选').click())
  expect(symbols()).toEqual(['600002.SH', '600001.SH', '600003.SH'])
  for (const label of ['卖', '选', '买']) {
    await act(async () => tab(label).click())
    const row = document.querySelector('tbody tr')!
    for (const field of [0, 1]) {
      await act(async () => row.querySelectorAll('td')[field].querySelector('button')!.click())
      expect(onPreview).toHaveBeenLastCalledWith(expect.any(String), expect.any(String),
        expect.arrayContaining([expect.objectContaining({ symbol: '600001.SH' })]))
    }
  }
})
it('uses amount priority when all stocks fit and retains navigation order', async () => {
  await render({}, { positionLimit: 3 })
  await act(async () => tab('选').click())
  expect(symbols()).toEqual(['600001.SH', '600002.SH', '600003.SH'])
  await act(async () => document.querySelector<HTMLButtonElement>('tbody button')!.click())
  expect(calls.preview.navList.map((x: any) => x.symbol)).toEqual(symbols())
})
