// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, it, vi } from 'vitest'
import { Data } from './Data'
import { QK } from '@/lib/queryKeys'

Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
afterEach(() => vi.unstubAllGlobals())

it.each(['succeeded', 'failed'] as const)('refreshes cached charts after a %s repair, including partial writes', async (status) => {
  vi.stubGlobal('fetch', vi.fn(async (path: string) => {
    let value: unknown = {}
    if (path === '/api/settings/data-sources') value = { builtin: [], plugins: [], custom: [] }
    else if (path === '/api/settings/capability-matrix') value = { capabilities: [] }
    else if (path === '/api/settings/kline-sync') value = Object.fromEntries(
      ['1m', '5m', '10m', '15m', '1d', '1w'].map(period => [period, {
        enabled: true, interval_seconds: 60, windows: [['09:15', '11:35'], ['13:00', '15:20']], time: '16:00',
      }]),
    )
    else if (path === '/api/ext-data') value = { items: [] }
    else if (path === '/api/regime/coverage') value = null
    else if (path === '/api/data/status') value = { storage: {}, indicators_ready: true }
    else if (path.startsWith('/api/pipeline/jobs?')) value = { active_id: 'repair-1', jobs: [] }
    else if (path === '/api/pipeline/jobs/repair-1') value = {
      id: 'repair-1', kind: 'kline_1m', status, stage: 'sync_1m', progress: 95, stage_pct: 100,
      log: [], started_at: null, finished_at: null, duration_s: null,
      result: status === 'succeeded' ? { period: '1m', rows: 240, universe_size: 1 } : null,
      error: status === 'failed' ? '部分批次失败' : null,
    }
    return new Response(JSON.stringify(value))
  }))
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const keys = [
    QK.kline('600000.SH', '2026-10-01', '2026-10-26'),
    QK.klineLatest('600000.SH'),
    QK.klineMinute('600000.SH', '2026-10-26'),
    QK.klineMinuteRange('600000.SH', 5),
    QK.minuteBatch('600000.SH'),
  ]
  keys.forEach(key => client.setQueryData(key, [{ close: 10 }]))
  client.setQueryData(['unrelated-chart'], [{ value: 123 }])
  const host = document.createElement('div')
  document.body.append(host)
  const root = createRoot(host)
  try {
    await act(async () => root.render(
      <QueryClientProvider client={client}><MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}><Data /></MemoryRouter></QueryClientProvider>,
    ))
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 100)) })
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)) })
    expect(host.textContent).toContain('repair-1')
    expect(client.getQueryData(QK.pipelineJob('repair-1'))).toMatchObject({ status })
    for (const key of keys) expect(client.getQueryState(key)?.isInvalidated).toBe(true)
    expect(client.getQueryState(['unrelated-chart'])?.isInvalidated).toBe(false)
  } finally {
    await act(async () => root.unmount())
    host.remove()
    client.clear()
  }
})
