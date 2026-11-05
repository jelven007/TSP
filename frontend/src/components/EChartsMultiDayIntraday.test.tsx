// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { MinuteKlineSession } from '@/lib/api'
import { EChartsMultiDayIntraday } from './EChartsMultiDayIntraday'

const chart = vi.hoisted(() => ({
  handlers: {} as Record<string, (event?: any) => void>,
  on: vi.fn(), off: vi.fn(), setOption: vi.fn(), resize: vi.fn(), dispose: vi.fn(),
  getZr: () => ({ on: vi.fn(), off: vi.fn() }),
}))
vi.mock('@/lib/echarts', () => ({ init: () => chart }))
vi.mock('@/lib/theme', () => ({ useChartTheme: () => ({}) }))

function session(date = '2026-11-05'): MinuteKlineSession {
  return {
    date,
    prev_close: 10,
    rows: [
      { datetime: `${date} 09:30:00`, open: 10, high: 12, low: 9, close: 11, volume: 100, amount: 110000 },
      { datetime: `${date} 15:00:00`, open: 10.5, high: 10.5, low: 10.5, close: 10.5, volume: 50, amount: 52500 },
    ],
  }
}

let host: HTMLDivElement
let root: ReturnType<typeof createRoot>
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} })
  chart.on.mockImplementation((name, callback) => { chart.handlers[name] = callback })
  host = document.createElement('div')
  root = createRoot(host)
})
afterEach(async () => {
  await act(async () => root.unmount())
  vi.unstubAllGlobals()
})
async function render(sessions: MinuteKlineSession[]) {
  await act(async () => root.render(<EChartsMultiDayIntraday sessions={sessions} />))
}
function field(label: string) {
  return Array.from(host.querySelectorAll('span')).find(el => el.textContent === label)?.nextElementSibling?.textContent
}

it('1日分时默认显示全日OHLC与累计量额，而非最后一根价格相同的分钟', async () => {
  await render([session()])
  expect(field('开')).toBe('10.00')
  expect(field('高')).toBe('12.00')
  expect(field('低')).toBe('9.00')
  expect(field('收')).toBe('10.50')
  expect(field('累计量')).toBe('150')
  expect(field('累计额')).toBe('16万')
  expect(field('均价')).toBe('10.83')
  expect(host.textContent).toContain('分时汇总')
  expect(host.textContent).not.toContain('15:00')
})

it('多日默认汇总最新交易日，不把前一天的高低价和量额混入', async () => {
  const previous = session('2026-11-04')
  previous.rows[0].high = 30
  previous.rows[0].low = 1
  await render([previous, session()])
  expect(field('高')).toBe('12.00')
  expect(field('低')).toBe('9.00')
  expect(field('累计量')).toBe('150')
  expect(host.textContent).toContain('2026-11-05')
})

it('悬停明确显示对应分钟，移出后恢复全日汇总', async () => {
  await render([session()])
  await act(async () => chart.handlers.updateAxisPointer({ axesInfo: [{ axisDim: 'x', axisIndex: 0, value: '2026-11-05 15:00' }] }))
  expect(host.textContent).toContain('15:00 分钟')
  expect(field('开')).toBe('10.50')
  expect(field('高')).toBe('10.50')
  expect(field('量')).toBe('50')
  await act(async () => chart.handlers.globalout())
  expect(field('高')).toBe('12.00')
  expect(field('累计量')).toBe('150')
})

it('实时新增分钟和切换日期后更新汇总，不残留旧分钟', async () => {
  const current = session()
  current.rows[1].datetime = `${current.date} 14:59:00`
  await render([current])
  await act(async () => chart.handlers.updateAxisPointer({ axesInfo: [{ value: '2026-11-05 09:30' }] }))
  const updated = { ...current, rows: [...current.rows, { ...current.rows[1], datetime: `${current.date} 15:00:00`, high: 13, close: 12.5, volume: 80 }] }
  await render([updated])
  expect(field('高')).toBe('13.00')
  expect(field('收')).toBe('12.50')
  expect(field('累计量')).toBe('230')
  await render([session('2026-11-06')])
  expect(field('高')).toBe('12.00')
  expect(host.textContent).toContain('2026-11-06')
  expect(host.textContent).not.toContain('2026-11-05')
})

it('缺失开盘价或成交额保留未知，不用最新价或0补造', async () => {
  const current = session()
  current.rows[0].open = null
  current.rows[0].amount = null
  await render([current])
  expect(field('开')).toBe('—')
  expect(field('累计额')).toBe('—')
  expect(field('均价')).toBe('—')
  expect(field('高')).toBe('12.00')
})

it.each<[number, number, string]>([
  [100_000_000, 50_000_000, '1.50亿'],
  [1_000_000_000, 1_000_000_000, '20.00亿'],
])('累计成交额按1亿元换算：%s + %s 显示 %s', async (first, last, expected) => {
  const current = session()
  current.rows[0].amount = first
  current.rows[1].amount = last
  await render([current])
  expect(field('累计额')).toBe(expected)
})

it('真实全日同价仍显示同价，不强行制造价差；空数据可展示', async () => {
  const current = session()
  current.rows = current.rows.map(row => ({ ...row, open: 10.5, high: 10.5, low: 10.5, close: 10.5 }))
  await render([current])
  for (const label of ['开', '高', '低', '收']) expect(field(label)).toBe('10.50')
  await render([])
  expect(host.textContent).toContain('0 个交易日')
  expect(host.textContent).not.toContain('10.50')
})
