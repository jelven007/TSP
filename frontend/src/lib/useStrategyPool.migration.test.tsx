// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { notifyManager, QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { expect, it, vi } from 'vitest'
import { useStrategyPool } from './useStrategyPool'
const values = vi.hoisted(() => ({
  daily: ['a', 's021', 's022', 's02', 'b'], minute: ['s021'],
  server: { ids: [] as string[], revision: 0 },
}))
vi.mock('./storage', () => ({ storage: {
  strategyPool: { get: () => values.daily, set: (v: string[]) => { values.daily = v } },
  strategyPoolMinute: { get: () => values.minute, remove: () => { values.minute = [] } },
} }))
vi.mock('./accountSession', () => ({ accountScope: () => 'migration-account' }))
vi.mock('./api', () => ({ api: {
  strategyPool: async () => values.server,
  strategyPoolSave: async ({ ids }: { ids: string[] }) => {
    values.server = { ids, revision: 1 }
    return values.server
  },
} }))
it('renames the stored strategy in place, removes the retired strategy and stays idempotent', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  notifyManager.setNotifyFunction(callback => { act(callback) })
  const host = document.createElement('div'); document.body.append(host)
  let pool: string[] = []
  function Probe() { pool = useStrategyPool().pool; return null }
  const root = createRoot(host)
  const client = new QueryClient()
  await act(async () => {
    root.render(<QueryClientProvider client={client}><Probe /></QueryClientProvider>)
  })
  try {
    await vi.waitFor(async () => {
      await act(async () => { await Promise.resolve() })
      expect(pool).toEqual(['a', 's02', 'b'])
      expect(values.daily).toEqual(pool)
      expect(values.minute).toEqual([])
    })
  } finally {
    await act(async () => root.unmount()); client.clear(); host.remove()
    notifyManager.setNotifyFunction(callback => { callback() })
  }
})
