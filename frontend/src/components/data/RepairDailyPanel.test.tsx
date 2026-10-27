// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, it, vi } from 'vitest'
import { RepairDailyPanel } from './RepairDailyPanel'

const mocks = vi.hoisted(() => ({ repair: vi.fn(async () => ({ job_id: 'repair-1', status: 'started' })) }))
vi.mock('@/lib/api', () => ({ api: { repairDaily: mocks.repair } }))
vi.mock('@/components/DatePicker', () => ({
  DatePicker: ({ value, onChange, clearable: _clearable, align: _align, ...props }: {
    value: string; onChange: (value: string) => void; clearable?: boolean; align?: string
  }) => <input {...props} value={value} onChange={e => onChange(e.target.value)} />,
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
  vi.useRealTimers()
})

async function render(hasCap = true, hasMinuteCap = true, isRunning = false) {
  vi.setSystemTime(new Date(2026, 9, 27, 12))
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

async function enterDate(label: string, value: string) {
  const input = host.querySelector<HTMLInputElement>(`input[aria-label="${label}"]`)!
  expect(input).not.toBeNull()
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, value)
    input.dispatchEvent(new Event('input', { bubbles: true }))
  })
}

it('defaults to daily and exposes only minute and daily repairs', async () => {
  const onStart = await render()
  const select = host.querySelector<HTMLSelectElement>('select[aria-label="K线周期"]')!
  expect(select).not.toBeNull()
  expect(select.value).toBe('1d')
  expect([...select.options].map(option => [option.value, option.text])).toEqual([['1m', '1分K'], ['1d', '日K']])
  await act(async () => host.querySelector<HTMLButtonElement>('button')!.click())
  expect(mocks.repair).toHaveBeenCalledWith(expect.any(String), '1d', 'full', '2026-10-27')
  expect(onStart).toHaveBeenCalledWith('repair-1')
})

it('explains repair scope, modes, sources, and daily downstream work', async () => {
  await render()
  expect(host.textContent).toContain('修正说明')
  expect(host.textContent).toContain('从起始日期检查到结束日期，包含结束当天')
  expect(host.textContent).toContain('重新获取选定区间，并覆盖同一记录的旧值')
  expect(host.textContent).toContain('只写入缺失的证券和时间记录，已有数据保持不变')
  expect(host.textContent).toContain('1分K、日K通过当前数据源接口获取')
  expect(host.textContent).not.toContain('由本地1分K聚合')
  expect(host.textContent).toContain('日K修正后会重算受影响证券的复权与技术指标')
})

it('sends the selected period and shows that period’s own latest date', async () => {
  await render()
  await selectPeriod('1m')
  expect(host.textContent).toContain('2026-10-22')
  expect(host.textContent).not.toContain('2026-10-23')
  await act(async () => host.querySelector<HTMLButtonElement>('button')!.click())
  expect(mocks.repair).toHaveBeenCalledWith(expect.any(String), '1m', 'full', '2026-10-27')
})

it('gates both repairs by their corresponding source capability', async () => {
  await render(false, false)
  const button = () => host.querySelector<HTMLButtonElement>('button')!
  expect(button().disabled).toBe(true)
  expect(host.textContent).toContain('kline.daily.batch')
  await selectPeriod('1m')
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
  expect([...host.querySelectorAll<HTMLButtonElement>('button')].every(button => button.disabled)).toBe(true)
})

it('offers full and missing acquisition and sends missing mode with the selected period', async () => {
  await render()
  await selectPeriod('1m')
  const buttons = [...host.querySelectorAll<HTMLButtonElement>('button')]
  expect(buttons.map(button => button.textContent)).toEqual(['获取全量数据', '获取遗漏数据'])
  await act(async () => buttons[1].click())
  expect(mocks.repair).toHaveBeenCalledWith(expect.any(String), '1m', 'missing', '2026-10-27')
})

it.each(['full', 'missing'] as const)('accepts a selected end date for %s acquisition', async mode => {
  await render()
  const end = host.querySelector<HTMLInputElement>('input[aria-label="结束日期"]')!
  expect(end).not.toBeNull()
  expect(end.value).toBe('2026-10-27')
  await enterDate('起始日期', '2026-10-22')
  await enterDate('结束日期', '2026-10-23')
  expect(host.textContent).toContain('2026-10-22 → 2026-10-23')
  await act(async () => host.querySelectorAll<HTMLButtonElement>('button')[mode === 'full' ? 0 : 1].click())
  expect(mocks.repair).toHaveBeenCalledWith('2026-10-22', '1d', mode, '2026-10-23')
})

it.each(['', '2026-10-21', '2026-10-28'])('blocks an empty, reversed or future end date: %s', async end => {
  await render()
  await enterDate('起始日期', '2026-10-22')
  await enterDate('结束日期', end)
  expect([...host.querySelectorAll<HTMLButtonElement>('button')].every(button => button.disabled)).toBe(true)
  expect(host.querySelector('[role="alert"]')).not.toBeNull()
  expect(mocks.repair).not.toHaveBeenCalled()
})

it('allows a same-day range and blocks an empty start date', async () => {
  await render()
  await enterDate('起始日期', '2026-10-23')
  await enterDate('结束日期', '2026-10-23')
  expect(host.querySelector<HTMLButtonElement>('button')!.disabled).toBe(false)
  await enterDate('起始日期', '')
  expect(host.querySelector<HTMLButtonElement>('button')!.disabled).toBe(true)
})

it('disables both actions while submitting and re-enables them after failure', async () => {
  vi.useFakeTimers()
  await render()
  let reject!: (reason: Error) => void
  mocks.repair.mockImplementationOnce(() => new Promise((_, rej) => { reject = rej }))
  await act(async () => host.querySelectorAll<HTMLButtonElement>('button')[1].click())
  await act(async () => { await vi.runOnlyPendingTimersAsync() })
  expect([...host.querySelectorAll<HTMLButtonElement>('button')].every(button => button.disabled)).toBe(true)
  expect(host.querySelector<HTMLInputElement>('input[aria-label="结束日期"]')!.disabled).toBe(true)
  await act(async () => reject(new Error('请求失败')))
  await act(async () => { await vi.runOnlyPendingTimersAsync() })
  expect([...host.querySelectorAll<HTMLButtonElement>('button')].every(button => !button.disabled)).toBe(true)
})
