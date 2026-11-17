import { expect, it } from 'vitest'
import { loadBacktestDefaults } from './defaults'

it('defaults liquidation to the five-minute window starting after the signal', () => {
  expect(loadBacktestDefaults(null)).toMatchObject({
    defaultsVersion: 5, exitFill: 'signal_next_minute',
  })
})

it.each([1, 2, 3, 4])('adds the liquidation default to version %s without changing saved fees', defaultsVersion => {
  expect(loadBacktestDefaults({ defaultsVersion, maxPositions: '7', slippage: '3' }))
    .toEqual({ defaultsVersion: 5, maxPositions: '7', slippage: '3', exitFill: 'signal_next_minute' })
})

it.each(['close_t', 'open_t+1', 'signal_next_minute'])('preserves an explicit liquidation choice %s', exitFill => {
  expect(loadBacktestDefaults({ defaultsVersion: 4, exitFill, maxPositions: '5', slippage: '3' }))
    .toEqual({ defaultsVersion: 5, exitFill, maxPositions: '5', slippage: '3' })
  expect(loadBacktestDefaults({ exitFill }).exitFill).toBe(exitFill)
})

it.each(['close_t', 'open_t+1'])('preserves the historical matching choice %s', matching => {
  expect(loadBacktestDefaults({ defaultsVersion: 4, matching }).exitFill).toBe(matching)
  expect(loadBacktestDefaults({ matching }).exitFill).toBe(matching)
})

it('supplies the requested defaults without saved state', () => {
  expect(loadBacktestDefaults(null)).toMatchObject({
    defaultsVersion: 5,
    exitFill: 'signal_next_minute',
    maxPositions: '4',
    stampTax: '1',
    slippage: '10',
    minimumCommission: '5',
    transferFee: '5',
  })
})

it('migrates the requested fields once and preserves later edits', () => {
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
    defaultsVersion: 5,
    exitFill: 'signal_next_minute',
    maxPositions: '4',
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
    defaultsVersion: 5,
    exitFill: 'signal_next_minute',
    maxPositions: '4',
  })
  expect(saved.maxPositions).toBe('5')
})

it('preserves a custom holding limit from the previous defaults version', () => {
  expect(loadBacktestDefaults({ defaultsVersion: 1, maxPositions: '7', slippage: '3' }))
    .toEqual({ defaultsVersion: 5, maxPositions: '7', slippage: '3', exitFill: 'signal_next_minute' })
})

it('supplies four positions when the previous defaults version has no holding limit', () => {
  expect(loadBacktestDefaults({ defaultsVersion: 1, slippage: '3' }))
    .toEqual({ defaultsVersion: 5, maxPositions: '4', slippage: '3', exitFill: 'signal_next_minute' })
})

it('preserves a manually saved five-position limit after the migration', () => {
  const saved = { defaultsVersion: 4, maxPositions: '5', slippage: '3' }

  expect(loadBacktestDefaults(saved)).toEqual({ ...saved, defaultsVersion: 5, exitFill: 'signal_next_minute' })
})

it('does not downgrade a newer defaults version', () => {
  const saved = { defaultsVersion: 6, maxPositions: '8', slippage: '3' }

  expect(loadBacktestDefaults(saved)).toBe(saved)
})

it('preserves the version two four-position default, saved fees and result', () => {
  const saved = { defaultsVersion: 2, maxPositions: '4', slippage: '12', transferFee: '0', result: { run_id: 'old' } }
  expect(loadBacktestDefaults(saved)).toEqual({ ...saved, defaultsVersion: 5, maxPositions: '4', exitFill: 'signal_next_minute' })
  expect(saved.maxPositions).toBe('4')
})

it('preserves custom version two holding limits', () => {
  expect(loadBacktestDefaults({ defaultsVersion: 2, maxPositions: '7', slippage: '12' }))
    .toEqual({ defaultsVersion: 5, maxPositions: '7', slippage: '12', exitFill: 'signal_next_minute' })
})

it('preserves a manually saved five-position limit from version two', () => {
  const saved = { defaultsVersion: 2, maxPositions: '5', slippage: '12' }
  expect(loadBacktestDefaults(saved)).toEqual({ ...saved, defaultsVersion: 5, exitFill: 'signal_next_minute' })
})

it('updates the version three five-position default without changing other saved inputs', () => {
  const saved = {
    defaultsVersion: 3,
    maxPositions: '5',
    selectedStrategy: 'sample',
    stampTax: '',
    slippage: '3',
    minimumCommission: '8',
    transferFee: '0',
    initialCapital: '1000000',
    start: '2026-10-01',
    end: '2026-11-17',
    result: { run_id: 'previous-run' },
  }
  expect(loadBacktestDefaults(saved)).toEqual({ ...saved, defaultsVersion: 5, maxPositions: '4', exitFill: 'signal_next_minute' })
  expect(saved.maxPositions).toBe('5')
})

it('preserves custom version three holding limits', () => {
  expect(loadBacktestDefaults({ defaultsVersion: 3, maxPositions: '7', slippage: '12' }))
    .toEqual({ defaultsVersion: 5, maxPositions: '7', slippage: '12', exitFill: 'signal_next_minute' })
})

it.each([2, 3])('supplies four positions for a missing version %s holding limit', (defaultsVersion) => {
  expect(loadBacktestDefaults({ defaultsVersion, slippage: '12' }))
    .toEqual({ defaultsVersion: 5, maxPositions: '4', slippage: '12', exitFill: 'signal_next_minute' })
})
