// @vitest-environment jsdom
import { expect, it, vi } from 'vitest'
import { defaultStrategyResultColumns, loadStrategyResultColumns, resolveStrategyResultColumns, strategyResultGroups } from './strategy-result-columns'

const { get } = vi.hoisted(() => ({ get: vi.fn(() => ({})) }))
vi.mock('./storage', () => ({ storage: { strategySignalColumns: { get, set: vi.fn() } } }))
const removed = ['builtin:selection_rank', 'builtin:amount_mean_20d', 'builtin:score']
const s02 = 'custom_mvxxcp88'

it('migrates old S02 preferences without retaining the removed selection columns', () => {
  const old = defaultStrategyResultColumns('select')
  get.mockReturnValue({ select: old.map(({ id, visible }) => ({ id, visible })) })
  const columns = loadStrategyResultColumns(s02).select
  expect(columns.some(col => removed.includes(col.id))).toBe(false)
  const ids = columns.filter(col => col.visible).map(col => col.id)
  expect(ids[ids.indexOf('builtin:reference_change_pct') + 1]).toBe('builtin:reference_turnover_rate')
  expect(columns.find(col => col.id === 'builtin:reference_turnover_rate')?.label).toBe('参考日换手率')
  const oldLabel = columns.map(col => col.id === 'builtin:reference_turnover_rate'
    ? { ...col, label: '换手率', visible: false } : col)
  expect(resolveStrategyResultColumns('select', s02, oldLabel)
    .find(col => col.id === 'builtin:reference_turnover_rate')).toMatchObject({
      label: '参考日换手率', visible: false,
    })
  expect(strategyResultGroups('select', s02)[0].keys).toContain('reference_turnover_rate')
  expect(strategyResultGroups('select', s02)[0].keys).not.toContain('selection_rank')
})

it('preserves other strategy selection columns and S02 buy/sell columns', () => {
  expect(defaultStrategyResultColumns('select', 'custom_other').map(col => col.id))
    .toEqual(defaultStrategyResultColumns('select').map(col => col.id))
  for (const tab of ['buy', 'sell'] as const) {
    expect(defaultStrategyResultColumns(tab, s02)).toEqual(
      defaultStrategyResultColumns(tab).filter(col => col.id !== 'builtin:atr_rank'))
  }
})
