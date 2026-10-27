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

async function render(period: '5m' | '10m' | '15m' | '1m' | '1w' | '1d', onStart = vi.fn(), hasCap = true, isRunning = false, earliestDate: string | null = null) {
  host = document.createElement('div')
  document.body.appendChild(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await act(async () => {
    root!.render(<QueryClientProvider client={client}><KlineSyncConfig period={period} hasCap={hasCap} isRunning={isRunning} earliestDate={earliestDate} onJobStart={onStart} /></QueryClientProvider>)
  })
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 30)) })
  return onStart
}

it('shows the unified intraday collection window and submits explicit sync settings', async () => {
  await render('5m')
  const times = [...host.querySelectorAll('button[aria-label$="同步时间"]')]
  expect(times).toHaveLength(0)
  expect(host.textContent).toContain('09:15–11:45 / 13:00–16:00')
  expect(host.querySelector<HTMLInputElement>('input[type="number"]')?.value).toBe('300')
  expect(mocks.save).not.toHaveBeenCalled()
  const button = [...host.querySelectorAll('button')].find(node => node.textContent === '保存周期设置')!
  await act(async () => button.click())
  expect(mocks.save.mock.calls[0][0]).toBe('5m')
})

it.each(['5m', '10m', '15m', '1w'] as const)('generates local %s data without capability or history controls', async period => {
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

it.each(['1m', '1d'] as const)('keeps %s history controls and provider capability gating', async period => {
  await render(period, vi.fn(), false)
  expect(host.querySelector<HTMLInputElement>('input[aria-label="历史扩展数量"]')?.value).toBe('6')
  expect(host.querySelector<HTMLSelectElement>('select[aria-label="历史扩展单位"]')?.value).toBe('month')
  expect([...host.querySelectorAll('button')].find(node => node.textContent === '获取数据')!.disabled).toBe(true)
})

it.each(['1m', '1d'] as const)('submits an explicit expansion range for %s history', async period => {
  const onStart = await render(period)
  await act(async () => [...host.querySelectorAll('button')].find(node => node.textContent === '获取数据')!.click())
  expect(mocks.sync).toHaveBeenCalledWith(period, 6, 'month')
  expect(onStart).toHaveBeenCalledWith('offline-1')
})

it.each(['5m', '1w'] as const)('disables %s generation while another data task runs', async period => {
  await render(period, vi.fn(), false, true)
  const button = [...host.querySelectorAll('button')].find(node => node.textContent === '生成数据')!
  expect(button.disabled).toBe(true)
})

it('uses a Saturday completion time and local daily history for weekly synchronization', async () => {
  await render('1w', vi.fn(), false)
  expect(host.querySelector('[aria-label="周六同步时间"]')?.textContent).toContain('16:00')
  expect(host.textContent).not.toContain('周期间隔（秒）')
  expect(host.textContent).toContain('每周六')
  expect(host.textContent).toContain('本地日K')
  expect([...host.querySelectorAll('button')].find(node => node.textContent === '生成数据')!.disabled).toBe(false)
})

it('saves a selected daily synchronization time in 24-hour format', async () => {
  await render('1d')
  await act(async () => host.querySelector<HTMLButtonElement>('[aria-label="交易日同步时间"]')!.click())
  await act(async () => document.querySelector<HTMLButtonElement>('[aria-label="18 时"]')!.click())
  await act(async () => document.querySelector<HTMLButtonElement>('[aria-label="45 分"]')!.click())
  await act(async () => [...document.querySelectorAll('button')].find(button => button.textContent === '确定')!.click())
  await act(async () => [...host.querySelectorAll('button')].find(button => button.textContent === '保存周期设置')!.click())
  expect(mocks.save).toHaveBeenCalledWith('1d', expect.objectContaining({ time: '18:45' }))
})

it('restores daily provider acquisition and subsequent adjustment and indicators', async () => {
  await render('1d')
  expect(host.textContent).toContain('获取日K后继续同步除权因子并重算指标')
  expect(host.textContent).not.toContain('不请求上游')
  expect(host.textContent).not.toContain('生成数据')
  expect(host.textContent).toContain('交易日同步时间')
})

it('uses the selected year range and keeps the existing earliest daily date visible', async () => {
  await render('1d', vi.fn(), true, false, '2025-04-01')
  const unit = host.querySelector<HTMLSelectElement>('select[aria-label="历史扩展单位"]')!
  const quantity = host.querySelector<HTMLInputElement>('input[aria-label="历史扩展数量"]')!
  expect(unit).not.toBeNull()
  expect(quantity).not.toBeNull()
  await act(async () => {
    unit.value = 'year'
    unit.dispatchEvent(new Event('change', { bubbles: true }))
  })
  expect(quantity.value).toBe('1')
  expect(quantity.max).toBe('10')
  expect(host.textContent).toContain('2025-04-01')
  await act(async () => [...host.querySelectorAll('button')].find(node => node.textContent === '获取数据')!.click())
  expect(mocks.sync).toHaveBeenCalledWith('1d', 1, 'year')
})

it('disables daily acquisition while another data task runs', async () => {
  await render('1d', vi.fn(), true, true)
  const button = [...host.querySelectorAll('button')].find(node => node.textContent === '获取数据')!
  expect(button).toBeDefined()
  expect(button.disabled).toBe(true)
})

it('keeps the daily form and displays an acquisition failure', async () => {
  const onStart = await render('1d')
  mocks.sync.mockRejectedValueOnce(new Error('日K获取失败'))
  await act(async () => [...host.querySelectorAll('button')].find(node => node.textContent === '获取数据')!.click())
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 30)) })
  expect(host.querySelector('[role="alert"]')?.textContent).toBe('日K获取失败')
  expect(host.querySelector('[aria-label="历史扩展数量"]')).not.toBeNull()
  expect(onStart).not.toHaveBeenCalled()
})
