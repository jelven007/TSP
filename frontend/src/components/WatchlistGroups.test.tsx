// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, it, vi } from 'vitest'
import { WatchlistGroupBar } from './WatchlistGroups'
import { api, type WatchlistEntry, type WatchlistGroup } from '@/lib/api'
import { QK } from '@/lib/queryKeys'

vi.mock('@/lib/useSharedQueries', () => ({ usePreferences: () => ({ data: {} }) }))
vi.mock('@/lib/accountSession', () => ({ hasPermission: () => false }))

const groups: WatchlistGroup[] = [
  { id: 'core', name: '核心/观察:组', color: 'sky', referenced_by: ['demo'] },
  { id: 'other', name: '其他', color: 'teal' },
  { id: 'empty', name: '空组', color: 'blue' },
]
const entries: WatchlistEntry[] = [
  { symbol: '000001.SZ', name: '平安银行', added_at: '', group_ids: ['core', 'other'] },
  { symbol: '600000.SH', name: '浦发,"银行"\n第二行', added_at: '', group_ids: ['core'] },
  { symbol: '300750.SZ', added_at: '', group_ids: ['core'] },
  { symbol: '600036.SH', name: '招商银行', added_at: '', group_ids: ['other'] },
  { symbol: '600519.SH', name: '贵州茅台', added_at: '' },
]
let root: Root | undefined
let host: HTMLDivElement
let client: QueryClient
let blobs: Blob[]
let downloads: string[]
Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })

afterEach(async () => {
  await act(async () => root?.unmount())
  root = undefined
  client?.clear()
  host?.remove()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

async function render(selected = 'all') {
  blobs = []
  downloads = []
  vi.stubGlobal('URL', class extends URL {
    static createObjectURL = vi.fn((blob: Blob) => {
      blobs.push(blob)
      return 'blob:watchlist-export'
    })
    static revokeObjectURL = vi.fn()
  })
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
    downloads.push(this.download)
  })
  const list = vi.spyOn(api, 'watchlistList').mockResolvedValue({ symbols: entries })
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  // The export must refresh the complete list even when a previous view is cached.
  client.setQueryData(QK.watchlist, { symbols: [entries[0]] })
  await act(async () => root!.render(
    <QueryClientProvider client={client}>
      <WatchlistGroupBar groups={groups} counts={{ core: 3, other: 2, empty: 0 }}
        selected={selected} total={5} onSelect={vi.fn()} onCreate={vi.fn()}
        onRename={vi.fn()} onDelete={vi.fn()} />
    </QueryClientProvider>,
  ))
  await act(async () => host.querySelector<HTMLButtonElement>('[aria-label="管理自选分组"]')!.click())
  return list
}

function exportButton() {
  const button = [...document.querySelectorAll<HTMLButtonElement>('[role="dialog"] button')]
    .find(node => /导出 CSV|导出中/.test(node.textContent ?? ''))
  expect(button, 'The group manager should offer CSV export').toBeDefined()
  return button!
}

async function select(groupId: string) {
  const radio = document.querySelector<HTMLInputElement>(`input[type="radio"][value="${groupId}"]`)
  expect(radio, 'A group can be selected for export').not.toBeNull()
  await act(async () => radio!.click())
}

async function csv() {
  const buffer = await new Promise<ArrayBuffer>((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(reader.result as ArrayBuffer)
    reader.onerror = () => reject(reader.error)
    reader.readAsArrayBuffer(blobs[0])
  })
  expect([...new Uint8Array(buffer).slice(0, 3)]).toEqual([0xef, 0xbb, 0xbf])
  expect(blobs[0].type).toBe('text/csv;charset=utf-8')
  return new TextDecoder().decode(buffer)
}

it('requires a selection and exports only that group, including shared members', async () => {
  const list = await render()
  expect(exportButton().disabled).toBe(true)
  expect(list).not.toHaveBeenCalled()
  await select('other')
  expect(exportButton().disabled).toBe(false)
  await act(async () => exportButton().click())
  expect(list).toHaveBeenCalledOnce()
  expect(downloads).toEqual(['其他_股票列表.csv'])
  expect(await csv()).toBe('股票代码\r\n000001.SZ\r\n600036.SH\r\n')
})

it('preselects the active group and exports only codes with leading zeros and exchange suffixes', async () => {
  await render('core')
  expect(document.querySelector<HTMLInputElement>('input[value="core"]')?.checked).toBe(true)
  await act(async () => exportButton().click())
  expect(downloads).toEqual(['核心_观察_组_股票列表.csv'])
  expect(await csv()).toBe('股票代码\r\n000001.SZ\r\n600000.SH\r\n300750.SZ\r\n')
})

it('exports the full group beyond the cached or visible rows', async () => {
  const list = await render('core')
  const many = Array.from({ length: 125 }, (_, index) => ({
    symbol: `${600100 + index}.SH`, name: `股票${index}`, added_at: '', group_ids: ['core'],
  }))
  list.mockResolvedValue({ symbols: [...many, entries[3], entries[4]] })
  await act(async () => exportButton().click())
  const text = await csv()
  expect(text.split('\r\n').filter(Boolean)).toHaveLength(126)
  expect(text).toContain('600224.SH\r\n')
  expect(text).not.toContain(',')
  expect(text).not.toContain('股票124')
  expect(text).not.toContain('600036.SH')
  expect(text).not.toContain('600519.SH')
})

it('shows an empty-group message without downloading a misleading file', async () => {
  await render('empty')
  await act(async () => exportButton().click())
  expect(document.querySelector('[role="alert"]')?.textContent).toContain('没有股票')
  expect(downloads).toEqual([])
})

it('reports read failures and never exports a stale cached list', async () => {
  const list = await render('core')
  list.mockRejectedValue(new Error('读取自选失败'))
  await act(async () => exportButton().click())
  expect(document.querySelector('[role="alert"]')?.textContent).toBe('读取自选失败')
  expect(downloads).toEqual([])
  expect(exportButton().disabled).toBe(false)
})

it('disables selection and repeated export while the complete list is loading', async () => {
  const list = await render('core')
  let resolve!: (data: { symbols: WatchlistEntry[] }) => void
  list.mockReturnValue(new Promise(done => { resolve = done }))
  await act(async () => exportButton().click())
  expect(exportButton().textContent).toContain('导出中')
  expect(exportButton().disabled).toBe(true)
  expect(document.querySelector<HTMLInputElement>('input[value="other"]')?.disabled).toBe(true)
  await act(async () => exportButton().click())
  expect(list).toHaveBeenCalledOnce()
  await act(async () => resolve({ symbols: entries }))
  expect(downloads).toHaveLength(1)
})

it('does not download a late response after the dialog or account context is unmounted', async () => {
  const list = await render('core')
  let resolve!: (data: { symbols: WatchlistEntry[] }) => void
  list.mockReturnValue(new Promise(done => { resolve = done }))
  await act(async () => exportButton().click())
  await act(async () => root!.unmount())
  root = undefined
  await act(async () => resolve({ symbols: entries }))
  expect(downloads).toEqual([])
})
