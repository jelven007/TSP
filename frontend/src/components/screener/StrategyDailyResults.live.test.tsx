// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { StrategyDailyResults } from './StrategyDailyResults'
import type { ScreenerResult } from '@/lib/api'
import { QK } from '@/lib/queryKeys'

const { snapshot, strategyGet, customSignalsList } = vi.hoisted(() => ({
  snapshot: vi.fn(), strategyGet: vi.fn(), customSignalsList: vi.fn(),
}))
vi.mock('@/lib/api', () => ({ api: { marketSnapshot: snapshot, strategyGet, customSignalsList } }))
vi.mock('@/components/StockPreviewDialog', () => ({ StockPreviewDialog: () => null }))
Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
let root: Root
let host: HTMLDivElement
let client: QueryClient
const result: ScreenerResult = {
  strategy: 'custom_s02', as_of: '2026-11-06', record_id: 'old', rows: [], total: 0, elapsed_ms: 0,
  section_mode: 'signals', sections: {
    sell: [{ symbol: '600001.SH', name: '卖股', entry_date: '2026-11-04', close: 9, change_pct: .01, score: 80,
      volume: 1234.6, amount: 1234567.6,
      suggested_sell_price: { lower: 9.4, upper: 10.6, reference_date: '2026-11-04', reference_close: 10 } }],
    select: [{ symbol: '600002.SH', name: '选股', reference_date: '2026-11-04', score: 99 }],
    buy: [{ symbol: '600003.SH', name: '买股', close: 9, score: 80, volume: 1234.6, amount: 1234567.6,
      suggested_lots_100k: 111 }],
  },
}
const liveLabels = ['现价', '涨跌幅', '换手率', '量比', '信号']
beforeEach(() => {
  vi.useFakeTimers()
  vi.setSystemTime(new Date('2026-11-06T10:00:00+08:00'))
  strategyGet.mockReset().mockResolvedValue({ id: 'custom_mvxxcs03', exit_signals: ['csgi_s03_exit'] })
  customSignalsList.mockReset().mockResolvedValue({ signals: [
    { id: 'other_exit', kind: 'exit', enabled: true, template: 'morning_liquidation',
      params: { take_profit_pct: 99 } },
    { id: 's03_exit', kind: 'exit', enabled: true, template: 'morning_liquidation',
      params: { take_profit_pct: 3 } },
  ] })
  snapshot.mockReset().mockResolvedValue({ as_of: '2026-11-06', rows: [
    { symbol: '600001.SH', raw_close: 12.5, close: 6.25, change_pct: .02, turnover_rate: 3.45,
      vol_ratio_5d: 1.8, signal_ma_golden_5_20: true, volume: 999999, amount: 888888 },
    { symbol: '600002.SH', raw_close: 20 },
    { symbol: '600003.SH', raw_close: 12.5 },
  ] })
})
afterEach(async () => {
  await act(async () => root?.unmount())
  client?.clear(); host?.remove(); vi.useRealTimers()
})
async function flush() { await act(async () => { await vi.advanceTimersByTimeAsync(10) }) }
async function render(value: ScreenerResult = result) {
  host = document.createElement('div'); document.body.append(host); root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await act(async () => root.render(<QueryClientProvider client={client}>
    <StrategyDailyResults result={value} positionLimit={5} currentDate={value.as_of} />
  </QueryClientProvider>))
  await flush()
}
async function tab(index: number) {
  await act(async () => host.querySelectorAll<HTMLButtonElement>('[role="tab"]')[index].click())
  await flush()
}
function headers() { return [...host.querySelectorAll('th')].map(el => el.textContent) }
function cell(label: string) { return host.querySelectorAll('tbody tr:first-child td')[headers().indexOf(label)] }

