// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { notifyManager, QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { useStrategyPool } from './useStrategyPool'
import { api } from './api'
import { QK } from './queryKeys'

const state = vi.hoisted(() => ({
  stored: { ids: ['s01', 's02'], revision: 2 },
  local: [] as string[], old: [] as string[], scope: 'same-account',
}))
vi.mock('./accountSession', () => ({ accountScope: () => state.scope }))
vi.mock('./storage', () => ({ storage: {
  strategyPool: { get: () => state.local, set: (ids: string[]) => { state.local = ids } },
  strategyPoolMinute: { get: () => state.old, remove: () => { state.old = [] } },
} }))
vi.mock('./api', () => ({ api: { strategyPool: vi.fn(), strategyPoolSave: vi.fn() } }))
Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
const roots: Root[] = []
const clients: QueryClient[] = []

beforeEach(() => {
  notifyManager.setNotifyFunction(callback => { act(callback) })
  state.stored = { ids: ['s01', 's02'], revision: 2 }
  state.local = []; state.old = []; state.scope = 'same-account'
  vi.mocked(api.strategyPool).mockImplementation(async () => structuredClone(state.stored))
  vi.mocked(api.strategyPoolSave).mockImplementation(async request => {
    if (!request.migrate && request.revision !== state.stored.revision) throw new Error('记录已更新')
    if (request.migrate && state.stored.revision > 0) return structuredClone(state.stored)
    state.stored = { ids: request.ids, revision: state.stored.revision + 1 }
    return structuredClone(state.stored)
  })
})
afterEach(async () => {
  for (const root of roots.splice(0)) await act(async () => root.unmount())
  for (const client of clients.splice(0)) client.clear()
  vi.clearAllMocks()
  notifyManager.setNotifyFunction(callback => { callback() })
})

async function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const root = createRoot(document.createElement('div'))
  let value!: ReturnType<typeof useStrategyPool>
  function Probe() { value = useStrategyPool(); return null }
  roots.push(root); clients.push(client)
  await act(async () => root.render(<QueryClientProvider client={client}><Probe /></QueryClientProvider>))
  return { client, value: () => value }
}
async function settled(check: () => void) {
  await vi.waitFor(async () => { await act(async () => { await Promise.resolve() }); check() })
}

it('a fresh browser loads the same saved account pool with no local selection', async () => {
  const first = await mount()
  await settled(() => expect(first.value().pool).toEqual(['s01', 's02']))
  expect(api.strategyPoolSave).not.toHaveBeenCalled()
})

it('server selection replaces stale browser preferences including an explicitly empty pool', async () => {
  state.local = ['retired_local']
  state.stored = { ids: [], revision: 3 }
  const browser = await mount()
  await settled(() => expect(browser.value().pool).toEqual([]))
  expect(state.local).toEqual([])
  expect(state.stored).toEqual({ ids: [], revision: 3 })
})

it('a save is visible to a separate query client and does not start strategy execution', async () => {
  const first = await mount()
  const second = await mount()
  await settled(() => expect(second.value().pool).toEqual(['s01', 's02']))
  await act(async () => { await first.value().reorderPool(['s02']) })
  await act(async () => { await second.client.invalidateQueries({ queryKey: QK.strategyPool(state.scope) }) })
  await settled(() => expect(second.value().pool).toEqual(['s02']))
  expect(state.stored.ids).toEqual(['s02'])
})

it('read failure preserves local choices and never saves an empty fallback', async () => {
  state.local = ['s01']
  vi.mocked(api.strategyPool).mockRejectedValue(new Error('读取失败'))
  const browser = await mount()
  await settled(() => expect(browser.value().error).toBeTruthy())
  expect(state.local).toEqual(['s01'])
  expect(api.strategyPoolSave).not.toHaveBeenCalled()
})

it('write failure leaves the saved list and local migration evidence intact', async () => {
  const browser = await mount()
  await settled(() => expect(browser.value().pool).toEqual(['s01', 's02']))
  vi.mocked(api.strategyPoolSave).mockRejectedValue(new Error('保存失败'))
  await act(async () => { await expect(browser.value().reorderPool([])).rejects.toThrow('保存失败') })
  expect(browser.value().pool).toEqual(['s01', 's02'])
  expect(state.local).toEqual(['s01', 's02'])
})

it('migrates old nonempty browser choices without letting initial empty state overwrite them', async () => {
  state.local = ['s021', 'custom_existing']; state.old = ['s02']
  state.stored = { ids: [], revision: 0 }
  const browser = await mount()
  await settled(() => expect(browser.value().pool).toEqual(['s02', 'custom_existing']))
  expect(api.strategyPoolSave).toHaveBeenCalledWith(expect.objectContaining({
    ids: ['s02', 'custom_existing'], migrate: true,
  }))
})
