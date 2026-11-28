// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider, useQuery } from '@tanstack/react-query'
import { afterEach, expect, it, vi } from 'vitest'
import { Data } from './Data'
import { QK } from '@/lib/queryKeys'
import { storage } from '@/lib/storage'
import { ToastContainer } from '@/components/Toast'

Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
afterEach(() => {
  vi.unstubAllGlobals()
  localStorage.clear()
})

it('keeps readiness distinct from the navigation summary and links to the related strategy task context', async () => {
  const fetchMock = vi.fn(async (url: string) => new Response(JSON.stringify(
    url === '/api/pipeline/jobs?limit=15&include_readiness=true' ? {
      active_id: null, jobs: [],
      schedule_readiness: {
        automatic_enabled: true, observed_at: '2026-11-28T06:53:00+08:00',
        stock_status: { target_date: '2026-11-30', status: 'failed', coverage: 'unverified',
          scheduled_for: '2026-11-27T20:30:00+08:00', counts: { pending: 5224 } },
        post_close: { trade_date: '2026-11-27', sync: { status: 'failed' },
          daily: { status: 'succeeded' }, minute: { status: 'not_run' },
          review: { status: 'succeeded', coverage: 'complete', valid: 5219, remaining: 0 } },
        next_check: { kind: 'evening_status', due_at: '2026-11-28T20:30:00+08:00' },
      },
    } : url === '/api/pipeline/jobs?limit=1' ? { active_id: null, jobs: [] }
      : url === '/api/settings/capability-matrix' ? { capabilities: [] }
      : url === '/api/settings/data-sources' ? { builtin: [], plugins: [], custom: [] }
        : url === '/api/data/status' ? { storage: {}, indicators_ready: true }
          : url === '/api/regime/coverage' ? null
            : url === '/api/ext-data' ? { items: [] }
          : url === '/api/settings/kline-sync' ? Object.fromEntries(
            ['1m', '5m', '10m', '15m', '1d', '1w'].map(period => [period, {
              enabled: true, interval_seconds: 60, windows: [], time: '16:00',
            }]),
          ) : {})))
  vi.stubGlobal('fetch', fetchMock)
  function NavigationSummary() {
    useQuery({
      queryKey: QK.pipelineJobs, queryFn: async () => (await fetch('/api/pipeline/jobs?limit=1')).json(),
    })
    return null
  }
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const host = document.createElement('div')
  const root = createRoot(host)
  document.body.append(host)
  try {
    await act(async () => root.render(
      <QueryClientProvider client={client}><MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <NavigationSummary />
        <Data />
      </MemoryRouter></QueryClientProvider>,
    ))
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 40)) })
    const panel = host.querySelector('[aria-label="交易数据就绪"]')
    expect(panel).not.toBeNull()
    expect(panel?.textContent).toContain('覆盖未核实')
    expect(panel?.textContent).toContain('待核 5,224 只')
    expect(panel?.textContent).toContain('15:35 补缺失败')
    expect(panel?.textContent).toContain('日线已定版')
    expect(panel?.textContent).toContain('15:45 复核完成')
    const link = [...panel!.querySelectorAll('a')].find(a => a.textContent?.includes('查看相关策略任务'))
    expect(link?.getAttribute('href')).toBe('/monitor?task_context=stock-status&data_date=2026-11-30#strategy-tasks')
    expect(fetchMock).toHaveBeenCalledWith('/api/pipeline/jobs?limit=1')
    expect(fetchMock).toHaveBeenCalledWith('/api/pipeline/jobs?limit=15&include_readiness=true', expect.anything())
  } finally {
    await act(async () => root.unmount())
    host.remove()
    client.clear()
  }
})

