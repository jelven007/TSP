// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, it, vi } from 'vitest'
import { Watchlist } from './Watchlist'
import { QK } from '@/lib/queryKeys'
import { api } from '@/lib/api'

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

async function render() {
  vi.spyOn(api, 'watchlistList').mockResolvedValue({ symbols: [
    { symbol: '600000.SH', added_at: '', note: '', group_ids: ['core', 'other'] },
  ] })
  vi.spyOn(api, 'watchlistGroups').mockResolvedValue({ groups: [
    { id: 'core', name: '核心', color: 'sky', referenced_by: ['demo'] },
    { id: 'other', name: '观察', color: 'teal' },
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
  await act(async () => root.render(
    <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
      <QueryClientProvider client={client}><Watchlist /></QueryClientProvider>
    </MemoryRouter>,
  ))
  await vi.waitFor(async () => {
    await act(async () => new Promise(resolve => setTimeout(resolve, 0)))
    expect(host.querySelector('button[aria-label="清空自选"], button[title="清空自选"]')).not.toBeNull()
  })
  return clear
}

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
