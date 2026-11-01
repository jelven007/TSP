// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api, type Lot } from '@/lib/api'
import { Lots } from './Lots'
import { QK } from '@/lib/queryKeys'

vi.mock('@/components/StockPreviewDialog', () => ({ StockPreviewDialog: () => null }))
Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
let root: Root
let client: QueryClient
const batch = { id: '2026110101', name: '测试批次', number: '2026110101', created_at: '' }
const lot: Lot = {
  id: 'lot_test', batch_id: batch.id, symbol: '600519.SH', qty: 100, cost_price: 10,
  buy_date: '2026-09-30', target_pct: 6, stop_pct: 5, remind_date: '2026-10-20', lead_days: 1,
}

beforeEach(() => {
  vi.spyOn(HTMLElement.prototype, 'offsetParent', 'get').mockImplementation(function (this: HTMLElement) {
    return this.hidden ? null : document.body
  })
  vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
    queueMicrotask(() => callback(0)); return 1
  })
  vi.stubGlobal('cancelAnimationFrame', () => {})
  vi.spyOn(api, 'lotsList').mockResolvedValue({ lots: [lot] })
  vi.spyOn(api, 'lotBatches').mockResolvedValue({ batches: [batch], next_number: '2026110102' })
  vi.spyOn(api, 'instrumentNames').mockResolvedValue({ names: { '600519.SH': '贵州茅台' } })
  vi.spyOn(api, 'klineDailyBatch').mockResolvedValue({ data: {} })
  vi.spyOn(api, 'instrumentSearch').mockResolvedValue({ results: [
    { symbol: '600519.SH', name: '贵州茅台', code: '600519' },
    { symbol: '000001.SZ', name: '平安银行', code: '000001' },
  ] })
  vi.spyOn(api, 'lotSave').mockResolvedValue({ ok: true, lot })
  vi.spyOn(api, 'lotBatchCreate').mockResolvedValue({ ok: true, batch, lots: [] })
})

afterEach(async () => {
  await act(async () => root?.unmount())
  client?.clear()
  vi.restoreAllMocks()
  vi.useRealTimers()
  vi.unstubAllGlobals()
  document.body.innerHTML = ''
})

async function settle() {
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
}

async function renderLots() {
  const host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  await act(async () => root.render(
    <QueryClientProvider client={client}>
      <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}><Lots /></MemoryRouter>
    </QueryClientProvider>,
  ))
  await settle()
}

function button(label: string) {
  const found = [...document.querySelectorAll<HTMLButtonElement>('button')]
    .find(item => item.getAttribute('aria-label') === label || item.textContent?.trim() === label)
  expect(found, `button ${label}`).toBeDefined()
  return found!
}

async function click(label: string) {
  await act(async () => button(label).click())
  await settle()
}

async function input(label: string, value: string) {
  const field = document.querySelector<HTMLInputElement>(`input[aria-label="${label}"]`)!
  expect(field).not.toBeNull()
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(field, value)
    field.dispatchEvent(new Event('input', { bubbles: true }))
  })
  await settle()
}

function mockOptions() {
  return vi.spyOn(api, 'lotExpiryOptions').mockImplementation(async base => ({
    base_date: base!,
    next_trading_day: base === '2026-09-30' ? '2026-10-08' : '2026-11-02',
    five_trading_days: base === '2026-09-30' ? '2026-10-14' : '2026-11-06',
  }))
}

it('provides the requested choices in the batch dialog', async () => {
  mockOptions()
  // This check also fails against the existing calendar-day shortcuts.
  await renderLots()
  await click('新增批次')
  expect(button('下个交易日').disabled).toBe(false)
  expect(button('5个交易日').disabled).toBe(false)
  expect(button('批次到期日期').textContent).toContain('选择日期')
  expect(document.body.textContent).not.toContain('10天')
})

it('uses Beijing today when no buy date is recorded and saves the same expiry for every stock', async () => {
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(new Date('2026-10-31T17:00:00Z'))
  const options = mockOptions()
  await renderLots()
  await click('新增批次')
  await input('搜索添加股票', '600519')
  await click('600519.SH 贵州茅台')
  await input('600519.SH 成本价', '10')
  await input('搜索添加股票', '000001')
  await click('000001.SZ 平安银行')
  await input('000001.SZ 成本价', '12')
  await click('5个交易日')
  expect(options).toHaveBeenLastCalledWith('2026-11-01')
  expect(button('批次到期日期').textContent).toContain('2026-11-06')
  await click('保存批次')
  const saved = vi.mocked(api.lotBatchCreate).mock.calls[0][0]
  expect(saved.stocks).toHaveLength(2)
  expect(saved.stocks.every(stock => stock.remind_date === '2026-11-06' && stock.buy_date === null)).toBe(true)
  vi.useRealTimers()
})