it.each(['success', 'failure', 'preferences-error'] as const)('places quotes in the portrait and handles the %s auto-sync switch', async outcome => {
  let auto = true
  let finishUpdate: (value: Response) => void = () => {}
  let preferencesReads = 0
  let statusReads = 0
  const fetchMock = vi.fn(async (path: string, init?: RequestInit) => {
    let value: unknown = {}
    if (path === '/api/settings/preferences/data-sync-auto') {
      return new Promise<Response>(resolve => {
        finishUpdate = response => {
          if (response.ok) auto = JSON.parse(String(init?.body)).enabled
          resolve(response)
        }
      })
    }
    if (path === '/api/settings/preferences') {
      preferencesReads++
      if (outcome === 'preferences-error') return new Response(JSON.stringify({ detail: '配置不可用' }), { status: 500 })
      value = { data_sync_auto_enabled: auto, realtime_quotes_enabled: true }
    } else if (path === '/api/settings/data-sources') value = { builtin: [], plugins: [], custom: [] }
    else if (path === '/api/settings/capability-matrix') value = { capabilities: [] }
    else if (path === '/api/settings/kline-sync') value = Object.fromEntries(
      ['1m', '5m', '10m', '15m', '1d', '1w'].map(period => [period, {
        enabled: true, interval_seconds: 60, windows: [], time: '16:00',
      }]),
    )
    else if (path === '/api/ext-data') value = { items: [] }
    else if (path === '/api/regime/coverage') value = null
    else if (path === '/api/data/status') { statusReads++; value = { storage: {}, indicators_ready: true } }
    else if (path.startsWith('/api/pipeline/jobs?')) value = { active_id: null, jobs: [] }
    return new Response(JSON.stringify(value))
  })
  vi.stubGlobal('fetch', fetchMock)
  storage.dataCardOrder.set(['instruments', 'minute', '5m', '10m', '15m', 'daily', 'weekly'])
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const host = document.createElement('div')
  document.body.append(host)
  const root = createRoot(host)
  const flush = () => act(async () => { await new Promise(resolve => setTimeout(resolve, 30)) })
  try {
    await act(async () => root.render(
      <QueryClientProvider client={client}>
        <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}><Data /><ToastContainer /></MemoryRouter>
      </QueryClientProvider>,
    ))
    await flush()
    const sync = host.querySelector('[aria-label="数据同步"]')
    expect(sync).not.toBeNull()
    expect(host.textContent).not.toContain('数据修正')
    const toggle = sync!.querySelector<HTMLButtonElement>('[role="switch"][aria-label="自动同步"]')!
    expect(toggle).not.toBeNull()
    const portraitTitle = Array.from(host.querySelectorAll('h2')).find(h => h.textContent === '数据画像')!
    const portrait = portraitTitle.parentElement!
    const titles = Array.from(portrait.querySelectorAll('h3'), h => h.textContent)
    expect(titles[titles.indexOf('15分K') + 1]).toBe('实时行情')
    expect(sync!.querySelector('h3')?.textContent).toBe('数据同步')
    expect(toggle.getAttribute('aria-checked')).toBe('true')
    expect(toggle.disabled).toBe(outcome === 'preferences-error')
    if (outcome === 'preferences-error') return
    const oldReads = { preferencesReads, statusReads }
    await act(async () => toggle.click())
    await flush()
    expect(toggle.disabled).toBe(true)
    expect(fetchMock).toHaveBeenCalledWith('/api/settings/preferences/data-sync-auto', expect.objectContaining({
      method: 'PUT', body: '{"enabled":false}',
    }))
    await act(async () => finishUpdate(new Response(
      JSON.stringify(outcome === 'failure' ? { detail: '保存失败' } : { data_sync_auto_enabled: false }),
      { status: outcome === 'failure' ? 500 : 200 },
    )))
    await flush()
    await flush()
    expect(toggle.disabled).toBe(false)
    expect(toggle.getAttribute('aria-checked')).toBe(outcome === 'success' ? 'false' : 'true')
    if (outcome === 'success') {
      expect(preferencesReads).toBeGreaterThan(oldReads.preferencesReads)
      expect(statusReads).toBeGreaterThan(oldReads.statusReads)
      expect(portrait.textContent).toContain('实时行情')
    } else {
      expect([...host.querySelectorAll('[role="status"]')].some(node =>
        node.textContent?.includes('保存失败'),
      )).toBe(true)
    }
  } finally {
    await act(async () => root.unmount())
    host.remove()
    client.clear()
  }
})

