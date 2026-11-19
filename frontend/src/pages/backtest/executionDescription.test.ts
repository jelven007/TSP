import { expect, it } from 'vitest'
import { customExecutionDescription } from './executionDescription'
import type { CustomSignal, StrategyDetail } from '@/lib/api'

const signal: CustomSignal = {
  id: 'exit', name: '当前清仓', kind: 'exit', timeframe: 'intraday',
  template: 'morning_liquidation', enabled: true, conditions: [],
  params: { start_time: null, rise_pct: null, fall_pct: null, decline_3m_pct: null,
    volume_ratio_min: null, time: null, fallback_time: null },
}
const detail = { source: 'custom', execution_backend: 'minute_filter',
  entry_signals: [], exit_signals: ['csgi_exit'] } as unknown as StrategyDetail

it('does not invent thresholds when all saved signal parameters are null', () => {
  const value = customExecutionDescription(detail, [signal], {})
  expect(value.exits[0].lines).toEqual(['全部参数为空，早盘清仓模板不触发出场'])
  expect(value.entryLabel).toBe('未选择入场触发器')
  expect(value.exits[0].lines.join('')).not.toContain('6%')
})

it('uses current overrides and reports disabled or missing signal definitions explicitly', () => {
  const value = customExecutionDescription(detail, [{ ...signal, enabled: false }], {})
  expect(value.exits[0].lines).toEqual(['已禁用，不产生买卖信号'])
  expect(customExecutionDescription(detail, [signal], { exit_signals: [] }).exitLabel).toBe('未选择出场触发器')
  expect(customExecutionDescription(detail, [], {}).exits[0].lines).toEqual(['信号定义尚未确认'])
})

it('describes each complete minute high for buys and low for morning sales', () => {
  const late: CustomSignal = { ...signal, id: 'late', kind: 'entry', template: 'late_session_strength' }
  const value = customExecutionDescription(
    { ...detail, entry_signals: ['csgi_late'] }, [late, signal], {},
  )
  expect(value.entry).toContain('每分钟K线的最高价')
  expect(value.exit).toContain('每分钟K线的最低价')
})
