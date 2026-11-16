import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { minuteRefetchInterval } from './kline'

beforeEach(() => {
  vi.useFakeTimers()
  vi.setSystemTime(new Date('2026-11-16T02:20:00Z'))
})

afterEach(() => vi.useRealTimers())

it('当日分钟暂时为空时继续刷新，数据源恢复后无需重新打开详情', () => {
  const interval = minuteRefetchInterval(10_000)
  expect(interval({ state: { data: { date: '2026-11-16', source: 'none', rows: [] } } })).toBe(10_000)
  expect(interval({ state: { data: {
    date: '2026-11-16', source: 'live', rows: [{ datetime: '2026-11-16 10:20:00' }],
  } } })).toBe(10_000)
})

it('历史日空数据和有效数据均不重复轮询', () => {
  for (const source of ['none', 'local']) {
    expect(minuteRefetchInterval(10_000)({ state: {
      data: { date: '2026-11-13', source, rows: [] },
    } })).toBe(false)
  }
})

it('收盘根尚未到达时继续刷新，15点30后收到收盘根才停表', () => {
  vi.setSystemTime(new Date('2026-11-16T07:31:00Z'))
  const interval = minuteRefetchInterval(10_000)
  expect(interval({ state: { data: { date: '2026-11-16', source: 'none', rows: [] } } })).toBe(10_000)
  expect(interval({ state: { data: {
    date: '2026-11-16', source: 'local', rows: [{ datetime: '2026-11-16 14:59:00' }],
  } } })).toBe(10_000)
  expect(interval({ state: { data: {
    date: '2026-11-16', source: 'local', rows: [{ datetime: '2026-11-16 15:00:00' }],
  } } })).toBe(false)
})

it('未配置刷新间隔时保持关闭', () => {
  expect(minuteRefetchInterval()({ state: { data: undefined } })).toBe(false)
})
