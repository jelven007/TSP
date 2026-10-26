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

async function render(period: '5m' | '10m' | '15m' | '1m' | '1w' | '1d', onStart = vi.fn(), hasCap = true) {
  host = document.createElement('div')
  document.body.appendChild(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await act(async () => {
    root!.render(<QueryClientProvider client={client}><KlineSyncConfig period={period} hasCap={hasCap} isRunning={false} earliestDate={null} onJobStart={onStart} /></QueryClientProvider>)
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

it.each(['5m', '10m', '15m'] as const)('allows local %s history without provider capability and explains the dependency', async period => {
  const onStart = await render(period, vi.fn(), false)
  expect(host.textContent).toContain('由本地1分K计算')
  const button = [...host.querySelectorAll('button')].find(node => node.textContent === '获取数据')!
  expect(button.disabled).toBe(false)
  await act(async () => button.click())
  expect(mocks.sync).toHaveBeenCalledWith(period, 6, 'month')
  expect(onStart).toHaveBeenCalledWith('offline-1')
})

it('keeps remote one minute history gated by provider capability', async () => {
  await render('1m', vi.fn(), false)
  expect([...host.querySelectorAll('button')].find(node => node.textContent === '获取数据')!.disabled).toBe(true)
})

it('uses a Saturday completion time and local daily history for weekly synchronization', async () => {
  await render('1w', vi.fn(), false)
  expect(host.querySelector<HTMLInputElement>('input[type="time"]')?.value).toBe('16:00')
  expect(host.textContent).not.toContain('周期间隔（秒）')
  expect(host.textContent).toContain('每周六')
  expect(host.textContent).toContain('本地日K')
  expect([...host.querySelectorAll('button')].find(node => node.textContent === '获取数据')!.disabled).toBe(false)
})

it('uses the weekly-style history form and allows daily data from an empty table', async () => {
  const onStart = await render('1d')
  expect(host.querySelector<HTMLInputElement>('input[aria-label="历史扩展数量"]')?.value).toBe('6')
  expect(host.querySelector<HTMLSelectElement>('select[aria-label="历史扩展单位"]')?.value).toBe('month')
  const button = [...host.querySelectorAll('button')].find(node => node.textContent === '获取数据')!
  expect(button.disabled).toBe(false)
  await act(async () => button.click())
  expect(mocks.sync).toHaveBeenCalledWith('1d', 6, 'month')
  expect(onStart).toHaveBeenCalledWith('offline-1')
})
