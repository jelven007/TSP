// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, it, vi } from 'vitest'
import { api } from '@/lib/api'
import { StockMultiDayIntradayChart } from './StockMultiDayIntradayChart'

vi.mock('@/components/EChartsMultiDayIntraday', () => ({
  EChartsMultiDayIntraday: () => <div>分时图</div>,
}))
vi.mock('@/components/Toast', () => ({ toast: vi.fn() }))

const symbol = '002452.SZ'
type MinuteKlineRangeResponse = Awaited<ReturnType<typeof api.klineMinuteRange>>
const session = {
  date: '2026-11-24', prev_close: 10,
  rows: [{ datetime: '2026-11-24 15:00:00', open: 11, high: 11, low: 11,
    close: 11, volume: 100, amount: 110000 }],
}
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

async function mount(days = 1) {
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await act(async () => root!.render(
    <QueryClientProvider client={client}>
      <StockMultiDayIntradayChart symbol={symbol} days={days} />
    </QueryClientProvider>,
  ))
}

async function flushQueries() {
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 10)) })
}

it.each([1, 10])('最新分时先返回时，等待%d日历史响应再判断缺口', async days => {
  let resolveHistory!: (value: MinuteKlineRangeResponse) => void
  vi.spyOn(api, 'klineMinuteRange').mockImplementation(() => new Promise(resolve => { resolveHistory = resolve }))
  vi.spyOn(api, 'klineMinute').mockResolvedValue({ symbol, ...session, source: 'local' })
  const sync = vi.spyOn(api, 'syncMinuteSingle').mockImplementation(() => new Promise(() => {}))
  await mount(days)
  await flushQueries()
  expect(host.textContent).toContain('分时图')
  expect(sync).not.toHaveBeenCalled()
  expect(host.textContent).not.toContain('目标')
  expect(host.textContent).not.toContain('补齐')
  await act(async () => resolveHistory({
    symbol, requested_days: days, asset_type: 'stock', source: 'local',
    sessions: Array.from({ length: days }, (_, index) => ({
      ...session, date: `2026-11-${String(24 - index).padStart(2, '0')}`,
    })),
  }))
  await flushQueries()
  expect(sync).not.toHaveBeenCalled()
  expect(host.textContent).not.toContain('目标')
})

it('历史响应确认真实缺口后仍自动补齐，最新分时不抵消本地缺口', async () => {
  let resolveHistory!: (value: MinuteKlineRangeResponse) => void
  vi.spyOn(api, 'klineMinuteRange').mockImplementation(() => new Promise(resolve => { resolveHistory = resolve }))
  vi.spyOn(api, 'klineMinute').mockResolvedValue({ symbol, ...session, source: 'local' })
  const sync = vi.spyOn(api, 'syncMinuteSingle').mockImplementation(() => new Promise(() => {}))
  await mount()
  await flushQueries()
  expect(sync).not.toHaveBeenCalled()
  await act(async () => resolveHistory({
    symbol, requested_days: 1, asset_type: 'stock', source: 'none', sessions: [],
  }))
  await flushQueries()
  expect(sync).toHaveBeenCalledExactlyOnceWith(symbol, 1)
  await flushQueries()
  expect(host.textContent).toContain('正在补齐')
})

it('历史请求失败时不把缺口当作零并触发自动补齐', async () => {
  vi.spyOn(api, 'klineMinuteRange').mockRejectedValue(new Error('history unavailable'))
  vi.spyOn(api, 'klineMinute').mockResolvedValue({ symbol, ...session, source: 'local' })
  const sync = vi.spyOn(api, 'syncMinuteSingle').mockImplementation(() => new Promise(() => {}))
  await mount()
  await flushQueries()
  expect(sync).not.toHaveBeenCalled()
})
