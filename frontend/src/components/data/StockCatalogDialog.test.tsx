// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { StockCatalogDialog } from './StockCatalogDialog'
import type { StockCatalogRow } from '@/lib/api'

const state = vi.hoisted(() => ({ rows: [] as StockCatalogRow[], fail: false, writes: [] as string[] }))
vi.mock('@/lib/api', () => ({
  api: {
    stockCatalog: async (q: string, exchange: string) => {
      const items = state.rows.filter(row => (!q || row.name.includes(q) || row.symbol.includes(q)) && (!exchange || row.exchange === exchange))
      return { items, total: items.length, page: 1, page_size: 50 }
    },
    createStockCatalogRow: async (body: StockCatalogRow) => {
      if (state.fail) throw new Error('仅管理员可以管理个股维表')
      state.writes.push(body.symbol)
      state.rows.push({ ...body, exchange: 'SH', board: '沪主板', snapshot_source: 'manual' })
      return body
    },
    updateStockCatalogRow: async (symbol: string, body: StockCatalogRow) => {
      state.writes.push(symbol)
      state.rows = state.rows.map(row => row.symbol === symbol ? { ...row, ...body } : row)
      return body
    },
    deleteStockCatalogRow: async (symbol: string) => {
      state.writes.push(symbol)
      state.rows = state.rows.filter(row => row.symbol !== symbol)
      return { deleted: symbol }
    },
  },
}))
let host: HTMLDivElement
let root: Root
let client: QueryClient
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  state.rows = [{
    symbol: '600000.SH', code: '600000', name: '浦发银行', exchange: 'SH', board: '沪主板',
    listing_date: '1999-11-10', region: 'CN', industry: null, total_shares: 1000,
    float_shares: 800, tick_size: null, as_of: '2026-10-24', snapshot_source: 'sse',
  }]
  state.fail = false; state.writes = []
  host = document.createElement('div'); document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
})
afterEach(async () => {
  await act(async () => root.unmount())
  client.clear(); host.remove()
})
async function settle() {
  for (let i = 0; i < 5; i++) await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
}
async function render() {
  await act(async () => root.render(<QueryClientProvider client={client}><StockCatalogDialog onClose={() => {}} /></QueryClientProvider>))
  await settle()
}
async function click(label: string) {
  const button = Array.from(host.querySelectorAll('button')).find(el => el.getAttribute('aria-label') === label || el.textContent === label)
  expect(button).toBeDefined()
  await act(async () => button!.click())
  await settle()
}
async function fill(label: string, value: string) {
  const input = host.querySelector<HTMLInputElement>(`input[aria-label="${label}"]`)!
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, value)
    input.dispatchEvent(new Event('input', { bubbles: true }))
  })
}
it('resets create/edit state, saves changes and requires confirmation before deleting', async () => {
  await render()
  expect(host.textContent).toContain('浦发银行')
  await click('编辑 600000.SH')
  expect(host.querySelector<HTMLInputElement>('[aria-label="股票代码"]')?.disabled).toBe(true)
  await click('取消编辑')
  await click('新增股票')
  expect(host.querySelector<HTMLInputElement>('[aria-label="股票名称"]')?.value).toBe('')
  await fill('股票代码', '600001.SH'); await fill('股票名称', '新增示例')
  await click('保存')
  expect(host.textContent).toContain('新增示例')
  expect(host.querySelector('form')).toBeNull()
  await click('编辑 600001.SH'); await fill('股票名称', '*ST示例'); await click('保存')
  expect(host.textContent).toContain('*ST示例')
  const before = state.writes.length
  await click('删除 600001.SH')
  expect(state.writes).toHaveLength(before)
  await click('取消删除')
  expect(state.rows).toHaveLength(2)
  await click('删除 600001.SH'); await click('确认删除')
  expect(state.rows).toHaveLength(1)
  expect(host.textContent).not.toContain('*ST示例')
})
it('keeps unsaved fields and shows permission errors when the server rejects a write', async () => {
  await render(); await click('新增股票')
  await fill('股票代码', '600001.SH'); await fill('股票名称', '未保存')
  state.fail = true
  await click('保存')
  expect(host.querySelector('[role="alert"]')?.textContent).toContain('仅管理员')
  expect(host.querySelector<HTMLInputElement>('[aria-label="股票名称"]')?.value).toBe('未保存')
  expect(state.rows).toHaveLength(1)
})
