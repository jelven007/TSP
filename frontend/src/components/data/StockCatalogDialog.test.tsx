// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { StockCatalogDialog } from './StockCatalogDialog'
import { api, type StockCatalogRow } from '@/lib/api'

const state = vi.hoisted(() => ({ rows: [] as StockCatalogRow[], fail: false, writes: [] as string[] }))
vi.mock('@/lib/api', () => ({
  api: {
    stockCatalog: vi.fn(async (q: string, exchange: string, page = 1, pageSize = 50, board = '') => {
      const items = state.rows.filter(row => (!q || row.name.includes(q) || row.symbol.includes(q)) && (!exchange || row.exchange === exchange) && (!board || row.board === board))
      return { items: items.slice((page - 1) * pageSize, page * pageSize), total: items.length, page, page_size: pageSize }
    }),
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
let mount: HTMLDivElement
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
  vi.mocked(api.stockCatalog).mockClear()
  mount = document.createElement('div'); document.body.append(mount)
  root = createRoot(mount)
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
})
afterEach(async () => {
  await act(async () => root.unmount())
  client.clear(); mount.remove()
})
async function settle() {
  for (let i = 0; i < 5; i++) await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
}
async function render() {
  await act(async () => root.render(<QueryClientProvider client={client}><StockCatalogDialog onClose={() => {}} /></QueryClientProvider>))
  await settle()
  host = document.querySelector<HTMLDivElement>('[role="dialog"]')!
  expect(host).not.toBeNull()
}
async function click(label: string) {
  const button = Array.from(host.querySelectorAll('button')).find(el => el.getAttribute('aria-label') === label || el.textContent === label)
  expect(button).toBeDefined()
  await act(async () => button!.click())
  await settle()
}
async function fill(label: string, value: string) {
  const input = host.querySelector<HTMLInputElement>(`input[aria-label="${label}"]`)!
  expect(input, `missing input: ${label}`).not.toBeNull()
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

async function select(label: string, value: string) {
  const control = host.querySelector<HTMLSelectElement>(`select[aria-label="${label}"]`)
  expect(control, `missing select: ${label}`).not.toBeNull()
  await act(async () => {
    control!.value = value
    control!.dispatchEvent(new Event('change', { bubbles: true }))
  })
  await settle()
}

it('filters by board after the exchange selector and combines the filters', async () => {
  const base = state.rows[0]
  state.rows.push(
    { ...base, symbol: '000001.SZ', code: '000001', name: '深市示例', exchange: 'SZ', board: '深主板' },
    { ...base, symbol: '300001.SZ', code: '300001', name: '创业示例', exchange: 'SZ', board: '创业板' },
    { ...base, symbol: '688001.SH', code: '688001', name: '科创示例', board: '科创板' },
  )
  await render()
  const controls = Array.from(host.querySelectorAll('select'))
  expect(controls.map(control => control.getAttribute('aria-label'))).toEqual(['交易所筛选', '板块筛选'])
  expect(Array.from(controls[1].options).map(option => option.textContent)).toEqual(['全部板块', '沪主板', '深主板', '创业板', '科创板'])
  await select('板块筛选', '创业板')
  expect(host.querySelector('tbody')?.textContent).toContain('创业示例')
  expect(host.querySelector('tbody')?.textContent).not.toContain('深市示例')
  await select('交易所筛选', 'SH')
  expect(controls[1].value).toBe('')
  expect(Array.from(controls[1].options).map(option => option.textContent)).toEqual(['全部板块', '沪主板', '科创板'])
  expect(host.querySelector('tbody')?.textContent).toContain('浦发银行')
  expect(host.querySelector('tbody')?.textContent).toContain('科创示例')
  expect(host.querySelector('tbody')?.textContent).not.toContain('创业示例')
  expect(api.stockCatalog).toHaveBeenLastCalledWith('', 'SH', 1, 50, '')
  await select('板块筛选', '科创板')
  expect(host.querySelector('tbody')?.textContent).toContain('科创示例')
  expect(api.stockCatalog).toHaveBeenLastCalledWith('', 'SH', 1, 50, '科创板')
  await select('交易所筛选', '')
  expect(controls[1].value).toBe('科创板')
  expect(host.querySelector('tbody')?.textContent).toContain('科创示例')
  expect(Array.from(controls[1].options).map(option => option.textContent)).toEqual(['全部板块', '沪主板', '深主板', '创业板', '科创板'])
  await select('交易所筛选', 'SZ')
  expect(controls[1].value).toBe('')
  expect(Array.from(controls[1].options).map(option => option.textContent)).toEqual(['全部板块', '深主板', '创业板'])
  expect(host.querySelector('tbody')?.textContent).toContain('深市示例')
  expect(host.querySelector('tbody')?.textContent).toContain('创业示例')
  expect(host.querySelector('tbody')?.textContent).not.toContain('科创示例')
  await select('板块筛选', '创业板')
  expect(host.querySelector('tbody')?.textContent).toContain('创业示例')
  expect(host.querySelector('tbody')?.textContent).not.toContain('深市示例')
  expect(api.stockCatalog).toHaveBeenLastCalledWith('', 'SZ', 1, 50, '创业板')
})

it('retains compatible boards and resets page and jump draft when changing exchange', async () => {
  const base = state.rows[0]
  state.rows = Array.from({ length: 105 }, (_, index) => ({
    ...base, symbol: `${600000 + index}.SH`, code: String(600000 + index), name: `股票${index}`,
  }))
  await render()
  await select('板块筛选', '沪主板')
  await click('下一页')
  await fill('跳转页码', '3')
  await select('交易所筛选', 'SH')
  expect(host.querySelector<HTMLSelectElement>('[aria-label="板块筛选"]')?.value).toBe('沪主板')
  expect(host.querySelector<HTMLInputElement>('[aria-label="跳转页码"]')?.value).toBe('')
  expect(host.textContent).toContain('第 1 / 3 页')
  expect(api.stockCatalog).toHaveBeenLastCalledWith('', 'SH', 1, 50, '沪主板')
  await select('交易所筛选', 'SZ')
  expect(host.querySelector<HTMLSelectElement>('[aria-label="板块筛选"]')?.value).toBe('')
  expect(api.stockCatalog).toHaveBeenLastCalledWith('', 'SZ', 1, 50, '')
  expect(api.stockCatalog).not.toHaveBeenCalledWith('', 'SZ', expect.any(Number), 50, '沪主板')
})

it('jumps to a specified page, validates the range, and resets pagination on filters', async () => {
  const base = state.rows[0]
  state.rows = Array.from({ length: 105 }, (_, index) => ({
    ...base, symbol: `${600000 + index}.SH`, code: String(600000 + index), name: `股票${index}`,
  }))
  await render()
  await click('下一页')
  expect(host.querySelector('tbody')?.textContent).toContain('股票50')
  await fill('跳转页码', '3')
  await click('跳转')
  expect(host.textContent).toContain('第 3 / 3 页')
  expect(host.querySelectorAll('tbody tr')).toHaveLength(5)
  expect(host.querySelector('tbody')?.textContent).toContain('股票100')
  const requests = vi.mocked(api.stockCatalog).mock.calls.length
  for (const invalid of ['', '0', '4', '1.5']) {
    await fill('跳转页码', invalid)
    await click('跳转')
  }
  expect(api.stockCatalog).toHaveBeenCalledTimes(requests)
  await fill('跳转页码', '1')
  await act(async () => host.querySelector<HTMLInputElement>('[aria-label="跳转页码"]')!
    .dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true })))
  await settle()
  expect(host.textContent).toContain('第 1 / 3 页')
  await click('下一页')
  await select('板块筛选', '沪主板')
  expect(api.stockCatalog).toHaveBeenLastCalledWith('', '', 1, 50, '沪主板')
  await select('板块筛选', '科创板')
  expect(host.textContent).toContain('没有匹配的股票')
  expect(host.querySelector<HTMLInputElement>('[aria-label="跳转页码"]')?.disabled).toBe(true)
})
