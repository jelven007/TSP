import { describe, expect, it } from 'vitest'
import { backtestRuleDraft, equalEquityPct } from './paperRuleDraft'
import { BACKTEST_DEFAULTS_VERSION } from '@/pages/backtest/defaults'

const saved = {
  defaultsVersion: BACKTEST_DEFAULTS_VERSION, selectedStrategy: 's-one', maxPositions: '5', maxExposure: '80',
  positionSizing: 'equal', mode: 'position', entryFill: 'open_t+1', exitFill: 'close_t',
  fees: '2.5', stampTax: '', minimumCommission: '3', transferFee: '0.1', slippage: '7',
}

describe('backtest rule draft compatibility', () => {
  it('derives equal equity size and converts fee units once without replacing opening capacity', () => {
    const draft = backtestRuleDraft(saved, { id: 's-one', execution_backend: 'polars_expr' })
    expect(draft.rule).toEqual({
      match_kind: 'strategy', match_id: 's-one', max_positions: 5,
      max_total_exposure_pct: 80, size_mode: 'pct_equity', size_value: 16, sell_order_type: 'close',
    })
    expect(draft.fees).toEqual({
      commission_pct: 0.00025, stamp_tax_mode: 'historical', min_commission: 3,
      transfer_fee_pct: 0.00001, slippage_bps: 7,
    })
    expect(draft.notes.join(' ')).toContain('5分钟容量')
    expect(draft.rule).not.toHaveProperty('buy_order_type')
  })
  it('does not turn zero investment, weighted sizing or next minute into unlimited/equal/market', () => {
    const draft = backtestRuleDraft({
      ...saved, maxExposure: '0', positionSizing: 'score_weight', exitFill: 'signal_next_minute',
    }, { id: 's-one', execution_backend: 'minute_filter' })
    expect(draft.rule).not.toHaveProperty('max_total_exposure_pct')
    expect(draft.rule).not.toHaveProperty('size_value')
    expect(draft.rule).not.toHaveProperty('sell_order_type')
    for (const note of ['零投入', '评分加权', '下一分钟', '分钟容量']) {
      expect(draft.notes.join(' ')).toContain(note)
    }
  })
  it('rejects missing or unknown saved versions and cannot borrow a different strategy definition', () => {
    for (const state of [null, { ...saved, defaultsVersion: undefined }, { ...saved, defaultsVersion: 99 }]) {
      const draft = backtestRuleDraft(state, { id: 's-one', execution_backend: 'polars_expr' })
      expect(draft.available).toBe(false)
      expect(draft.rule).toEqual({})
      expect(draft.fees).toEqual({})
    }
    expect(backtestRuleDraft(saved, { id: 's-two', execution_backend: 'polars_expr' }).available).toBe(false)
  })
  it('keeps invalid/missing fees out of the patch and exposes full mode and overrides', () => {
    const draft = backtestRuleDraft({
      ...saved, fees: '', minimumCommission: '-5', transferFee: 'Infinity',
      mode: 'full', overrides: { stop_loss: 0.05 },
    }, { id: 's-one', execution_backend: 'polars_expr' })
    expect(draft.fees).not.toHaveProperty('commission_pct')
    expect(draft.fees).not.toHaveProperty('min_commission')
    expect(draft.fees).not.toHaveProperty('transfer_fee_pct')
    expect(draft.rule).not.toHaveProperty('size_value')
    expect(draft.notes.join(' ')).toContain('全量模拟')
    expect(draft.notes.join(' ')).toContain('策略覆盖')
  })
})

it('requires a finite positive integer slot count and positive total exposure for equal sizing', () => {
  expect(equalEquityPct('80', '5')).toBe(16)
  for (const [exposure, slots] of [['0', '5'], ['80', '0'], ['80', '1.5'], ['', '5'], ['101', '5']]) {
    expect(equalEquityPct(exposure, slots)).toBeNull()
  }
})
