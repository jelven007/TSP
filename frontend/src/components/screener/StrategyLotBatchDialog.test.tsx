// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api } from '@/lib/api'
import { StrategyLotBatchDialog } from './StrategyLotBatchDialog'

vi.mock('@/components/DatePicker', () => ({
  DatePicker: ({ value, onChange, 'aria-label': label }: any) =>
    <input aria-label={label} value={value} onChange={e => onChange(e.target.value)} />,
}))
Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
let root: Root
let client: QueryClient
const batch = { id: '2026093001', number: '2026093001', name: '批次', created_at: '' }
const rows = [{ symbol: '600001.SH', name: '甲', filled_shares: 1100, buy_price: 10.25 }]
beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date'] }); vi.setSystemTime(new Date('2026-09-29T17:00:00Z'))
  vi.spyOn(HTMLElement.prototype, 'offsetParent', 'get').mockReturnValue(document.body)
  vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => { queueMicrotask(() => cb(0)); return 1 })
  vi.stubGlobal('cancelAnimationFrame', () => {})
  vi.spyOn(api, 'lotBatches').mockResolvedValue({ batches: [], next_number: batch.number })
  vi.spyOn(api, 'strategyGet').mockResolvedValue({
    params_defaults: { take_profit_pct: .08, stop_loss_pct: .07 },
  } as any)
  vi.spyOn(api, 'screenerResultHistoryDetail').mockResolvedValue({
    summary: { params: { take_profit_pct: .06, stop_loss_pct: .05 }, overrides: { params: { stop_loss_pct: .04 } } },
  } as any)
  vi.spyOn(api, 'lotExpiryOptions').mockResolvedValue({
    base_date: '2026-09-30', next_trading_day: '2026-10-08', five_trading_days: '2026-10-14',
  })
  vi.spyOn(api, 'lotBatchCreate').mockResolvedValue({ ok: true, batch, lots: [] })
})
afterEach(async () => {
  await act(async () => root?.unmount()); client?.clear()
  vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.useRealTimers(); document.body.innerHTML = ''
})
async function settle() { await act(async () => { await new Promise(r => setTimeout(r, 10)) }) }
async function render(recordId?: string, stocks = rows) {
  const host = document.createElement('div'); document.body.append(host); root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  await act(async () => root.render(<QueryClientProvider client={client}>
    <StrategyLotBatchDialog strategyId="s02" recordId={recordId} rows={stocks} onClose={vi.fn()} onCreated={vi.fn()} />
  </QueryClientProvider>))
  await settle(); await settle()
}
function value(label: string) { return document.querySelector<HTMLInputElement>(`[aria-label="${label}"]`)!.value }
async function input(label: string, text: string) {
  const field = document.querySelector<HTMLInputElement>(`[aria-label="${label}"]`)!
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(field, text)
    field.dispatchEvent(new Event('input', { bubbles: true }))
  })
}
async function save() {
  await act(async () => [...document.querySelectorAll<HTMLButtonElement>('button')]
    .find(b => b.textContent?.trim() === '保存批次')!.click())
  await settle()
}

it('prefills a saved buy result from its snapshot and uses today and the next holiday-adjusted trading day', async () => {
  await render('saved')
  expect(value('600001.SH 数量')).toBe('1100')
  expect(value('600001.SH 成本价')).toBe('10.25')
  expect(value('批次止盈%')).toBe('6')
  expect(value('批次止损%')).toBe('4')
  expect(value('批次买入日期')).toBe('2026-09-30')
  expect(value('批次到期日期')).toBe('2026-10-08')
  expect(api.strategyGet).not.toHaveBeenCalled()
  await save()
  expect(vi.mocked(api.lotBatchCreate).mock.calls[0][0]).toMatchObject({
    stocks: [{ symbol: '600001.SH', qty: 1100, cost_price: 10.25, target_pct: 6, stop_pct: 4,
      buy_date: '2026-09-30', remind_date: '2026-10-08', lead_days: 1 }],
  })
})

it('uses effective current risk settings for a result without an archive', async () => {
  await render()
  expect(value('批次止盈%')).toBe('8')
  expect(value('批次止损%')).toBe('7')
  expect(api.screenerResultHistoryDetail).not.toHaveBeenCalled()
})

it('does not override a manually entered expiry when the calendar arrives late', async () => {
  let finish!: (data: any) => void
  vi.mocked(api.lotExpiryOptions).mockImplementation(() => new Promise(resolve => { finish = resolve }))
  await render()
  await input('批次到期日期', '2026-10-20')
  await act(async () => finish({ base_date: '2026-09-30', next_trading_day: '2026-10-08' }))
  await settle()
  expect(value('批次到期日期')).toBe('2026-10-20')
})

it('keeps editable drafts after save failure and retries with the same request id', async () => {
  vi.mocked(api.lotBatchCreate).mockRejectedValueOnce(new Error('保存失败'))
  await render()
  await save()
  expect(document.querySelector('[role="alert"]')?.textContent).toContain('保存失败')
  expect(value('600001.SH 数量')).toBe('1100')
  const request = vi.mocked(api.lotBatchCreate).mock.calls[0][0].request_id
  await save()
  expect(vi.mocked(api.lotBatchCreate).mock.calls[1][0].request_id).toBe(request)
})

it('shows a retryable snapshot failure without falling back to current settings', async () => {
  vi.mocked(api.screenerResultHistoryDetail).mockRejectedValue(new Error('快照读取失败'))
  await render('saved')
  expect(document.querySelector('[role="alert"]')?.textContent).toContain('快照读取失败')
  expect(api.strategyGet).not.toHaveBeenCalled()
  expect(document.querySelector('[aria-label="600001.SH 成本价"]')).toBeNull()
})

it('leaves absent fill data blank and requires quantity and cost to be completed', async () => {
  await render(undefined, [{ symbol: '600001.SH', name: '甲' }] as any)
  expect(value('600001.SH 数量')).toBe('')
  expect(value('600001.SH 成本价')).toBe('')
  await save()
  expect(api.lotBatchCreate).not.toHaveBeenCalled()
  expect(document.querySelector('[role="alert"]')?.textContent).toContain('补填')
})

it('keeps manual expiry available when the trading calendar cannot be read', async () => {
  vi.mocked(api.lotExpiryOptions).mockRejectedValue(new Error('calendar unavailable'))
  await render()
  expect(document.body.textContent).toContain('交易日历加载失败')
  expect(value('批次到期日期')).toBe('')
  await input('批次到期日期', '2026-10-20')
  await save()
  expect(vi.mocked(api.lotBatchCreate).mock.calls[0][0].stocks[0].remind_date).toBe('2026-10-20')
})