it.each(['succeeded', 'failed'] as const)('refreshes cached charts after a %s repair, including partial writes', async (status) => {
  vi.stubGlobal('fetch', vi.fn(async (path: string) => {
    let value: unknown = {}
    if (path === '/api/settings/data-sources') value = { builtin: [], plugins: [], custom: [] }
    else if (path === '/api/settings/capability-matrix') value = { capabilities: [] }
    else if (path === '/api/settings/kline-sync') value = Object.fromEntries(
      ['1m', '5m', '10m', '15m', '1d', '1w'].map(period => [period, {
        enabled: true, interval_seconds: 60, windows: [['09:15', '11:45'], ['13:00', '16:00']], time: '16:00',
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

it.each([
  ['1d', '日 K', '日K', '获取全量数据'],
  ['5m', '5分K', '5分K', '生成数据'],
  ['10m', '10分K', '10分K', '生成数据'],
  ['15m', '15分K', '15分K', '生成数据'],
  ['1w', '周K', '周K', '生成数据'],
] as const)('opens %s profile settings and submits its date range', async (period, cardTitle, dialogTitle, action) => {
  const fetchMock = vi.fn(async (path: string, _init?: RequestInit) => {
    let value: unknown = {}
    if (path === '/api/settings/preferences') value = { data_sync_auto_enabled: true }
    else if (path === '/api/settings/data-sources') value = { builtin: [], plugins: [], custom: [] }
    else if (path === '/api/settings/capability-matrix') value = {
      capabilities: [{ id: 'daily', usable: true }],
    }
    else if (path === '/api/settings/kline-sync') value = Object.fromEntries(
      ['1m', '5m', '10m', '15m', '1d', '1w'].map(period => [period, {
        enabled: true, interval_seconds: 60, windows: [], time: '16:30',
      }]),
    )
    else if (path === '/api/ext-data') value = { items: [] }
    else if (path === '/api/regime/coverage') value = null
    else if (path === '/api/data/status') value = {
      storage: {}, indicators_ready: true,
      daily: { rows: 4, earliest_date: '2025-10-27', latest_date: '2026-10-27', symbols_covered: 4, trading_days: 1 },
    }
    else if (path.startsWith('/api/pipeline/jobs?')) value = { active_id: null, jobs: [] }
    else if (path === '/api/kline/sync_period') value = { status: 'started', job_id: 'daily-history-1' }
    else if (path === '/api/pipeline/jobs/daily-history-1') value = {
      id: 'daily-history-1', kind: 'extend_history', status: 'running', stage: 'extend_history',
      progress: 10, stage_pct: 0, log: [], started_at: null, finished_at: null, duration_s: null, result: null, error: null,
    }
    return new Response(JSON.stringify(value))
  })
  vi.stubGlobal('fetch', fetchMock)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const host = document.createElement('div')
  document.body.append(host)
  const root = createRoot(host)
  const flush = () => act(async () => { await new Promise(resolve => setTimeout(resolve, 30)) })
  try {
    await act(async () => root.render(
      <QueryClientProvider client={client}><MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}><Data /></MemoryRouter></QueryClientProvider>,
    ))
    await flush()
    const card = [...host.querySelectorAll('h3')].find(heading => heading.textContent === cardTitle)!.closest('.rounded-card')!
    const settings = card.querySelector<HTMLButtonElement>('button[title="设置"]')!
    expect(settings).not.toBeNull()
    await act(async () => settings.click())
    await flush()
    const dialog = document.querySelector('[role="dialog"]')!
    expect(dialog.textContent).toContain(`${dialogTitle} · 同步设置`)
    if (period === '1d') expect(dialog.textContent).toContain('获取日K后继续同步除权因子并重算指标')
    expect(dialog.querySelector('[aria-label="历史扩展数量"]')).toBeNull()
    expect(dialog.querySelector('[aria-label="起始日期"]')).not.toBeNull()
    expect(dialog.querySelector('[aria-label="结束日期"]')).not.toBeNull()
    if (period === '1d') {
      expect(dialog.querySelector('[aria-label="交易日同步时间"]')?.textContent).toContain('15:35（固定）')
      expect(dialog.textContent).toContain('2025-10-27')
    }
    const acquire = [...dialog.querySelectorAll('button')].find(button => button.textContent === action)!
    await act(async () => acquire.click())
    await flush()
    const request = fetchMock.mock.calls.find(call => call[0] === '/api/kline/sync_period')!
    expect(request[1]).toEqual(expect.objectContaining({ method: 'POST' }))
    expect(JSON.parse(String(request[1]?.body))).toEqual({
      period,
      start_date: expect.any(String),
      end_date: expect.any(String),
      mode: 'full',
    })
    expect(host.textContent).toContain('daily-history-1')
  } finally {
    await act(async () => root.unmount())
    host.remove()
    client.clear()
  }
})
