import { expect, it } from 'vitest'
import { toOHLC } from './StockDailyKChart'

it('分钟K保留日期和时间，不将未知OHLC伪造成0或NaN蜡烛', () => {
  const base = { open: 10, high: 11, low: 9, close: 10.5, volume: 100 }
  const rows = [
    { ...base, date: '2026-10-23 09:35:00' },
    { ...base, date: '2026-10-23 09:40:00' },
    { ...base, date: '2026-10-23 09:45:00', high: null },
    { ...base, date: '2026-10-23 09:50:00', low: NaN },
  ] as any
  const result = toOHLC(rows)
  expect(result.map(r => r.date)).toEqual(['2026-10-23 09:35:00', '2026-10-23 09:40:00'])
  expect(result[1].close).toBe(10.5)
})

it('已知OHLC但未知成交量时保留未知量，不把它当作零成交', () => {
  const rows = [{ date: '2026-10-23 09:35:00', open: 10, high: 11, low: 9, close: 10.5, volume: null }] as any
  expect(toOHLC(rows)[0].volume).toBeUndefined()
})
