import { expect, it } from 'vitest'
import { loadBacktestDefaults } from './defaults'

it('supplies the five requested defaults without saved state', () => {
  expect(loadBacktestDefaults(null)).toMatchObject({
    defaultsVersion: 1,
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
    defaultsVersion: 1,
    maxPositions: '5',
    stampTax: '1',
    slippage: '10',
    minimumCommission: '5',
    transferFee: '5',
    initialCapital: '500000',
  })
  expect(loadBacktestDefaults({ ...first, maxPositions: '7' }).maxPositions).toBe('7')
})
