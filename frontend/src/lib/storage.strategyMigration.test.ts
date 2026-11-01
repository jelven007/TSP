// @vitest-environment jsdom
import { expect, it, vi } from 'vitest'
const saved = vi.hoisted(() => new Map<string, string>())
vi.mock('./accountSession', () => ({ accountStorage: {
  getItem: (key: string) => saved.get(key) ?? null,
  setItem: (key: string, value: string) => saved.set(key, value),
  removeItem: (key: string) => saved.delete(key),
} }))
import { storage } from './storage'
it('updates a saved backtest selection and removes retired cached results', () => {
  saved.set('strategy-backtest-last', JSON.stringify({
    selectedStrategy: 's021', result: { strategy_info: { id: 's021', name: 's021 · 净6% / 毛6%' },
      stats: { total_return: .18 } },
  }))
  const next = storage.strategyBacktestLast.get(null)!
  expect(next.selectedStrategy).toBe('s02')
  expect(next.result.strategy_info).toEqual({ id: 's02', name: 's02 · 净6% / 毛6%' })
  expect(next.result.stats.total_return).toBe(.18)
  saved.set('strategy-backtest-last', JSON.stringify({
    selectedStrategy: 's022', result: { strategy_info: { id: 's022' }, stats: { total_return: .16 } },
  }))
  expect(storage.strategyBacktestLast.get(null)?.result).toBeNull()
})
