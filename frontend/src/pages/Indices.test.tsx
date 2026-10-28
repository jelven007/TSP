// @vitest-environment jsdom
import { act, useState } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { Indices } from './Indices'

type MinuteResponse = {
  symbol: string
  date: string
  rows: { datetime: string; close: number }[]
}
const fixtures = vi.hoisted(() => ({
  pending: [] as { symbol: string; day: string; resolve: (value: MinuteResponse) => void }[],
  mounts: 0,
  klines: [] as { symbol: string; period: string }[],
  repairs: [] as unknown[][],
}))

vi.mock('@/lib/api', () => ({
  api: {
    indexCatalog: async () => ({ rows: [
      { symbol: '000001.SH', name: '上证指数' }, { symbol: '399001.SZ', name: '深证成指' },
      { symbol: '399006.SZ', name: '创业板指' }, { symbol: '000680.SH', name: '科创综指' },
      { symbol: '000300.SH', name: '沪深300' }, { symbol: '000016.SH', name: '上证50' },
      { symbol: '399330.SZ', name: '深证100' },
    ] }),
    indexQuotes: async () => ({ rows: [] }),
    indexKline: async (symbol: string, period: string) => {
      fixtures.klines.push({ symbol, period })
      return {
        symbol, period,
        rows: ['2026-09-09', '2026-09-10', '2026-09-11'].map((date, i) => ({
          date: period === '1d' ? date : `2026-0${7 + i}-01`, trade_date: date,
          open: 10, high: 12, low: 9, close: 11, volume: 1,
        })),
      }
    },
    pipelineJobs: async () => ({ jobs: [], active_id: null }),
    repairIndex: async (...args: unknown[]) => {
      fixtures.repairs.push(args)
      return { job_id: 'index-job', status: 'started' }
    },
    pipelineJob: async () => ({
      id: 'index-job', kind: 'index_repair', status: 'succeeded', stage: 'done',
      progress: 100, stage_pct: 100, log: [], duration_s: 1,
      result: { period: '1mo', rows: 7, universe_size: 7 }, error: null,
    }),
    indexDaily: async (symbol: string) => ({
      symbol,
      rows: ['2026-09-09', '2026-09-10', '2026-09-11'].map(date => ({
        date, open: 10, high: 12, low: 9, close: 11, volume: 1,
      })),
    }),
    indexMinute: (symbol: string, day: string) => new Promise<MinuteResponse>(resolve => {
      fixtures.pending.push({ symbol, day, resolve })
    }),
  },
}))
vi.mock('@/lib/useSharedQueries', () => ({
  useCapabilities: () => ({ data: { capabilities: { 'kline.minute.batch': true } } }),
  useCapabilityMatrix: () => ({ data: { capabilities: [
    { id: 'daily', usable: true }, { id: 'minute', usable: true },
  ] } }),
}))
vi.mock('@/lib/accountSession', async importOriginal => ({
  ...await importOriginal<typeof import('@/lib/accountSession')>(),
  hasPermission: () => true,
}))
vi.mock('@/components/EChartsCandlestick', () => ({
  SUB_CHARTS: [],
  OVERLAY_INDICATORS: [],
  EChartsCandlestick: ({ data, onDateClick }: {
    data: { date: string }[]; onDateClick: (date: string) => void
  }) => <div>{data.map(row => (
    <button key={row.date} data-day={row.date} onClick={() => onDateClick(row.date)}>{row.date}</button>
  ))}</div>,
}))
vi.mock('@/components/EChartsIntraday', () => ({
  EChartsIntraday: ({ data, date }: { data: MinuteResponse['rows']; date: string }) => {
    const [mount] = useState(() => ++fixtures.mounts)
    return <div data-chart data-mount={mount}>{`${date}|${data[0].datetime}|${data[0].close}`}</div>
  },
}))

let host: HTMLDivElement
let root: Root
let client: QueryClient

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  fixtures.pending.length = 0
  fixtures.mounts = 0
  fixtures.klines.length = 0
  fixtures.repairs.length = 0
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: {
    retry: false, staleTime: Infinity, gcTime: Infinity,
  } } })
})

afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
})

// React Query batches observer notifications on the next timer tick.
async function settle() {
  for (let i = 0; i < 5; i++) {
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
  }
}

