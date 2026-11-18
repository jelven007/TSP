import { expect, it } from 'vitest'
import type { CustomSignal } from './api'
import { S01_EXIT_DEFAULTS, s01ExitDraft, s01SignalSummary } from './customSignals'

const volumeParams = {
  decline_3m: .002, volume_ratio_min: 1.5,
  start_time: '11:15:00', stop_time: '11:26:00', fallback_time: '13:30:00',
}
const legacy: CustomSignal = {
  id: 'morning_liquidation_s01', name: '早盘清仓_s01', kind: 'exit', enabled: true,
  timeframe: 'intraday', template: 's01_morning_exit', conditions: [],
  params: { vwap_buffer: .0003, peak_profit_min: .02, peak_retrace_min: .005,
    return_15m_max: -.003, decline_3m: .002, volume_ratio_min: 1.5,
    guard_time: '11:26', sell_deadline: '13:30' },
}

it('uses only volume decline thresholds and three clocks for new defaults and legacy edit drafts', () => {
  expect(S01_EXIT_DEFAULTS).toEqual(volumeParams)
  expect(s01ExitDraft(legacy)).toEqual(volumeParams)
  expect(legacy.params).toHaveProperty('vwap_buffer')
  const nineField = { ...legacy, params: { ...legacy.params, ...volumeParams } }
  expect(s01ExitDraft(nineField)).toEqual(volumeParams)
  expect(s01ExitDraft({ ...legacy, params: { ...volumeParams, decline_3m: .004 } }).decline_3m).toBe(.004)
})

it('describes current volume-only exits without removed conditions and preserves frozen summaries', () => {
  const summary = s01SignalSummary({ ...legacy, params: volumeParams }).join('；')
  expect(summary).toContain('放量下跌')
  expect(summary).toContain('同时满足')
  expect(summary).toContain('0.2%')
  expect(summary).toContain('1.5')
  expect(summary).not.toMatch(/均价|浮盈|15分钟|NaN/)
  expect(s01SignalSummary(legacy).join('；')).toContain('旧冻结规则')
  expect(s01SignalSummary(legacy).join('；')).toContain('低于均价')
})
