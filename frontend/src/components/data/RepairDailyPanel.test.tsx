// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, it, vi } from 'vitest'
import { RepairDailyPanel } from './RepairDailyPanel'

const mocks = vi.hoisted(() => ({ repair: vi.fn(async () => ({ job_id: 'repair-1', status: 'started' })) }))
vi.mock('@/lib/api', () => ({ api: { repairDaily: mocks.repair } }))
vi.mock('@/components/DatePicker', () => ({
  DatePicker: ({ value }: { value: string }) => <span data-date={value}>{value}</span>,
}))
vi.mock('@/lib/capability-labels', () => ({
  MissingCapChip: ({ capKey }: { capKey: string }) => <span>{capKey}</span>,
}))
Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
let root: Root
let host: HTMLDivElement
let client: QueryClient
afterEach(async () => {
  await act(async () => root?.unmount())
  host?.remove()
  client?.clear()
  mocks.repair.mockReset()
  mocks.repair.mockResolvedValue({ job_id: 'repair-1', status: 'started' })
})

async function render(hasCap = true, hasMinuteCap = true, isRunning = false) {
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient()
  const onStart = vi.fn()
  await act(async () => root.render(
    <QueryClientProvider client={client}>
      <RepairDailyPanel hasCap={hasCap} hasMinuteCap={hasMinuteCap} isRunning={isRunning}
        latestDates={{ '1d': '2026-10-23', '1m': '2026-10-22', '10m': null }} onStart={onStart} />
    </QueryClientProvider>,
  ))
  return onStart
}

async function selectPeriod(period: string) {
  const select = host.querySelector<HTMLSelectElement>('select[aria-label="K线周期"]')!
  expect(select).not.toBeNull()
  await act(async () => {
    select.value = period
    select.dispatchEvent(new Event('change', { bubbles: true }))
  })
}

it('defaults to daily and exposes exactly the six requested periods', async () => {
  const onStart = await render()
  const select = host.querySelector<HTMLSelectElement>('select[aria-label="K线周期"]')!
  expect(select).not.toBeNull()
  expect(select.value).toBe('1d')
  expect([...select.options].map(option => option.text)).toEqual(['1分K', '5分K', '10分K', '15分K', '日K', '周K'])
  await act(async () => host.querySelector<HTMLButtonElement>('button')!.click())
  expect(mocks.repair).toHaveBeenCalledWith(expect.any(String), '1d')
  expect(onStart).toHaveBeenCalledWith('repair-1')
})

it('sends the selected period and shows that period’s own latest date', async () => {
  await render()
  await selectPeriod('1m')
  expect(host.textContent).toContain('2026-10-22')
  expect(host.textContent).not.toContain('2026-10-23')
  await act(async () => host.querySelector<HTMLButtonElement>('button')!.click())
  expect(mocks.repair).toHaveBeenCalledWith(expect.any(String), '1m')
})

it('gates remote periods by their route while allowing local ten minute repair', async () => {
  await render(false, false)
  const button = () => host.querySelector<HTMLButtonElement>('button')!
  expect(button().disabled).toBe(true)
  await selectPeriod('10m')
  expect(button().disabled).toBe(false)
  expect(host.textContent).toContain('本地1分K')
  await selectPeriod('5m')
  expect(button().disabled).toBe(true)
  expect(host.textContent).toContain('kline.minute.batch')
})

it('keeps the panel open and displays a rejected request', async () => {
  const onStart = await render()
  mocks.repair.mockRejectedValueOnce(new Error('当前K线数据源不可用'))
  await act(async () => host.querySelector<HTMLButtonElement>('button')!.click())
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)) })
  expect(host.querySelector('[role="alert"]')?.textContent).toContain('当前K线数据源不可用')
  expect(onStart).not.toHaveBeenCalled()
})

it('prevents submission while another data task runs', async () => {
  await render(true, true, true)
  expect(host.querySelector<HTMLButtonElement>('button')!.disabled).toBe(true)
})