async function resolveDay(day: string, close: number, symbol?: string) {
  const index = fixtures.pending.findIndex(call => call.day === day && (!symbol || call.symbol === symbol))
  expect(index).toBeGreaterThanOrEqual(0)
  const [call] = fixtures.pending.splice(index, 1)
  await act(async () => call.resolve({
    symbol: call.symbol, date: day, rows: [{ datetime: `${day}T09:35:00`, close }],
  }))
  await settle()
}

async function clickDay(day: string) {
  const button = host.querySelector<HTMLButtonElement>(`[data-day="${day}"]`)
  expect(button).not.toBeNull()
  await act(async () => button!.click())
  await settle()
}

it('keeps selected dates isolated across delayed, out-of-order and cached responses', async () => {
  await act(async () => root.render(
    <MemoryRouter><QueryClientProvider client={client}><Indices /></QueryClientProvider></MemoryRouter>,
  ))
  await settle()
  const chart = () => host.querySelector<HTMLElement>('[data-chart]')
  await resolveDay('2026-09-11', 11)
  expect(chart()?.textContent).toBe('2026-09-11|2026-09-11T09:35:00|11')

  await clickDay('2026-09-10')
  expect(chart()).toBeNull()
  await clickDay('2026-09-09')
  await resolveDay('2026-09-10', 10)
  expect(chart()).toBeNull()
  await resolveDay('2026-09-09', 9)
  expect(chart()?.textContent).toBe('2026-09-09|2026-09-09T09:35:00|9')

  const mount9 = chart()?.dataset.mount
  await clickDay('2026-09-11')
  expect(chart()?.textContent).toBe('2026-09-11|2026-09-11T09:35:00|11')
  expect(chart()?.dataset.mount).not.toBe(mount9)

  const mount11 = chart()?.dataset.mount
  await act(async () => { void client.invalidateQueries({ queryKey: ['index-minute'] }) })
  await settle()
  expect(chart()?.textContent).toBe('2026-09-11|2026-09-11T09:35:00|11')
  await resolveDay('2026-09-11', 12)
  expect(chart()?.textContent).toBe('2026-09-11|2026-09-11T09:35:00|12')
  expect(chart()?.dataset.mount).toBe(mount11)
})

async function renderPage() {
  await act(async () => root.render(
    <MemoryRouter><QueryClientProvider client={client}><Indices /></QueryClientProvider></MemoryRouter>,
  ))
  await settle()
}

it('shows seven indices and switches the added index across day, week and month', async () => {
  await renderPage()
  for (const name of ['沪深300', '上证50', '深证100']) expect(host.textContent).toContain(name)
  const button = [...host.querySelectorAll('aside button')].find(b => b.textContent?.includes('深证100'))
  await act(async () => (button as HTMLButtonElement).click())
  await settle()
  const select = host.querySelector<HTMLSelectElement>('select[aria-label="K线周期"]')!
  expect(select).not.toBeNull()
  expect([...select.options].map(o => o.value)).toEqual(['1d', '1w', '1mo'])
  for (const period of ['1w', '1mo']) {
    await act(async () => {
      select.value = period
      select.dispatchEvent(new Event('change', { bubbles: true }))
    })
    await settle()
    expect(fixtures.klines).toContainEqual({ symbol: '399330.SZ', period })
  }
  await resolveDay('2026-09-11', 13, '399330.SZ')
  expect(host.querySelector('[data-chart]')?.textContent).toContain('2026-09-11')
})

it('opens data repair with seven indices and defaults to daily with week/month options', async () => {
  await renderPage()
  const button = [...host.querySelectorAll('button')].find(b => b.textContent?.includes('数据修正'))!
  expect(button).toBeDefined()
  await act(async () => button.click())
  await settle()
  const dialog = document.querySelector('[role="dialog"]')!
  expect(dialog.textContent).toContain('七只指数')
  const select = dialog.querySelector<HTMLSelectElement>('select[aria-label="K线周期"]')!
  expect(select.value).toBe('1d')
  expect([...select.options].map(o => o.value)).toEqual(['1d', '1w', '1mo'])
  await act(async () => {
    select.value = '1mo'
    select.dispatchEvent(new Event('change', { bubbles: true }))
  })
  await act(async () => [...dialog.querySelectorAll('button')].find(b => b.textContent === '获取遗漏数据')!.click())
  await settle()
  expect(fixtures.repairs[0]).toEqual([expect.any(String), '1mo', 'missing', expect.any(String)])
})
