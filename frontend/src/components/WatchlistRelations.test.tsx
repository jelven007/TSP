// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, it, vi } from 'vitest'
import { api, type WatchlistEntry } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { WatchlistRelations, WatchlistStrategyDialog } from './WatchlistRelations'
import { WatchlistExportButton } from './WatchlistExportButton'

Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
let root: Root | undefined
let host: HTMLDivElement
let client: QueryClient

afterEach(async () => {
  await act(async () => root?.unmount())
  root = undefined
  host?.remove()
  client?.clear()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

async function render(content: React.ReactNode) {
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
  await act(async () => root!.render(<QueryClientProvider client={client}>{content}</QueryClientProvider>))
}

const entry: WatchlistEntry = {
  symbol: '000001.SZ', added_at: '', group_ids: ['a', 'b'], strategy_ids: ['daily', 'minute'],
}
const strategies = [{ id: 'daily', name: '日K策略' }, { id: 'minute', name: '分钟策略' }]

it('shows all group and strategy names together with an edit action', async () => {
  const edit = vi.fn()
  await render(<WatchlistRelations entry={entry} groups={[
    { id: 'a', name: '核心', color: 'sky' }, { id: 'b', name: '观察', color: 'rose' },
  ]} strategies={strategies} onEdit={edit} />)
  for (const name of ['核心', '观察', '日K策略', '分钟策略']) expect(host.textContent).toContain(name)
  await act(async () => host.querySelector<HTMLButtonElement>('button')!.click())
  expect(edit).toHaveBeenCalledOnce()
})

it('saves multiple strategies and can remove an unavailable association', async () => {
  vi.spyOn(api, 'strategyList').mockResolvedValue({ strategies: strategies as any })
  const save = vi.spyOn(api, 'watchlistSetStrategies').mockResolvedValue({ symbols: [entry] })
  const saved = vi.fn()
  await render(<WatchlistStrategyDialog entry={{ ...entry, strategy_ids: ['missing'] }}
    onClose={vi.fn()} onSaved={saved} />)
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 10)) })
  expect(client.getQueryData(QK.watchlistStrategyOptions())).toBeDefined()
  const labels = [...document.querySelectorAll('label')]
  expect(document.body.textContent).toContain('策略不可用')
  await act(async () => {
    labels.find(label => label.textContent?.includes('日K策略'))!.querySelector('input')!.click()
    labels.find(label => label.textContent?.includes('分钟策略'))!.querySelector('input')!.click()
    labels.find(label => label.textContent?.includes('策略不可用'))!.querySelector('input')!.click()
  })
  await act(async () => {
    [...document.querySelectorAll<HTMLButtonElement>('button')].find(button => button.textContent === '保存')!.click()
  })
  expect(save).toHaveBeenCalledWith('000001.SZ', ['daily', 'minute'])
  expect(saved).toHaveBeenCalledOnce()
})

it('keeps the dialog and selection on a failed save', async () => {
  vi.spyOn(api, 'strategyList').mockResolvedValue({ strategies: strategies as any })
  vi.spyOn(api, 'watchlistSetStrategies').mockRejectedValue(new Error('保存失败'))
  const saved = vi.fn()
  await render(<WatchlistStrategyDialog entry={entry} onClose={vi.fn()} onSaved={saved} />)
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 10)) })
  await act(async () => {
    [...document.querySelectorAll<HTMLButtonElement>('button')].find(button => button.textContent === '保存')!.click()
  })
  expect(document.body.textContent).toContain('保存失败')
  expect(saved).not.toHaveBeenCalled()
  expect(document.querySelectorAll('input:checked')).toHaveLength(2)
})

it('ignores a strategy save response after unmount', async () => {
  vi.spyOn(api, 'strategyList').mockResolvedValue({ strategies: strategies as any })
  let resolve!: (value: { symbols: WatchlistEntry[] }) => void
  vi.spyOn(api, 'watchlistSetStrategies').mockImplementation(() => new Promise(done => { resolve = done }))
  const saved = vi.fn()
  await render(<WatchlistStrategyDialog entry={entry} onClose={vi.fn()} onSaved={saved} />)
  await act(async () => { await new Promise(done => setTimeout(done, 10)) })
  await act(async () => {
    [...document.querySelectorAll<HTMLButtonElement>('button')].find(button => button.textContent === '保存')!.click()
  })
  await act(async () => root!.unmount())
  root = undefined
  await act(async () => resolve({ symbols: [entry] }))
  expect(saved).not.toHaveBeenCalled()
})

it('exports the complete fetched list with one code column, BOM, CRLF and deduplication', async () => {
  vi.spyOn(api, 'watchlistList').mockResolvedValue({ symbols: [
    entry, { symbol: '600000.SH', added_at: '', group_ids: ['other'] }, entry,
  ] })
  let blob!: Blob
  vi.stubGlobal('URL', {
    createObjectURL: vi.fn((value: Blob) => { blob = value; return 'blob:test' }), revokeObjectURL: vi.fn(),
  })
  const download = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
  await render(<WatchlistExportButton selectedGroup="all" />)
  await act(async () => host.querySelector<HTMLButtonElement>('button')!.click())
  const text = await new Promise<string>(resolve => {
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result))
    reader.readAsText(blob)
  })
  // FileReader decodes and strips UTF-8 BOM; inspect raw bytes separately.
  const bytes = await new Promise<Uint8Array>(resolve => {
    const reader = new FileReader()
    reader.onload = () => resolve(new Uint8Array(reader.result as ArrayBuffer))
    reader.readAsArrayBuffer(blob)
  })
  expect([...bytes.slice(0, 3)]).toEqual([239, 187, 191])
  expect(text).toBe('股票代码\r\n000001.SZ\r\n600000.SH\r\n')
  expect(download).toHaveBeenCalledOnce()
})

it.each(['empty', 'error', 'unmount'])('does not download for %s', async mode => {
  let resolve!: (value: { symbols: WatchlistEntry[] }) => void
  vi.spyOn(api, 'watchlistList').mockImplementation(() => mode === 'error'
    ? Promise.reject(new Error('读取失败'))
    : new Promise(done => { resolve = done }))
  const download = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
  await render(<WatchlistExportButton selectedGroup="all" />)
  await act(async () => host.querySelector<HTMLButtonElement>('button')!.click())
  if (mode !== 'error') {
    expect(host.querySelector<HTMLButtonElement>('button')!.disabled).toBe(true)
    if (mode === 'unmount') {
      await act(async () => root!.unmount())
      root = undefined
    }
    await act(async () => resolve({ symbols: mode === 'empty' ? [] : [entry] }))
  }
  expect(download).not.toHaveBeenCalled()
  if (mode !== 'unmount') expect(host.querySelector('[role="alert"]')).not.toBeNull()
})
