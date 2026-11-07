import { expect, it } from 'vitest'
import { LATE_SESSION_DEFAULTS, MORNING_LIQUIDATION_DEFAULTS, lateSessionSummary, morningLiquidationSummary } from './customSignals'

it('summarizes new check time and preserves frozen legacy scheduled time', () => {
  const signal = { id: 'late', name: '尾盘建仓', kind: 'entry' as const, enabled: true, conditions: [] }
  expect(LATE_SESSION_DEFAULTS.evaluation_delay_minutes).toBe(0.5)
  const current = lateSessionSummary({ ...signal, params: LATE_SESSION_DEFAULTS }).join(' ')
  expect(current).toContain('启动时间 14:30:00')
  expect(current).toContain('确认时间 14:30:30')
  expect(current).not.toContain('编辑保存后')
  expect(current).toContain('K线截止 14:30:00')
  expect(current).toContain('7 个')
  expect(current).toContain('6 次')
  const exact = lateSessionSummary({
    ...signal, params: { ...LATE_SESSION_DEFAULTS, time: '14:30:20' },
  }).join(' ')
  expect(exact).toContain('启动时间 14:30:20')
  expect(exact).toContain('确认时间 14:30:50')
  expect(exact).toContain('K线截止 14:30:00')
  const { evaluation_delay_minutes: _delay, ...legacyParams } = LATE_SESSION_DEFAULTS
  expect(lateSessionSummary({ ...signal, params: legacyParams }).join(' ')).toContain('旧判断时间 14:30:00')
  expect(lateSessionSummary({ ...signal, params: { ...LATE_SESSION_DEFAULTS, time: '14:58:00' } })
    .join(' ')).toContain('确认时间 14:58:30')
  const previous = lateSessionSummary({ ...signal, params: {
    ...LATE_SESSION_DEFAULTS, evaluation_delay_minutes: 1.5,
  } }).join(' ')
  expect(previous).toContain('确认时间 14:31:30')
  expect(previous).toContain('确认延迟 90秒')
  expect(previous).not.toContain('编辑保存后改为')
})

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
