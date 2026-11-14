import { expect, it } from 'vitest'
import { loadBacktestDefaults } from './defaults'

it('supplies the five requested defaults without saved state', () => {
  expect(loadBacktestDefaults(null)).toMatchObject({
    defaultsVersion: 3,
    maxPositions: '5',
    stampTax: '1',
    slippage: '10',
    minimumCommission: '5',
    transferFee: '5',
  })
})

it('migrates only the five fields once and preserves later edits', () => {
  const first = loadBacktestDefaults({
    selectedStrategy: 'sample',
    maxPositions: '10',
    stampTax: '',
    slippage: '5',
    minimumCommission: '0',
    transferFee: '0',
    initialCapital: '500000',
  })

  expect(first).toMatchObject({
    defaultsVersion: 3,
    maxPositions: '5',
    stampTax: '1',
    slippage: '10',
    minimumCommission: '5',
    transferFee: '5',
    initialCapital: '500000',
  })
  expect(loadBacktestDefaults({ ...first, maxPositions: '7' }).maxPositions).toBe('7')
})

it('updates the previous holding default without resetting saved fees or other inputs', () => {
  const saved = {
    defaultsVersion: 1,
    maxPositions: '5',
    stampTax: '',
    slippage: '3',
    minimumCommission: '8',
    transferFee: '0',
    initialCapital: '1000000',
    start: '2026-10-01',
    result: { run_id: 'previous-run' },
  }

  expect(loadBacktestDefaults(saved)).toEqual({
    ...saved,
    defaultsVersion: 3,
    maxPositions: '5',
  })
  expect(saved.maxPositions).toBe('5')
})

it('preserves a custom holding limit from the previous defaults version', () => {
  expect(loadBacktestDefaults({ defaultsVersion: 1, maxPositions: '7', slippage: '3' }))
    .toEqual({ defaultsVersion: 3, maxPositions: '7', slippage: '3' })
})

it('supplies five positions when the previous defaults version has no holding limit', () => {
  expect(loadBacktestDefaults({ defaultsVersion: 1, slippage: '3' }))
    .toEqual({ defaultsVersion: 3, maxPositions: '5', slippage: '3' })
})

it('preserves a manually saved five-position limit after the migration', () => {
  const saved = { defaultsVersion: 3, maxPositions: '5', slippage: '3' }

  expect(loadBacktestDefaults(saved)).toBe(saved)
})

it('does not downgrade a newer defaults version', () => {
  const saved = { defaultsVersion: 4, maxPositions: '8', slippage: '3' }

  expect(loadBacktestDefaults(saved)).toBe(saved)
})

it('updates the version two four-position default and preserves saved fees and result', () => {
  const saved = { defaultsVersion: 2, maxPositions: '4', slippage: '12', transferFee: '0', result: { run_id: 'old' } }
  expect(loadBacktestDefaults(saved)).toEqual({ ...saved, defaultsVersion: 3, maxPositions: '5' })
  expect(saved.maxPositions).toBe('4')
})

it('preserves custom version two holding limits', () => {
  expect(loadBacktestDefaults({ defaultsVersion: 2, maxPositions: '7', slippage: '12' }))
    .toEqual({ defaultsVersion: 3, maxPositions: '7', slippage: '12' })
})
