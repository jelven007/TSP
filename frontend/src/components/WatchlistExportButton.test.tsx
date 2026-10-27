// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, expect, it, vi } from 'vitest'
import { api, type WatchlistEntry } from '@/lib/api'
import { WatchlistExportButton } from './WatchlistExportButton'

const account = vi.hoisted(() => ({ scope: 'account-a' }))
vi.mock('@/lib/accountSession', () => ({ accountScope: () => account.scope }))

const entries: WatchlistEntry[] = [
  { symbol: '000001.SZ', added_at: '', group_ids: ['core', 'other'] },
  { symbol: '600000.SH', added_at: '', group_ids: ['core'] },
  { symbol: '600036.SH', added_at: '', group_ids: ['other'] },
  { symbol: '600519.SH', added_at: '', group_ids: [] },
  { symbol: '300750.SZ', added_at: '' },
]
let root: Root | undefined
let host: HTMLDivElement
let blobs: Blob[]
let downloads: string[]
Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })

afterEach(async () => {
  await act(async () => root?.unmount())
  root = undefined
  host?.remove()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

async function render(selectedGroup = 'all', groupName?: string) {
  account.scope = 'account-a'
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
  await act(async () => root!.render(
    <WatchlistExportButton selectedGroup={selectedGroup} groupName={groupName} />,
  ))
  return list
}

function button() {
  return host.querySelector<HTMLButtonElement>('button')!
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

it('offers an icon-only export button with an accessible name', async () => {
  await render()
  expect(button().textContent).toBe('')
  expect(button().getAttribute('aria-label')).toBe('导出当前分类股票代码')
  expect(button().querySelector('svg')).not.toBeNull()
})

it('exports all codes in saved order with duplicates removed', async () => {
  const list = await render()
  list.mockResolvedValue({ symbols: [...entries, entries[0]] })
  await act(async () => button().click())
  expect(list).toHaveBeenCalledOnce()
  expect(downloads).toEqual(['全部自选股.csv'])
  expect(await csv()).toBe('股票代码\r\n000001.SZ\r\n600000.SH\r\n600036.SH\r\n600519.SH\r\n300750.SZ\r\n')
})

it('exports only ungrouped entries, including legacy entries without group_ids', async () => {
  await render('ungrouped')
  await act(async () => button().click())
  expect(await csv()).toBe('股票代码\r\n600519.SH\r\n300750.SZ\r\n')
  expect(downloads).toEqual(['未分组_股票列表.csv'])
})

it('exports only the selected group, including members shared with other groups', async () => {
  await render('other', '观察/组')
  await act(async () => button().click())
  expect(await csv()).toBe('股票代码\r\n000001.SZ\r\n600036.SH\r\n')
  expect(downloads).toEqual(['观察_组_股票列表.csv'])
  expect(button().title).toContain('观察/组')
})

it('exports the complete freshly read group beyond the visible or cached rows', async () => {
  const list = await render('core', '核心')
  const many = Array.from({ length: 125 }, (_, index) => ({
    symbol: `${600100 + index}.SH`, added_at: '', group_ids: ['core'],
  }))
  list.mockResolvedValue({ symbols: [...many, entries[2], entries[3]] })
  await act(async () => button().click())
  const text = await csv()
  expect(text.split('\r\n').filter(Boolean)).toHaveLength(126)
  expect(text).toContain('600224.SH\r\n')
  expect(text).not.toContain('600036.SH')
  expect(text).not.toContain('600519.SH')
})

it.each(['all', 'ungrouped', 'empty'])('reports an empty %s selection without downloading', async selected => {
  const list = await render(selected, '空组')
  if (selected === 'all') list.mockResolvedValue({ symbols: [] })
  if (selected === 'ungrouped') list.mockResolvedValue({ symbols: entries.slice(0, 3) })
  await act(async () => button().click())
  expect(host.querySelector('[role="alert"]')?.textContent).toContain('没有股票')
  expect(downloads).toEqual([])
})

it('reports read failures without downloading and allows a retry', async () => {
  const list = await render('core', '核心')
  list.mockRejectedValueOnce(new Error('读取自选失败'))
  await act(async () => button().click())
  expect(host.querySelector('[role="alert"]')?.textContent).toBe('读取自选失败')
  expect(downloads).toEqual([])
  expect(button().disabled).toBe(false)
  await act(async () => button().click())
  expect(await csv()).toBe('股票代码\r\n000001.SZ\r\n600000.SH\r\n')
  expect(host.querySelector('[role="alert"]')).toBeNull()
})

it('disables repeated export and keeps the selection from the click while loading', async () => {
  const list = await render('core', '核心')
  let resolve!: (data: { symbols: WatchlistEntry[] }) => void
  list.mockReturnValue(new Promise(done => { resolve = done }))
  await act(async () => button().click())
  expect(button().disabled).toBe(true)
  await act(async () => button().click())
  expect(list).toHaveBeenCalledOnce()
  await act(async () => root!.render(<WatchlistExportButton selectedGroup="other" groupName="观察" />))
  await act(async () => resolve({ symbols: entries }))
  expect(await csv()).toBe('股票代码\r\n000001.SZ\r\n600000.SH\r\n')
  expect(downloads).toEqual(['核心_股票列表.csv'])
  expect(button().disabled).toBe(false)
})

it('ignores a late response after the account changes', async () => {
  const list = await render('core', '核心')
  let resolve!: (data: { symbols: WatchlistEntry[] }) => void
  list.mockReturnValue(new Promise(done => { resolve = done }))
  await act(async () => button().click())
  account.scope = 'account-b'
  await act(async () => resolve({ symbols: entries }))
  expect(downloads).toEqual([])
})

it('ignores a late response after unmount', async () => {
  const list = await render('core', '核心')
  let resolve!: (data: { symbols: WatchlistEntry[] }) => void
  list.mockReturnValue(new Promise(done => { resolve = done }))
  await act(async () => button().click())
  await act(async () => root!.unmount())
  root = undefined
  await act(async () => resolve({ symbols: entries }))
  expect(downloads).toEqual([])
})
