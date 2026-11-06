// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { expect, it, vi } from 'vitest'
import { ResearchHistoryDialog } from './ResearchHistoryDialog'

vi.mock('@/lib/accountSession', () => ({ multitenancyEnabled: () => false }))
vi.mock('@/lib/backtestTask', () => ({ attachBacktestJob: vi.fn() }))
vi.mock('@/lib/optimizerTask', () => ({ attachOptimizeJob: vi.fn() }))
vi.mock('@/lib/walkforwardTask', () => ({ attachWalkForwardJob: vi.fn() }))
vi.mock('@/lib/api', () => ({ api: {
  researchJobs: async () => ({ items: [] }),
  researchOriginals: async () => ({ items: [] }),
} }))

it.each([
  [{ slippage_bps: 10, params: {} }, '10 基点'],
  [{ slippage_bps: 0, params: {} }, '0 基点'],
  [{ params: { slippage: .002 } }, '20 基点'],
])('displays the saved run slippage in its correct unit (%j)', async (config, expected) => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  const client = new QueryClient({ defaultOptions: { queries: { staleTime: Infinity } } })
  client.setQueryData(['research-jobs'], { items: [{
    id: 'c88c133a5e424dadb0f6291e7e3dace2', kind: 'backtest', status: 'succeeded',
    created_at: '2026-11-06T12:53:18Z', metadata: { source: 'manual' },
    config: { strategy_id: 'custom_mvxxcp88', start: '2026-06-29', end: '2026-11-06',
      initial_capital: 1000000, ...config },
  }] })
  const host = document.createElement('div')
  document.body.append(host)
  const root = createRoot(host)
  try {
    await act(async () => root.render(
      <MemoryRouter><QueryClientProvider client={client}>
        <ResearchHistoryDialog onClose={() => {}} />
      </QueryClientProvider></MemoryRouter>,
    ))
    expect(document.body.textContent).toContain(expected)
  } finally {
    await act(async () => root.unmount())
    client.clear()
    host.remove()
  }
})