it('keeps trigger prices separate and appends five live columns with correct units', async () => {
  await render()
  expect(headers()).not.toContain('买入信号日期')
  expect(headers().slice(-9)).toEqual(['信号评分', '分钟成交量', '分钟成交额', '建议卖出价', ...liveLabels])
  expect(cell('分钟成交量')?.textContent).toBe('1,235')
  expect(cell('分钟成交额')?.textContent).toBe('1,234,568')
  expect(cell('建议卖出价')?.textContent).toBe('9.40～10.60')
  expect(cell('触发价')?.textContent).toBe('9.00')
  expect(cell('现价')?.textContent).toBe('12.50')
  expect(cell('涨跌幅')?.textContent).toBe('2.00%')
  expect(cell('换手率')?.textContent).toBe('3.45%')
  expect(cell('信号')?.textContent).toContain('MA金叉')
  await tab(1)
  expect(headers()).not.toContain('数值参考日')
  expect(headers().slice(-9)).toEqual(['选股评分', 'ATR14÷昨收', 'ATR排名', '排名分数', ...liveLabels])
})

it('formats minute volume and amount as integers and uses saved trigger-price lots', async () => {
  await render(); await tab(2)
  expect(headers().slice(-8)).toEqual(['分钟成交量', '分钟成交额', '建议购买量/10万', ...liveLabels])
  expect(cell('分钟成交量')?.textContent).toBe('1,235')
  expect(cell('分钟成交额')?.textContent).toBe('1,234,568')
  expect(cell('建议购买量/10万')?.textContent).toBe('111')
})

it('reads the active symbols without per-second polling and preserves tab and scroll on updates', async () => {
  await render()
  expect(snapshot).toHaveBeenLastCalledWith({ realtime: true, symbols: ['600001.SH'] })
  await tab(2)
  const panel = host.querySelector<HTMLElement>('[role="tabpanel"]')!
  panel.scrollLeft = 120
  snapshot.mockClear()
  await act(async () => { await vi.advanceTimersByTimeAsync(3010) })
  expect(snapshot).not.toHaveBeenCalled()
  await act(async () => root.render(<QueryClientProvider client={client}>
    <StrategyDailyResults result={{ ...result, record_id: 'new' }} positionLimit={5} currentDate={result.as_of} />
  </QueryClientProvider>))
  expect(host.querySelector('[aria-selected="true"]')?.textContent).toContain('买')
  expect(host.querySelector('[role="tabpanel"]')).toBe(panel)
  expect(panel.scrollLeft).toBe(120)
})

it.each([null, 0, -1, NaN, Infinity])('keeps saved lots without a valid live price (%s)', async price => {
  snapshot.mockResolvedValue({ as_of: '2026-11-06', rows: [{ symbol: '600003.SH', raw_close: price, close: 10 }] })
  await render(); await tab(2)
  expect(cell('现价')?.textContent).toBe('—')
  expect(cell('建议购买量/10万')?.textContent).toBe('111')
})

it('does not substitute a live estimate for a missing saved quantity', async () => {
  await render({ ...result, sections: { ...result.sections!, buy: [{ symbol: '600003.SH', close: 9 }] } })
  await tab(2)
  expect(cell('现价')?.textContent).toBe('12.50')
  expect(cell('建议购买量/10万')?.textContent).toBe('—')
})

it.each([null, { lower: -1, upper: 10 }, { lower: 11, upper: 10 }, { lower: 9 }])(
  'keeps missing or invalid saved sell intervals unknown (%j)', async interval => {
    await render({ ...result, sections: { ...result.sections!,
      sell: [{ symbol: '600001.SH', suggested_sell_price: interval }] } })
    expect(cell('建议卖出价')?.textContent).toBe('—')
  })

it('rejects stale days and late responses from an inactive tab', async () => {
  let finish!: (value: unknown) => void
  snapshot.mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
  await render(); await tab(1)
  expect(snapshot).toHaveBeenCalled()
  await act(async () => finish({ as_of: '2026-11-06', rows: [{ symbol: '600001.SH', raw_close: 777 }] }))
  await flush()
  expect(cell('现价')?.textContent).toBe('20.00')
  snapshot.mockResolvedValue({ as_of: '2026-11-05', rows: [{ symbol: '600003.SH', raw_close: 999 }] })
  await tab(2)
  expect(cell('现价')?.textContent).toBe('—')
})