it('calculates stock expiry from its buy date and lets manual dates be cleared', async () => {
  const options = mockOptions()
  await renderLots()
  const edit = document.querySelector<HTMLButtonElement>('button[title="编辑股票"]')!
  expect(edit).not.toBeNull()
  await act(async () => edit.click())
  await settle()
  expect(options).toHaveBeenCalledWith('2026-09-30')
  await click('下个交易日')
  expect(button('股票到期日期').textContent).toContain('2026-10-08')
  await click('5个交易日')
  expect(button('股票到期日期').textContent).toContain('2026-10-14')
  await click('股票到期日期')
  await click('2026-10-17')
  expect(button('股票到期日期').textContent).toContain('2026-10-17')
  expect(button('5个交易日').getAttribute('aria-pressed')).toBe('false')
  expect(button('下个交易日').getAttribute('aria-pressed')).toBe('false')
  await click('股票到期日期')
  await click('清空日期')
  expect(button('股票到期日期').textContent).toContain('选择日期')
  await click('保存')
  expect(api.lotSave).toHaveBeenCalledWith(expect.objectContaining({
    id: lot.id, batch_id: batch.id, remind_date: null,
  }))
})

it('disables shortcuts when the calendar is unknown while retaining the saved date and manual picker', async () => {
  vi.spyOn(api, 'lotExpiryOptions').mockResolvedValue({
    base_date: lot.buy_date!, next_trading_day: null, five_trading_days: null,
  })
  await renderLots()
  await act(async () => document.querySelector<HTMLButtonElement>('button[title="编辑股票"]')!.click())
  await settle()
  expect(button('下个交易日').disabled).toBe(true)
  expect(button('5个交易日').disabled).toBe(true)
  expect(button('股票到期日期').disabled).toBe(false)
  expect(button('股票到期日期').textContent).toContain(lot.remind_date)
  await click('股票到期日期')
  await click('2026-10-21')
  await click('保存')
  expect(api.lotSave).toHaveBeenCalledWith(expect.objectContaining({ remind_date: '2026-10-21' }))
})

it('keeps manual date selection available after a calendar request fails', async () => {
  vi.spyOn(api, 'lotExpiryOptions').mockRejectedValue(new Error('calendar unavailable'))
  await renderLots()
  await click('新增批次')
  expect(button('下个交易日').disabled).toBe(true)
  expect(button('5个交易日').disabled).toBe(true)
  expect(document.body.textContent).toContain('交易日历加载失败')
  await click('批次到期日期')
  expect(document.querySelector('[aria-label="批次到期日期日历"]')).not.toBeNull()
})

it('keeps batch records isolated when the same stock appears in several batches', async () => {
  mockOptions()
  const otherBatch = { ...batch, id: '2026110102', name: '另一批次', number: '2026110102' }
  const otherLot = { ...lot, id: 'lot_other', batch_id: otherBatch.id, cost_price: 25, qty: 300 }
  vi.mocked(api.lotsList).mockResolvedValue({ lots: [lot, otherLot] })
  vi.mocked(api.lotBatches).mockResolvedValue({ batches: [batch, otherBatch], next_number: '2026110103' })
  await renderLots()
  const panel = document.querySelector('[role="tabpanel"]')!
  expect(panel.querySelectorAll('tbody tr')).toHaveLength(1)
  expect(panel.textContent).toContain('10.00')
  await act(async () => document.getElementById(`holding-batch-${otherBatch.id}`)!.click())
  await settle()
  expect(panel.getAttribute('aria-labelledby')).toBe(`holding-batch-${otherBatch.id}`)
  expect(panel.querySelectorAll('tbody tr')).toHaveLength(1)
  expect(panel.textContent).toContain('25.00')
  await click(`编辑股票 ${lot.symbol}`)
  await click('保存')
  expect(api.lotSave).toHaveBeenCalledWith(expect.objectContaining({
    id: otherLot.id, batch_id: otherBatch.id, cost_price: 25, qty: 300,
  }))
})

it('preserves legacy records and shows empty batches without leaking another batch table', async () => {
  vi.mocked(api.lotsList).mockResolvedValue({ lots: [{ ...lot, batch_id: undefined }] })
  await renderLots()
  expect(document.querySelector('tbody')).toBeNull()
  expect(document.body.textContent).toContain('当前批次没有股票')
  await act(async () => document.getElementById('holding-batch-legacy')!.click())
  await settle()
  expect(document.querySelectorAll('tbody tr')).toHaveLength(1)
  expect(document.querySelector('[role="tabpanel"]')?.textContent).toContain('贵州茅台')
  await act(async () => document.getElementById(`holding-batch-${batch.id}`)!.click())
  await settle()
  expect(document.querySelector('tbody')).toBeNull()
  expect(document.body.textContent).toContain('当前批次没有股票')
})

