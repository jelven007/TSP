// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { expect, it, vi } from 'vitest'
import { Regime } from './Regime'

const reads = vi.hoisted(() => ({
  history: vi.fn(async () => ({ rows: [] })), states: vi.fn(async () => ({ rows: [] })),
  phases: vi.fn(async () => ({ segments: [] })), mainline: vi.fn(async () => ({ rows: [], leaders: [] })),
}))
vi.mock('@/lib/api', async original => ({
  ...await original<typeof import('@/lib/api')>(),
  api: { regimeCoverage: async () => ({ rows: 250 }), regimeHistory: reads.history,
    regimeStates: reads.states, regimePhases: reads.phases, regimeMainline: reads.mainline },
}))
vi.mock('@/lib/echarts', () => ({ init: vi.fn() }))
vi.mock('@/lib/accountSession', () => ({ hasPermission: () => false }))
Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })

it('requests phase-only data when its view opens and refreshes invalidated data on reopening', async () => {
  const node = document.createElement('div')
  const root = createRoot(node)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const click = async (label: string) => {
    await act(async () => [...node.querySelectorAll('button')].find(b => b.textContent === label)!.click())
  }
  try {
    await act(async () => root.render(<QueryClientProvider client={client}><Regime /></QueryClientProvider>))
    await vi.waitFor(() => expect(reads.history).toHaveBeenCalledOnce())
    expect(reads.phases).not.toHaveBeenCalled()
    expect(reads.mainline).not.toHaveBeenCalled()
    await click('情绪周期')
    await vi.waitFor(() => expect(reads.mainline).toHaveBeenCalledOnce())
    expect(reads.phases).toHaveBeenCalledOnce()
    await click('环境')
    await act(async () => { await client.invalidateQueries({ queryKey: ['regime-mainline'] }) })
    expect(reads.mainline).toHaveBeenCalledOnce()
    await click('情绪周期')
    await vi.waitFor(() => expect(reads.mainline).toHaveBeenCalledTimes(2))
  } finally { await act(async () => root.unmount()); client.clear() }
})
