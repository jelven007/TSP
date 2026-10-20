import { expect, it } from 'vitest'
import type { NullableMinuteKlineRow } from '@/lib/api'
import { computeIntradayAverage, summarizeMinutes } from './intraday-chart'

it('summarizes available prices without converting missing OHLCV to zero', () => {
  const rows = [
    {
      datetime: '2026-09-09 09:30:00',
      open: null,
      high: null,
      low: 2998,
      close: 2999,
      volume: 100,
      amount: 29_990_000,
    },
    {
      datetime: '2026-09-09 09:31:00',
      open: null,
      high: 3002,
      low: null,
      close: null,
      volume: null,
      amount: null,
    },
  ] satisfies NullableMinuteKlineRow[]

  expect(summarizeMinutes(rows)).toEqual({
    datetime: '2026-09-09 09:30:00',
    open: null,
    high: 3002,
    low: 2998,
    close: null,
    volume: null,
    amount: null,
  })
})

it('stops calculating the average after volume becomes unavailable', () => {
  const rows = [
    {
      datetime: '2026-09-09 09:30:00',
      open: 10,
      high: 10,
      low: 10,
      close: 10,
      volume: 100,
      amount: 100_000,
    },
    {
      datetime: '2026-09-09 09:31:00',
      open: 10,
      high: 10,
      low: 10,
      close: 10,
      volume: null,
      amount: 100_000,
    },
  ] satisfies NullableMinuteKlineRow[]

  expect(computeIntradayAverage(rows)).toEqual([10, null])
})