const s03Result: ScreenerResult = {
  ...result, strategy: 'custom_mvxxcs03', sections: {
    ...result.sections!,
    sell: [{ symbol: '600001.SH', close: 34.22, entry_signal_price: 34.74, holding_exit: true,
      atr14_previous_close: .05, atr_rank: 1, score: 100 }],
    buy: [{ symbol: '600003.SH', close: 34.74, atr14_previous_close: .05, atr_rank: 1, score: 100 }],
  },
}
it('uses the linked S03 exit take-profit and saved buy trigger price', async () => {
  await render(s03Result)
  expect(strategyGet).toHaveBeenCalledWith('custom_mvxxcs03')
  expect(cell('建议卖出价')?.textContent).toBe('35.78')
  expect(cell('触发价')?.textContent).toBe('34.22')
  expect(cell('现价')?.textContent).toBe('12.50')
  expect(headers()).not.toContain('ATR14÷昨收')
  expect(headers()).not.toContain('ATR排名')
  await act(async () => client.setQueryData(QK.customSignals, { signals: [
    { id: 's03_exit', kind: 'exit', enabled: true, template: 'morning_liquidation',
      params: { take_profit_pct: 5 } },
  ] }))
  await flush()
  expect(cell('建议卖出价')?.textContent).toBe('36.48')
  await tab(2)
  expect(headers()).not.toContain('ATR14÷昨收')
  expect(headers()).not.toContain('ATR排名')
  expect(cell('触发价')?.textContent).toBe('34.74')
})

it.each([null, 0, -1, NaN, Infinity])('keeps S03 target unknown for an invalid trigger price (%s)', async entry_signal_price => {
  await render({ ...s03Result, sections: { ...s03Result.sections!,
    sell: [{ symbol: '600001.SH', close: 34.22, entry_signal_price, holding_exit: true,
      suggested_sell_price: { lower: 9, upper: 10 } }] } })
  expect(cell('建议卖出价')?.textContent).toBe('—')
})

it('uses a pending sell roster buy price before an exit replaces the close field', async () => {
  await render({ ...s03Result, sections: { ...s03Result.sections!,
    sell: [{ symbol: '600001.SH', close: 34.74, signal_roster: true }] } })
  expect(cell('建议卖出价')?.textContent).toBe('35.78')
})

it('keeps the target unknown if an exited row has lost its entry reference', async () => {
  await render({ ...s03Result, sections: { ...s03Result.sections!,
    sell: [{ symbol: '600001.SH', close: 34.22, holding_exit: true }] } })
  expect(cell('建议卖出价')?.textContent).toBe('—')
})

it.each([
  { id: 'other_exit', kind: 'exit', enabled: true, template: 'morning_liquidation', params: { take_profit_pct: 3 } },
  { id: 's03_exit', kind: 'exit', enabled: false, template: 'morning_liquidation', params: { take_profit_pct: 3 } },
  { id: 's03_exit', kind: 'entry', enabled: true, template: 'morning_liquidation', params: { take_profit_pct: 3 } },
  { id: 's03_exit', kind: 'exit', enabled: true, template: 's01_morning_exit', params: { take_profit_pct: 3 } },
  ...[null, 0, -1, NaN, Infinity, '3'].map(take_profit_pct => ({
    id: 's03_exit', kind: 'exit', enabled: true, template: 'morning_liquidation', params: { take_profit_pct },
  })),
])('keeps S03 target unknown without a valid linked enabled exit (%j)', async signal => {
  customSignalsList.mockResolvedValue({ signals: [signal] })
  await render(s03Result)
  expect(cell('建议卖出价')?.textContent).toBe('—')
})
