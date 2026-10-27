// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, it, vi } from 'vitest'
import { KlineSyncConfig } from './KlineSyncConfig'

const mocks = vi.hoisted(() => ({
  save: vi.fn(async (_period: string, config: unknown) => config),
  sync: vi.fn(async () => ({ job_id: 'offline-1', status: 'started' })),
}))
vi.mock('@/lib/api', () => ({
  api: {
    klineSyncConfigs: async () => Object.fromEntries(
      ['1m', '5m', '10m', '15m', '1d', '1w'].map(period => [period, {
        enabled: true, interval_seconds: ({ '1m': 60, '5m': 300, '10m': 600, '15m': 900 } as Record<string, number>)[period] ?? 60,
        windows: [['09:15', '11:45'], ['13:00', '16:00']], time: '16:00',
      }]),
    ),
    saveKlineSyncConfig: mocks.save, syncKlinePeriod: mocks.sync,
  },
}))
Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
let root: Root | undefined
let host: HTMLDivElement
let client: QueryClient
afterEach(async () => {
  await act(async () => root?.unmount())
  host?.remove()
  client?.clear()
  vi.clearAllMocks()
})

async function render(period: '5m' | '10m' | '15m' | '1m' | '1w' | '1d', onStart = vi.fn(), hasCap = true, isRunning = false) {
  host = document.createElement('div')
  document.body.appendChild(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await act(async () => {
    root!.render(<QueryClientProvider client={client}><KlineSyncConfig period={period} hasCap={hasCap} isRunning={isRunning} earliestDate={null} onJobStart={onStart} /></QueryClientProvider>)
  })
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 30)) })
  return onStart
}

it('shows the unified intraday collection window and submits explicit sync settings', async () => {
  await render('5m')
  const times = [...host.querySelectorAll<HTMLInputElement>('input[type="time"]')]
  expect(times).toHaveLength(0)
  expect(host.textContent).toContain('09:15–11:45 / 13:00–16:00')
  expect(host.querySelector<HTMLInputElement>('input[type="number"]')?.value).toBe('300')
  expect(mocks.save).not.toHaveBeenCalled()
  const button = [...host.querySelectorAll('button')].find(node => node.textContent === '保存周期设置')!
  await act(async () => button.click())
  expect(mocks.save.mock.calls[0][0]).toBe('5m')
})

it.each(['5m', '10m', '15m', '1d', '1w'] as const)('generates local %s data without capability or history controls', async period => {
  const onStart = await render(period, vi.fn(), false)
  expect(host.textContent).toContain(period.endsWith('m') ? '本地1分K' : '本地日K')
  expect(host.textContent).not.toContain('向前扩展')
  expect(host.querySelector('input[aria-label="历史扩展数量"]')).toBeNull()
  expect(host.querySelector('select[aria-label="历史扩展单位"]')).toBeNull()
  const button = [...host.querySelectorAll('button')].find(node => node.textContent === '生成数据')!
  expect(button.disabled).toBe(false)
  await act(async () => button.click())
  expect(mocks.sync).toHaveBeenCalledWith(period)
  expect(onStart).toHaveBeenCalledWith('offline-1')
})

it('keeps one minute history controls and provider capability gating', async () => {
  await render('1m', vi.fn(), false)
  expect(host.querySelector<HTMLInputElement>('input[aria-label="历史扩展数量"]')?.value).toBe('6')
  expect(host.querySelector<HTMLSelectElement>('select[aria-label="历史扩展单位"]')?.value).toBe('month')
  expect([...host.querySelectorAll('button')].find(node => node.textContent === '获取数据')!.disabled).toBe(true)
})

it('still submits an explicit expansion range for one minute history', async () => {
  await render('1m')
  await act(async () => [...host.querySelectorAll('button')].find(node => node.textContent === '获取数据')!.click())
  expect(mocks.sync).toHaveBeenCalledWith('1m', 6, 'month')
})

it.each(['5m', '1d', '1w'] as const)('disables %s generation while another data task runs', async period => {
  await render(period, vi.fn(), false, true)
  const button = [...host.querySelectorAll('button')].find(node => node.textContent === '生成数据')!
  expect(button.disabled).toBe(true)
})

it('uses a Saturday completion time and local daily history for weekly synchronization', async () => {
  await render('1w', vi.fn(), false)
  expect(host.querySelector<HTMLInputElement>('input[type="time"]')?.value).toBe('16:00')
  expect(host.textContent).not.toContain('周期间隔（秒）')
  expect(host.textContent).toContain('每周六')
  expect(host.textContent).toContain('本地日K')
  expect([...host.querySelectorAll('button')].find(node => node.textContent === '生成数据')!.disabled).toBe(false)
})

it('explains daily local recomputation separately from automatic synchronization', async () => {
  await render('1d', vi.fn(), false)
  expect(host.textContent).toContain('本地除权因子')
  expect(host.textContent).toContain('Enriched')
  expect(host.textContent).toContain('交易日同步时间')
})
