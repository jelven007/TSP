import { expect, it } from 'vitest'
import { strategyLotStocks, strategyLotRisk } from './strategyLotDraft'

it('uses only the current rows and their actual buy quantities and average prices', () => {
  expect(strategyLotStocks([
    { symbol: '600001.SH', name: '甲', filled_shares: 1200, buy_price: 10.25, close: 99 },
    { symbol: '600002.SH', filled_shares: 0, buy_price: 0, close: 20 },
    { symbol: '600003.SH', shares: 900, entry_price: 15 },
    { symbol: '600001.SH', filled_shares: 800, buy_price: 11 },
    { symbol: null },
  ])).toEqual([
    { symbol: '600001.SH', name: '甲', qty: '1200', cost: '10.25' },
    { symbol: '600002.SH', name: '', qty: '0', cost: '' },
    { symbol: '600003.SH', name: '', qty: '', cost: '' },
  ])
})

it('converts effective strategy fractions into holding percentage values', () => {
  expect(strategyLotRisk({ params_defaults: { take_profit_pct: .06, stop_loss_pct: .055 },
    take_profit: .9, stop_loss: .8 })).toEqual({ target_pct: '6', stop_pct: '5.5' })
  expect(strategyLotRisk({ params_defaults: {}, take_profit: .08, stop_loss: .03 }))
    .toEqual({ target_pct: '8', stop_pct: '3' })
})

it('uses archived parameter overrides instead of current settings and respects disabled values', () => {
  expect(strategyLotRisk({ params: { take_profit_pct: .06, stop_loss_pct: .05 },
    overrides: { params: { take_profit_pct: .07, stop_loss_pct: 0 } } }))
    .toEqual({ target_pct: '7', stop_pct: '0' })
  expect(strategyLotRisk({ params: {}, overrides: { take_profit: .09, stop_loss: null } }))
    .toEqual({ target_pct: '9', stop_pct: '' })
  expect(strategyLotRisk({ params: { take_profit_pct: .1 },
    overrides: { params: { take_profit_pct: null } } }))
    .toEqual({ target_pct: '', stop_pct: '' })
})

it('leaves unknown or invalid risk values blank', () => {
  expect(strategyLotRisk({ params: { take_profit_pct: NaN, stop_loss_pct: -1 }, overrides: {} }))
    .toEqual({ target_pct: '', stop_pct: '' })
})
