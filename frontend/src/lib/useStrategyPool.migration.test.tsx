// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { expect, it, vi } from 'vitest'
import { useStrategyPool } from './useStrategyPool'
const values = vi.hoisted(() => ({ daily: ['a', 's021', 's022', 's02', 'b'], minute: ['s021'] }))
vi.mock('./storage', () => ({ storage: {
  strategyPool: { get: () => values.daily, set: (v: string[]) => { values.daily = v } },
  strategyPoolMinute: { get: () => values.minute, remove: () => { values.minute = [] } },
} }))
it('renames the stored strategy in place, removes the retired strategy and stays idempotent', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  const host = document.createElement('div'); document.body.append(host)
  let pool: string[] = []
  function Probe() { pool = useStrategyPool().pool; return null }
  const root = createRoot(host)
  await act(async () => root.render(<Probe />))
  expect(pool).toEqual(['a', 's02', 'b'])
  expect(values.daily).toEqual(pool)
  expect(values.minute).toEqual([])
  await act(async () => root.unmount()); host.remove()
})
