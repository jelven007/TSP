// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, it, vi } from 'vitest'
import { WatchlistImportDialog } from './WatchlistImportDialog'
import { api } from '@/lib/api'

const symbols = ['600036.SH', '515880.SH', '300750.SZ']
let root: Root | undefined
let host: HTMLDivElement
let client: QueryClient
Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })

afterEach(async () => {
  await act(async () => root?.unmount())
  root = undefined
  client?.clear()
  host?.remove()
  vi.restoreAllMocks()
})

it.each([false, true])('confirms CSV order after selection changes (existing member: %s)', async existing => {
  vi.spyOn(api, 'watchlistImportCsv').mockResolvedValue({
    provider: 'csv', codes: symbols.map(symbol => symbol.slice(0, 6)),
    candidates: symbols.map(symbol => ({
      code: symbol.slice(0, 6), symbol, name: symbol, matched: true,
      already_in_watchlist: existing && symbol === symbols[1],
    })), matched_count: 3, unmatched_count: 0,
  })
  const batch = vi.spyOn(api, 'watchlistBatchAdd').mockResolvedValue({ symbols: [], added: existing ? 2 : 3 })
  const onClose = vi.fn()
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await act(async () => root!.render(
    <QueryClientProvider client={client}>
      <WatchlistImportDialog open onClose={onClose} groupId="target"
        groups={[{ id: 'target', name: '目标组', color: 'sky' }]}
        existingBySymbol={new Map(existing ? [[symbols[1], ['old']]] : [])} />
    </QueryClientProvider>,
  ))
  const upload = document.querySelector<HTMLInputElement>('input[type="file"]')!
  await act(async () => {
    Object.defineProperty(upload, 'files', {
      value: [new File(['股票代码\n600036\n515880\n300750\n'], 'stocks.csv', { type: 'text/csv' })],
    })
    upload.dispatchEvent(new Event('change', { bubbles: true }))
  })
  const checks = [...document.querySelectorAll<HTMLInputElement>('li input[type="checkbox"]')]
  expect(checks).toHaveLength(3)
  // Existing members are explicitly selected; a deselected/reselected row must keep its CSV position.
  await act(async () => {
    if (existing) checks[1].click()
    else checks[0].click()
  })
  if (!existing) await act(async () => checks[0].click())
  const confirm = [...document.querySelectorAll<HTMLButtonElement>('button')]
    .find(button => button.textContent?.includes('导入所选'))!
  expect(confirm).toBeDefined()
  await act(async () => confirm.click())
  expect(batch).toHaveBeenCalledWith(symbols, '', undefined, ['target'])
  expect(onClose).toHaveBeenCalledOnce()
})
