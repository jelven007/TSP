// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { expect, it, vi } from 'vitest'
import { CustomSignalDialog } from './CustomSignalDialog'
import { StrategyMonitorTasks } from '@/components/screener/StrategyMonitorTasks'
import { api, type StrategyMonitoring } from '@/lib/api'
import { QK } from '@/lib/queryKeys'

vi.mock('@/components/Modal', () => ({
  Modal: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  useModalPortalContainer: () => document.body,
}))
Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })

it('shows the saved signal period together with its earlier live execution deadline', async () => {
  const host = document.createElement('div')
  const root = createRoot(host)
  const value = {
    strategy_id: 'custom_s03', enabled: true, as_of: '2026-11-20',
    tasks: {
      buy: { state: 'expired', windows: [{ start: '14:50:00', end: '15:00:00' }],
        execution_deadline: '14:55:00' },
      sell: { state: 'waiting', windows: [{ start: '11:00:00', end: '11:30:00' }] },
      select: { state: 'waiting', windows: [{ start: '20:00:00', end: '22:00:00' }] },
    },
  } as StrategyMonitoring
  try {
    await act(async () => root.render(<StrategyMonitorTasks value={value} />))
    expect(host.textContent).toContain('14:50:00～15:00:00')
    expect(host.textContent).toContain('实时入场截止：14:55:00')
  } finally { await act(async () => root.unmount()) }
})

it('invalidates existing monitor times immediately after a signal is saved', async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  client.setQueryData(['strategy-monitoring'], { strategies: [] })
  client.setQueryData(QK.customSignalsOptions, { fields: [], operators: [], kinds: [] })
  vi.spyOn(api, 'customSignalsOptions').mockResolvedValue({ fields: [], operators: [], kinds: [] })
  vi.spyOn(api, 'strategyAiStatus').mockResolvedValue({ configured: false } as never)
  vi.spyOn(api, 'customSignalSave').mockImplementation(async signal => ({ ok: true, signal }))
  const host = document.createElement('div')
  const root = createRoot(host)
  try {
    await act(async () => root.render(<QueryClientProvider client={client}><MemoryRouter>
      <CustomSignalDialog open onClose={() => {}} signal={{
        id: 'morning', name: '早盘清仓', kind: 'exit', enabled: true,
        timeframe: 'intraday', template: 'morning_liquidation', conditions: [],
        params: { start_time: '11:00:00', time: '11:25:00', fallback_time: '11:30:00',
          rise_pct: null, fall_pct: null, decline_3m_pct: .2, volume_ratio_min: 1.5 },
      }} />
    </MemoryRouter></QueryClientProvider>))
    await act(async () => [...host.querySelectorAll('button')].find(b => b.textContent?.includes('保存'))!.click())
    expect(client.getQueryState(['strategy-monitoring'])?.isInvalidated).toBe(true)
  } finally {
    await act(async () => root.unmount())
    client.clear()
    vi.restoreAllMocks()
  }
})
