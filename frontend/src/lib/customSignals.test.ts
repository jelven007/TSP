import { expect, it } from 'vitest'
import { MORNING_LIQUIDATION_DEFAULTS, morningLiquidationSummary } from './customSignals'

it('offers independent return thresholds and both default clocks', () => {
  expect(MORNING_LIQUIDATION_DEFAULTS).toEqual({
    time: '10:30:00', fallback_time: '10:45:00', rise_pct: 6, fall_pct: 6,
  })
  const lines = morningLiquidationSummary({
    id: 'morning', name: '早盘清仓', kind: 'exit', timeframe: 'intraday',
    template: 'morning_liquidation', enabled: true, conditions: [],
    params: { time: '10:30:00', fallback_time: '10:45:00', rise_pct: 8, fall_pct: 2 } as any,
  }).join(' ')
  expect(lines).toContain('止损时间')
  expect(lines).toContain('兜底时间')
  expect(lines).toContain('8%')
  expect(lines).toContain('2%')
  expect(lines).toContain('原始昨收')
})

it('labels stored legacy amplitude semantics until the signal is edited and saved', () => {
  const lines = morningLiquidationSummary({
    id: 'morning', name: '早盘清仓', kind: 'exit', timeframe: 'intraday',
    template: 'morning_liquidation', enabled: true, conditions: [],
    params: { time: '10:15:00', amplitude_pct: 5 },
  }).join(' ')
  expect(lines).toContain('旧振幅')
  expect(lines).toContain('5%')
  expect(lines).toContain('10:15:00')
  expect(lines).not.toContain('10:45:00')
})
