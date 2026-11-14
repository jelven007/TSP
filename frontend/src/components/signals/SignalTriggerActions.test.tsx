// @vitest-environment jsdom
import { act, useState } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, it, vi } from 'vitest'
import { SignalTriggerActions } from './SignalTriggerActions'
import { api, type CustomSignal } from '@/lib/api'
import { QK } from '@/lib/queryKeys'

vi.mock('./CustomSignalDialog', () => ({ CustomSignalDialog: () => null }))
vi.mock('@/pages/backtest/AddFactorSignalDialog', () => ({ AddFactorSignalDialog: () => null }))

Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
let root: Root
let host: HTMLDivElement
let client: QueryClient
const changed = vi.fn()
const signals: CustomSignal[] = [
  { id: 'entry_a', name: '已有买点', kind: 'entry', enabled: true, conditions: [] },
  { id: 'exit_a', name: '已有卖点', kind: 'exit', enabled: true, conditions: [] },
  { id: 'common', name: '通用条件', kind: 'both', enabled: true, conditions: [] },
  { id: 'off', name: '已停用信号', kind: 'entry', enabled: false, conditions: [] },
  { id: 'late_session_strength', name: '尾盘走强', kind: 'entry', enabled: true, conditions: [],
    timeframe: 'intraday', template: 'late_session_strength' },
  { id: 'morning_liquidation', name: '早盘清仓', kind: 'exit', enabled: true, conditions: [],
    timeframe: 'intraday', template: 'morning_liquidation' },
  { id: 'late_s01', name: '尾盘建仓_s01', kind: 'entry', enabled: true, conditions: [],
    timeframe: 'intraday', template: 's01_late_entry',
    params: { current_amount_min: 1e8, current_return_min: .02, current_return_max: .03,
      current_range_max: .0525, market_min_valid: 1000, market_positive_min: .65 } },
]
afterEach(async () => {
  await act(async () => root?.unmount())
  host?.remove()
  client?.clear()
  vi.restoreAllMocks()
  changed.mockClear()
})

async function render(kind: 'entry' | 'exit', allowIntraday = false, initial = ['signal_ma_golden_cross'], s01Template?: string) {
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  vi.spyOn(api, 'customSignalsList').mockResolvedValue({ signals })
  await client.fetchQuery({ queryKey: QK.customSignals, queryFn: api.customSignalsList })
  function Harness() {
    const [value, setValue] = useState(initial)
    return <SignalTriggerActions selectExisting kind={kind} signals={value} allowIntraday={allowIntraday} {...({ s01Template } as any)}
      onChange={next => { changed(next); setValue(next) }} />
  }
  await act(async () => root.render(<QueryClientProvider client={client}>
    <MemoryRouter><Harness /></MemoryRouter>
  </QueryClientProvider>))
}
async function click(label: string) {
  const button = document.querySelector<HTMLButtonElement>(`button[aria-label="${label}"]`)
    ?? [...document.querySelectorAll('button')].find(b => b.textContent?.trim() === label)
  expect(button).toBeTruthy()
  await act(async () => button!.click())
}
const checkbox = (name: string) => document.querySelector<HTMLInputElement>(`input[aria-label="${name}"]`)!

it('selects existing entry and common signals without losing builtins and keeps changes staged', async () => {
  await render('entry')
  await click('选择已有入场信号')
  expect(document.querySelector('[role="dialog"]')?.textContent).toContain('信号页')
  expect(checkbox('已有买点')).toBeTruthy()
  expect(checkbox('通用条件')).toBeTruthy()
  expect(checkbox('已有卖点')).toBeNull()
  expect(checkbox('尾盘走强').disabled).toBe(true)
  expect(checkbox('已停用信号').disabled).toBe(true)
  expect(document.querySelector('[role="dialog"]')?.textContent).toContain('通用分钟策略')
  await act(async () => { checkbox('已有买点').click(); checkbox('通用条件').click() })
  expect(changed).not.toHaveBeenCalled()
  await click('应用选择')
  expect(changed).toHaveBeenLastCalledWith(['signal_ma_golden_cross', 'csg_entry_a', 'csg_common'])
  await click('选择已有入场信号')
  expect(checkbox('已有买点').checked).toBe(true)
  await act(async () => checkbox('已有买点').click())
  await click('取消')
  expect(changed).toHaveBeenCalledTimes(1)
})

it('selects the existing minute exit template and supports searching by id', async () => {
  await render('exit', true, [])
  await click('选择已有出场信号')
  expect(checkbox('已有卖点')).toBeTruthy()
  expect(checkbox('已有买点')).toBeNull()
  const input = document.querySelector<HTMLInputElement>('input[aria-label="搜索已有信号"]')!
  expect(input).toBeTruthy()
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, 'morning')
    input.dispatchEvent(new Event('input', { bubbles: true }))
  })
  expect(checkbox('已有卖点')).toBeNull()
  expect(checkbox('早盘清仓').disabled).toBe(false)
  await act(async () => checkbox('早盘清仓').click())
  await click('应用选择')
  expect(changed).toHaveBeenLastCalledWith(['csgi_morning_liquidation'])
})

it('allows removing a previously selected disabled signal without re-enabling it', async () => {
  await render('entry', false, ['csg_off'])
  await click('选择已有入场信号')
  expect(checkbox('已停用信号').checked).toBe(true)
  expect(checkbox('已停用信号').disabled).toBe(false)
  await act(async () => checkbox('已停用信号').click())
  expect(checkbox('已停用信号').disabled).toBe(true)
  await click('应用选择')
  expect(changed).toHaveBeenLastCalledWith([])
})

it('selects the S01 template through the existing dialog and keeps apply/cancel staged', async () => {
  await render('entry', true, ['csgi_late_s01'], 's01_late_entry')
  await click('选择已有入场信号')
  expect(checkbox('尾盘建仓_s01').checked).toBe(true)
  expect(checkbox('尾盘建仓_s01').disabled).toBe(false)
  expect(checkbox('尾盘走强').disabled).toBe(false)
  expect(checkbox('已有买点').disabled).toBe(false)
  expect(document.querySelector('[role="dialog"]')?.textContent).toContain('可多选')
  expect(document.querySelector('[role="dialog"]')?.textContent).toContain('14:45')
  await act(async () => checkbox('尾盘建仓_s01').click())
  await click('取消')
  expect(changed).not.toHaveBeenCalled()
  await click('选择已有入场信号')
  expect(checkbox('尾盘建仓_s01').checked).toBe(true)
  await click('应用选择')
  expect(changed).toHaveBeenLastCalledWith(['csgi_late_s01'])
  await click('选择已有入场信号')
  await act(async () => checkbox('已有买点').click())
  await click('应用选择')
  expect(changed).toHaveBeenLastCalledWith(['csgi_late_s01', 'csg_entry_a'])
  expect(host.querySelector('button[title="新增自定义信号"]')).toBeTruthy()
  expect(host.querySelector('button[title="从因子快速创建条件（阈值给建议值）"]')).toBeTruthy()
})
