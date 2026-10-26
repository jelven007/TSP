import { expect, it } from 'vitest'
import { klineDailyQueryOptions, mergeLatestKlineRow } from './kline'

it('同股票切周期不占用上一周期的图表数据，日期变化可保留同周期数据', () => {
  const range = { start: '2026-10-01', end: '2026-10-26' }
  const daily = klineDailyQueryOptions('600000.SH', range)
  const minute = klineDailyQueryOptions('600000.SH', range, undefined, '5m')
  const previous = { rows: ['daily'] }
  expect(daily.queryKey).not.toEqual(minute.queryKey)
  expect(minute.placeholderData(previous, { queryKey: daily.queryKey })).toBeUndefined()
  expect(minute.placeholderData(previous, { queryKey: minute.queryKey })).toBe(previous)
  expect(minute.placeholderData(previous, { queryKey: ['kline', '000001.SZ'] })).toBeUndefined()
})

it('实时日K增量不能混入分钟K或周K缓存', () => {
  const row = { date: '2026-10-26', open: 10, high: 11, low: 9, close: 10 }
  for (const freq of ['1m', '5m', '10m', '15m', '1w'] as const) {
    const current = { symbol: '600000.SH', freq, rows: [] }
    expect(mergeLatestKlineRow(current, { symbol: '600000.SH', source: 'live', row })).toBe(current)
  }
})
