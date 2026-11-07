// @vitest-environment jsdom
import { beforeEach, expect, it, vi } from 'vitest'
import { defaultStrategyResultColumns, loadStrategyResultColumns, saveStrategyResultColumns } from './strategy-result-columns'

const { get, set } = vi.hoisted(() => ({ get: vi.fn(), set: vi.fn() }))
vi.mock('./storage', () => ({ storage: { strategySignalColumns: { get, set } } }))
const inserted = ['builtin:volume', 'builtin:amount', 'builtin:suggested_sell_price']
beforeEach(() => { get.mockReset().mockReturnValue({}); set.mockReset() })

it('places all three preselection ATR columns immediately after the existing selection score', () => {
  const ids = loadStrategyResultColumns().select.filter(col => col.visible).map(col => col.id)
  expect(ids.slice(ids.indexOf('builtin:score'), ids.indexOf('builtin:score') + 4))
    .toEqual(['builtin:score', 'builtin:atr14_previous_close', 'builtin:atr_rank', 'builtin:atr_rank_score'])
})

it('places the three sell columns immediately after the signal score for new users', () => {
  const columns = loadStrategyResultColumns().sell.filter(col => col.visible)
  const ids = columns.map(col => col.id)
  expect(ids.slice(ids.indexOf('builtin:score'), ids.indexOf('builtin:live_price') + 1))
    .toEqual(['builtin:score', ...inserted, 'builtin:live_price'])
})

it('inserts new sell columns at the score in saved preferences without resetting existing order or visibility', () => {
  const previous = defaultStrategyResultColumns('sell').filter(col => !inserted.includes(col.id))
    .map(({ id, visible }) => ({ id, visible: id === 'builtin:score' ? false : visible }))
  const trigger = previous.splice(previous.findIndex(col => col.id === 'builtin:close'), 1)[0]
  previous.push(trigger)
  get.mockReturnValue({ sell: [...previous, { id: 'builtin:unknown', visible: true }, previous[0]] })
  const columns = loadStrategyResultColumns().sell
  expect(columns.filter(col => !inserted.includes(col.id)).map(({ id, visible }) => ({ id, visible })))
    .toEqual(previous)
  const ids = columns.map(col => col.id)
  expect(ids.slice(ids.indexOf('builtin:score') + 1, ids.indexOf('builtin:score') + 4)).toEqual(inserted)
  expect(columns.filter(col => inserted.includes(col.id)).every(col => col.visible)).toBe(true)
})

it('preserves a subsequently customized position and visibility of the new columns when reopened', () => {
  const config = loadStrategyResultColumns()
  const amount = config.sell.splice(config.sell.findIndex(col => col.id === 'builtin:amount'), 1)[0]
  config.sell.push({ ...amount, visible: false })
  saveStrategyResultColumns(config)
  get.mockReturnValue(set.mock.calls[0][0])
  expect(loadStrategyResultColumns().sell).toEqual(config.sell)
})
