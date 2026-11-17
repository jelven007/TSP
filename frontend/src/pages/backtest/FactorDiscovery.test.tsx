// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { expect, it, vi } from 'vitest'
import { FactorDiscovery } from './FactorDiscovery'

const state = vi.hoisted(() => ({ loaded: [] as string[], run: vi.fn() }))
vi.mock('@/lib/api', () => ({ api: {
  factorColumns: async () => ({ columns: [] }), watchlistList: async () => ({ symbols: [] }),
  watchlistGroups: async () => ({ groups: [] }), factorBatch: state.run,
} }))
vi.mock('./FactorBacktest', () => { state.loaded.push('single'); return {
  FactorBacktest: ({ initialFactorName }: any) => <div data-single>{initialFactorName}</div>,
} })
vi.mock('./AutoMiningDialog', () => { state.loaded.push('auto'); return {
  AutoMiningDialog: ({ onClose }: any) => <button role="dialog" onClick={onClose}>close mining</button>,
} })
vi.mock('./AddFactorSignalDialog', () => { state.loaded.push('signal'); return { AddFactorSignalDialog: () => null } })
Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })

it('keeps single-factor charts and unopened mining/signal views out of the batch entry', async () => {
  const node = document.createElement('div')
  const root = createRoot(node)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const click = async (label: string) => {
    await act(async () => [...node.querySelectorAll('button')].find(b => b.textContent?.trim() === label)!.click())
  }
  try {
    await act(async () => root.render(<MemoryRouter><QueryClientProvider client={client}><FactorDiscovery /></QueryClientProvider></MemoryRouter>))
    expect(state.loaded).toEqual([])
    await click('单因子检验')
    await vi.waitFor(() => expect(node.querySelector('[data-single]')?.textContent).toBe('momentum_20d'))
    expect(state.loaded).toEqual(['single'])
    await click('批量筛选')
    await click('自动挖掘')
    await vi.waitFor(() => expect(node.querySelector('[role="dialog"]')).not.toBeNull())
    await click('close mining')
    expect(state.loaded).toEqual(['single', 'auto'])
    expect(state.run).not.toHaveBeenCalled()
  } finally { await act(async () => root.unmount()); client.clear() }
})
