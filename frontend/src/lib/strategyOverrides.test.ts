import { describe, expect, it } from 'vitest'
import type { StrategyDetail } from './api'
import { buildDefaultOverrides } from './strategyOverrides'

describe('research strategy configuration', () => {
  it('carries entry conditions into backtest and optimizer without sharing mutable rows', () => {
    const detail = {
      basic_filter: {}, entry_signals: [], exit_signals: [], scoring: {},
      overlay_filter: [{ left: 'close', op: '>', right: 'field:ma20', rightDays: 2 }],
    } as unknown as StrategyDetail
    const overrides = buildDefaultOverrides(detail)
    expect(overrides.overlay_filter).toEqual(detail.overlay_filter)
    overrides.overlay_filter[0].right = '0'
    expect(detail.overlay_filter?.[0].right).toBe('field:ma20')
  })

  it('represents an unfiltered legacy strategy as an empty condition list', () => {
    const detail = { basic_filter: {}, entry_signals: [], exit_signals: [], scoring: {} } as unknown as StrategyDetail
    expect(buildDefaultOverrides(detail).overlay_filter).toEqual([])
  })
})
