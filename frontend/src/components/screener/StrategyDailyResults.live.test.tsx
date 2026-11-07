// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { StrategyDailyResults } from './StrategyDailyResults'
import type { ScreenerResult } from '@/lib/api'

const { snapshot } = vi.hoisted(() => ({ snapshot: vi.fn() }))
vi.mock('@/lib/api', () => ({ api: { marketSnapshot: snapshot } }))
vi.mock('@/components/StockPreviewDialog', () => ({ StockPreviewDialog: () => null }))
Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
let root: Root
let host: HTMLDivElement
let client: QueryClient
const result: ScreenerResult = {
  strategy: 'custom_s02', as_of: '2026-11-05', record_id: 'old', rows: [], total: 0, elapsed_ms: 0,
  section_mode: 'signals', sections: {
    sell: [{ symbol: '600001.SH', name: '卖股', entry_date: '2026-11-04', close: 9, change_pct: .01, score: 80 }],
    select: [{ symbol: '600002.SH', name: '选股', reference_date: '2026-11-04', score: 99 }],
    buy: [{ symbol: '600003.SH', name: '买股', close: 9, score: 80, volume: 1234.6, amount: 1234567.6,
      suggested_lots_100k: 111 }],
  },
}
const liveLabels = ['现价', '涨跌幅', '换手率', '量比', '信号']
beforeEach(() => {
  vi.useFakeTimers()
  vi.setSystemTime(new Date('2026-11-06T10:00:00+08:00'))
  snapshot.mockReset().mockResolvedValue({ as_of: '2026-11-06', rows: [
    { symbol: '600001.SH', raw_close: 12.5, close: 6.25, change_pct: .02, turnover_rate: 3.45,
      vol_ratio_5d: 1.8, signal_ma_golden_5_20: true },
    { symbol: '600002.SH', raw_close: 20 },
    { symbol: '600003.SH', raw_close: 12.5 },
  ] })
})
afterEach(async () => {
  await act(async () => root?.unmount())
  client?.clear(); host?.remove(); vi.useRealTimers()
})
async function flush() { await act(async () => { await vi.advanceTimersByTimeAsync(10) }) }
async function render() {
  host = document.createElement('div'); document.body.append(host); root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await act(async () => root.render(<QueryClientProvider client={client}>
    <StrategyDailyResults result={result} positionLimit={5} />
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
  expect(headers().slice(-6)).toEqual(['建议卖出价', ...liveLabels])
  expect(cell('触发价')?.textContent).toBe('9.00')
  expect(cell('现价')?.textContent).toBe('12.50')
  expect(cell('涨跌幅')?.textContent).toBe('2.00%')
  expect(cell('换手率')?.textContent).toBe('3.45%')
  expect(cell('信号')?.textContent).toContain('MA金叉')
  await tab(1)
  expect(headers()).not.toContain('数值参考日')
  expect(headers().slice(-9)).toEqual(['选股评分', 'ATR14÷昨收', 'ATR排名', '排名分数', ...liveLabels])
})

it('formats minute volume and amount as integers and preserves frozen trigger-price lots', async () => {
  await render(); await tab(2)
  expect(headers().slice(-8)).toEqual(['分钟成交量', '分钟成交额', '建议购买量/10万', ...liveLabels])
  expect(cell('分钟成交量')?.textContent).toBe('1,235')
  expect(cell('分钟成交额')?.textContent).toBe('1,234,568')
  expect(cell('建议购买量/10万')?.textContent).toBe('111')
})

it('polls only the active symbol set every second and preserves tab and scroll on result updates', async () => {
  await render()
  expect(snapshot).toHaveBeenLastCalledWith({ realtime: true, symbols: ['600001.SH'] })
  await tab(2)
  const panel = host.querySelector<HTMLElement>('[role="tabpanel"]')!
  panel.scrollLeft = 120
  snapshot.mockClear()
  await act(async () => { await vi.advanceTimersByTimeAsync(3010) })
  expect(snapshot.mock.calls.length).toBe(3)
  expect(snapshot.mock.calls.every(([options]) => options.symbols.join() === '600003.SH')).toBe(true)
  await act(async () => root.render(<QueryClientProvider client={client}>
    <StrategyDailyResults result={{ ...result, record_id: 'new' }} positionLimit={5} />
  </QueryClientProvider>))
  expect(host.querySelector('[aria-selected="true"]')?.textContent).toContain('买')
  expect(host.querySelector('[role="tabpanel"]')).toBe(panel)
  expect(panel.scrollLeft).toBe(120)
})

it.each([null, 0, -1, NaN, Infinity])('preserves frozen lots without a finite positive live price (%s)', async price => {
  snapshot.mockResolvedValue({ as_of: '2026-11-06', rows: [{ symbol: '600003.SH', raw_close: price, close: 10 }] })
  await render(); await tab(2)
  expect(cell('现价')?.textContent).toBe('—')
  expect(cell('建议购买量/10万')?.textContent).toBe('111')
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
