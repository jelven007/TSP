// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, it, vi } from 'vitest'
import { Watchlist } from './Watchlist'
import { QK } from '@/lib/queryKeys'
import { api, type WatchlistEntry } from '@/lib/api'
import * as watchlistCsv from '@/lib/watchlistCsv'

vi.mock('@/lib/useSharedQueries', () => ({
  usePreferences: () => ({ data: {} }),
  useCapabilities: () => ({ data: { capabilities: {} } }),
  useQuoteStatus: () => ({ data: { running: false } }),
}))
vi.mock('@/components/StockPreviewDialog', () => ({ StockPreviewDialog: () => null }))
vi.mock('@/components/ColumnCustomizer', () => ({ ColumnCustomizer: () => null }))
vi.mock('@/components/WatchlistImportDialog', () => ({ WatchlistImportDialog: () => null }))
vi.mock('@/extensions/ExtensionSlot', () => ({ ExtensionSlot: () => null }))

let root: Root
let host: HTMLDivElement
let client: QueryClient
Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
afterEach(async () => {
  await act(async () => root?.unmount())
  client?.clear()
  host?.remove()
  vi.restoreAllMocks()
})

async function render(cached = false, initialEntry = '/watchlist', entries: WatchlistEntry[] = [
  { symbol: '600000.SH', added_at: '', note: '', group_ids: ['core', 'other'], strategy_ids: ['daily', 'minute'] },
]) {
  vi.spyOn(api, 'watchlistList').mockResolvedValue({ symbols: entries })
  vi.spyOn(api, 'strategyList').mockResolvedValue({ strategies: [
    { id: 'daily', name: '日K策略' }, { id: 'minute', name: '分钟策略' },
  ] as any })
  vi.spyOn(api, 'watchlistGroups').mockResolvedValue({ groups: [
    { id: 'core', name: '核心', color: 'sky', referenced_by: ['demo'] },
    { id: 'other', name: '观察', color: 'teal' },
    { id: 'empty', name: '空组', color: 'blue' },
  ] })
  vi.spyOn(api, 'watchlistEnriched').mockResolvedValue({
    rows: [{ symbol: '600000.SH', name: '浦发银行', close: 10 }],
    as_of: '2026-10-23', elapsed_ms: 0,
  })
  vi.spyOn(api, 'watchlistColumns').mockResolvedValue({ columns: [] })
  vi.spyOn(api, 'klineDailyBatch').mockResolvedValue({ data: {} })
  const clear = vi.spyOn(api, 'watchlistClear').mockResolvedValue({ removed: 1 })
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  if (cached) client.setQueryData(QK.watchlist, { symbols: [
    { symbol: '600000.SH', added_at: '', note: '', group_ids: ['core', 'other'], strategy_ids: [] },
  ] })
  await act(async () => root.render(
    <MemoryRouter initialEntries={[initialEntry]} future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
      <QueryClientProvider client={client}><Watchlist /></QueryClientProvider>
    </MemoryRouter>,
  ))
  await vi.waitFor(async () => {
    await act(async () => new Promise(resolve => setTimeout(resolve, 0)))
    expect(host.querySelector('button[aria-label="清空自选"], button[title="清空自选"]')).not.toBeNull()
  })
  return clear
}

it('revalidates a recently persisted list on mount so saved strategies survive an immediate reload', async () => {
  await render(true)
  await vi.waitFor(async () => {
    await act(async () => new Promise(resolve => setTimeout(resolve, 0)))
    expect(api.watchlistList).toHaveBeenCalled()
    expect(host.textContent).toContain('日K策略')
    expect(host.textContent).toContain('分钟策略')
  })
})

it('blocks clearing all watchlist entries while any group is referenced, across tabs', async () => {
  const clearApi = await render()
  const clear = () => host.querySelector<HTMLButtonElement>('button[aria-label="清空自选"], button[title="清空自选"]')!
  expect(clear().disabled).toBe(true)
  expect(clear().title).toContain('被策略引用')
  await act(async () => clear().click())
  expect(document.querySelector('[role="dialog"]')).toBeNull()
  const other = [...host.querySelectorAll<HTMLButtonElement>('[role="tab"]')]
    .find(button => button.textContent?.includes('观察'))!
  await act(async () => other.click())
  expect(clear().disabled).toBe(true)
  expect(host.querySelector<HTMLButtonElement>('[aria-label="清空当前分组"]')?.disabled).toBe(false)
  expect(clearApi).not.toHaveBeenCalled()
  await act(async () => client.setQueryData(QK.watchlistGroups, { groups: [
    { id: 'core', name: '核心', color: 'sky' }, { id: 'other', name: '观察', color: 'teal' },
  ] }))
  await vi.waitFor(async () => {
    await act(async () => new Promise(resolve => setTimeout(resolve, 0)))
    expect(clear().disabled).toBe(false)
  })
  await act(async () => clear().click())
  const confirm = [...document.querySelectorAll<HTMLButtonElement>('button')]
    .find(button => button.textContent === '确认清空')!
  await act(async () => confirm.click())
  expect(clearApi).toHaveBeenCalledOnce()
})

const exportEntries: WatchlistEntry[] = [
  { symbol: '000001.SZ', added_at: '', group_ids: ['core', 'other'] },
  { symbol: '600000.SH', added_at: '', group_ids: ['core'] },
  { symbol: '600036.SH', added_at: '', group_ids: ['other'] },
  { symbol: '600519.SH', added_at: '' },
]

it('exports the active all, ungrouped or custom tab independently of quote readiness', async () => {
  const download = vi.spyOn(watchlistCsv, 'downloadWatchlistCsv').mockImplementation(() => {})
  await render(false, '/watchlist', exportEntries)
  await vi.waitFor(() => expect(host.querySelector('[role="tab"][aria-selected="true"]')?.textContent).toContain('全部'))
  const exportButton = () => host.querySelector<HTMLButtonElement>('[aria-label="导出当前分类股票代码"]')!
  expect(exportButton()).not.toBeNull()
  await act(async () => exportButton().click())
  expect(download).toHaveBeenLastCalledWith(exportEntries, '全部自选股.csv')
  for (const [name, selectedEntries, filename] of [
    ['未分组', [exportEntries[3]], '未分组_股票列表.csv'],
    ['核心', exportEntries.slice(0, 2), '核心_股票列表.csv'],
    ['观察', [exportEntries[0], exportEntries[2]], '观察_股票列表.csv'],
  ] as const) {
    const tab = [...host.querySelectorAll<HTMLButtonElement>('[role="tab"]')]
      .find(node => node.textContent?.includes(name))!
    await act(async () => tab.click())
    expect(tab.getAttribute('aria-selected')).toBe('true')
    await act(async () => exportButton().click())
    expect(download).toHaveBeenLastCalledWith(selectedEntries, filename)
  }
})

it('uses the group selected by the page URL for the first export', async () => {
  const download = vi.spyOn(watchlistCsv, 'downloadWatchlistCsv').mockImplementation(() => {})
  await render(false, '/watchlist?group=other', exportEntries)
  await vi.waitFor(() => expect(host.querySelector('[role="tab"][aria-selected="true"]')?.textContent).toContain('观察'))
  const button = host.querySelector<HTMLButtonElement>('[aria-label="导出当前分类股票代码"]')
  expect(button).not.toBeNull()
  await act(async () => button!.click())
  expect(download).toHaveBeenCalledWith([exportEntries[0], exportEntries[2]], '观察_股票列表.csv')
})