it('confirms the selected batch number and count and cancel leaves all records intact', async () => {
  const remove = vi.fn()
  // Install the new API spy only after opening: RED must prove the missing UI.
  await renderLots()
  const removeButton = button('删除批次')
  expect(removeButton.closest('.page-header-actions')).not.toBeNull()
  expect(removeButton.previousElementSibling?.textContent).toBe('新增批次')
  expect(removeButton.querySelector('svg')).not.toBeNull()
  expect(document.querySelector('[role="tabpanel"]')?.textContent).not.toContain('个批次 ·')
  await click('删除批次')
  vi.spyOn(api, 'lotBatchDelete').mockImplementation(remove)
  const dialog = document.querySelector('[role="dialog"]')!
  expect(dialog.textContent).toContain(batch.number)
  expect(dialog.textContent).toContain('1 条股票记录')
  expect(dialog.textContent).toContain('止盈止损与到期提醒')
  await click('取消')
  expect(remove).not.toHaveBeenCalled()
  expect(document.querySelector('[role="dialog"]')).toBeNull()
  expect(document.querySelectorAll('tbody tr')).toHaveLength(1)
})

it('deletes the chosen batch atomically and switches to a surviving batch', async () => {
  const other = { ...batch, id: '2026110102', number: '2026110102', name: '另一批次' }
  const otherLot = { ...lot, id: 'lot_other', batch_id: other.id, cost_price: 25 }
  vi.mocked(api.lotsList).mockResolvedValue({ lots: [lot, otherLot] })
  vi.mocked(api.lotBatches).mockResolvedValue({ batches: [batch, other], next_number: '2026110103' })
  await renderLots()
  await act(async () => document.getElementById(`holding-batch-${other.id}`)!.click())
  await settle()
  await click('删除批次')
  expect(document.querySelector('[role="dialog"]')?.textContent).toContain(other.number)
  const remove = vi.spyOn(api, 'lotBatchDelete').mockImplementation(async () => {
    vi.mocked(api.lotsList).mockResolvedValue({ lots: [lot] })
    vi.mocked(api.lotBatches).mockResolvedValue({ batches: [batch], next_number: '2026110103' })
    return { ok: true, deleted_lots: 1 }
  })
  const invalidate = vi.spyOn(client, 'invalidateQueries')
  await click('确认删除批次')
  expect(remove).toHaveBeenCalledExactlyOnceWith(other.id)
  expect(document.querySelector('[role="dialog"]')).toBeNull()
  expect(document.getElementById(`holding-batch-${other.id}`)).toBeNull()
  expect(document.getElementById(`holding-batch-${batch.id}`)?.getAttribute('aria-selected')).toBe('true')
  expect(document.querySelectorAll('tbody tr')).toHaveLength(1)
  expect(invalidate).toHaveBeenCalledWith({ queryKey: QK.monitorRules })
})

it('keeps the confirmation and records after a failed delete and supports retry', async () => {
  await renderLots()
  await click('删除批次')
  const remove = vi.spyOn(api, 'lotBatchDelete').mockRejectedValue(new Error('删除失败，请重试'))
  await click('确认删除批次')
  expect(document.querySelector('[role="dialog"]')).not.toBeNull()
  expect(document.querySelector('[role="alert"]')?.textContent).toContain('删除失败，请重试')
  expect(document.querySelectorAll('tbody tr')).toHaveLength(1)
  remove.mockResolvedValue({ ok: true, deleted_lots: 1 })
  vi.mocked(api.lotsList).mockResolvedValue({ lots: [] })
  vi.mocked(api.lotBatches).mockResolvedValue({ batches: [], next_number: '2026110102' })
  await click('确认删除批次')
  expect(document.querySelector('[role="dialog"]')).toBeNull()
  expect(document.body.textContent).toContain('还没有批次')
})

it('permits deleting an empty named batch and excludes the legacy pseudo batch', async () => {
  vi.mocked(api.lotsList).mockResolvedValue({ lots: [{ ...lot, batch_id: undefined }] })
  await renderLots()
  await click('删除批次')
  expect(document.querySelector('[role="dialog"]')?.textContent).toContain('0 条股票记录')
  const remove = vi.spyOn(api, 'lotBatchDelete').mockResolvedValue({ ok: true, deleted_lots: 0 })
  vi.mocked(api.lotBatches).mockResolvedValue({ batches: [], next_number: '2026110102' })
  await click('确认删除批次')
  expect(remove).toHaveBeenCalledExactlyOnceWith(batch.id)
  expect(document.getElementById('holding-batch-legacy')?.getAttribute('aria-selected')).toBe('true')
  expect([...document.querySelectorAll('button')].find(b => b.textContent?.trim() === '删除批次')).toBeUndefined()
  expect(document.querySelectorAll('tbody tr')).toHaveLength(1)
})
