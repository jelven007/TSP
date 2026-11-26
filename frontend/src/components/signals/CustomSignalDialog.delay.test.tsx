// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, it, vi } from 'vitest'
import { CustomSignalDialog } from './CustomSignalDialog'
import { api, type CustomSignal, type LateSessionStrengthParams } from '@/lib/api'
import { LATE_SESSION_DEFAULTS, lateSessionSummary } from '@/lib/customSignals'

vi.mock('@/components/Modal', () => ({
  Modal: ({ children }: any) => <div role="dialog">{children}</div>,
  useModalPortalContainer: () => document.body,
}))
Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
let root: Root
let host: HTMLDivElement
afterEach(async () => {
  await act(async () => root?.unmount())
  host?.remove()
  vi.restoreAllMocks()
})

const signal = (delay: number): CustomSignal => ({
  id: 'late_session_strength', name: '尾盘建仓', kind: 'entry', timeframe: 'intraday',
  template: 'late_session_strength', enabled: true, conditions: [],
  params: { ...LATE_SESSION_DEFAULTS, evaluation_delay_minutes: delay },
})

async function fixture(initial?: CustomSignal) {
  vi.spyOn(api, 'customSignalsOptions').mockResolvedValue({ fields: [], operators: [], kinds: [] })
  vi.spyOn(api, 'strategyAiStatus').mockResolvedValue({ configured: false } as any)
  const save = vi.spyOn(api, 'customSignalSave').mockImplementation(async value => ({ ok: true, signal: value }))
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const render = async (value?: CustomSignal) => {
    await act(async () => root.render(<QueryClientProvider client={qc}><MemoryRouter>
      <CustomSignalDialog open defaultKind="entry" signal={value} onClose={() => {}} />
    </MemoryRouter></QueryClientProvider>))
  }
  await render(initial)
  if (!initial) {
    const template = host.querySelector<HTMLSelectElement>('select[aria-label="信号模板"]')!
    await act(async () => {
      template.value = 'late_session_strength'
      template.dispatchEvent(new Event('change', { bubbles: true }))
    })
  }
  const input = () => host.querySelector<HTMLInputElement>('input[aria-label="确认延迟（秒）"]')
  const setSeconds = async (seconds: string) => {
    expect(input()).not.toBeNull()
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input(), seconds)
      input()!.dispatchEvent(new Event('input', { bubbles: true }))
    })
  }
  const submit = async () => {
    await act(async () => [...host.querySelectorAll('button')].find(b => b.textContent?.includes('保存'))!.click())
  }
  return { save, input, setSeconds, submit, render }
}

it('creates a 30-second control and converts 900 seconds to persisted minutes', async () => {
  const f = await fixture()
  expect(f.input()).not.toBeNull()
  expect(f.input()!.value).toBe('30')
  expect([f.input()!.min, f.input()!.max, f.input()!.step]).toEqual(['30', '900', '30'])
  expect(host.textContent).toContain('14:30:30')
  await f.setSeconds('900')
  expect(host.textContent).toContain('启动后900秒确认')
  expect(host.textContent).toContain('14:45:00')
  expect(host.textContent).toContain('K线截止 14:30:00')
  await f.submit()
  expect(f.save.mock.calls[0][0].params).toEqual({ ...LATE_SESSION_DEFAULTS, evaluation_delay_minutes: 15 })
})

it.each([60, 90])('preserves the saved %s-second delay when editing other fields', async seconds => {
  const original = signal(seconds / 60)
  const f = await fixture(original)
  expect(f.input()?.value).toBe(String(seconds))
  expect(host.textContent).not.toContain('保存后改为')
  await f.submit()
  expect(f.save.mock.calls[0][0].params).toEqual(original.params)
})

it.each(['0', '45', '930', '30.5'])('rejects invalid delay %s before submitting', async value => {
  const f = await fixture(signal(1.5))
  await f.setSeconds(value)
  await f.submit()
  expect(f.save).not.toHaveBeenCalled()
  expect(host.textContent).toContain('30～900秒')
})

it('saves a cleared delay as null and preserves it on reopening', async () => {
  const f = await fixture(signal(1.5))
  await f.setSeconds('')
  await f.submit()
  expect((f.save.mock.calls[0][0].params as LateSessionStrengthParams).evaluation_delay_minutes).toBeNull()
  await f.render(f.save.mock.calls[0][0])
  expect(f.input()!.value).toBe('')
})

it.each(['-10', '0', '100', '-10.1', '100.1'])('validates and saves the daily change floor %s', async value => {
  const f = await fixture(signal(.5))
  const input = host.querySelector<HTMLInputElement>('[aria-label="当日涨幅下限（%）"]')!
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, value)
    input.dispatchEvent(new Event('input', { bubbles: true }))
  })
  await f.submit()
  if (Number(value) < -10 || Number(value) > 100) {
    expect(f.save).not.toHaveBeenCalled()
    expect(host.textContent).toContain('当日涨幅下限须为-10.0～100.0')
  } else {
    expect(f.save.mock.calls[0]?.[0].params).toMatchObject({ change_min_pct: Number(value) })
    await f.render(f.save.mock.calls[0][0])
    expect(host.querySelector<HTMLInputElement>('[aria-label="当日涨幅下限（%）"]')!.value).toBe(value)
  }
})

it.each(['tail_entry_s05', 'low_gap_entry_s05', 'board_entry_s05',
  'late_session_strength', 'late_session_strength_s01', 'late_session_strength_s03'])(
  'uses the same sixteen fields and drops obsolete configuration for %s', async id => {
  const original = signal(1)
  const f = await fixture({ ...original, id,
    params: { ...original.params as LateSessionStrengthParams, time: '14:40:00', end_time: '14:51:00',
      pool_up_min_pct: null, change_min_pct: null, change_max_pct: null, ma5_1m_count: null, ma5_5m_count: null,
      s05_entry: { prior_up_min_pct: 48, prior_above_ma20_min_pct: 45, change_max_pct: 7, sort_by: 'four_dimension' },
    } as LateSessionStrengthParams })
  expect(f.input()?.value).toBe('60')
  expect(host.querySelector('[aria-label="当日股票池上涨占比下限（%）"]')).not.toBeNull()
  expect(host.querySelector('[aria-label="S05分支入场条件"]')).toBeNull()
  expect(host.querySelector('[aria-label="买入排序"]')).toBeNull()
  expect(host.textContent).not.toContain('首名否决')
  const input = host.querySelector<HTMLInputElement>('[aria-label="最近5分钟涨幅下限（%）"]')!
  expect(input.value).toBe('')
  expect([input.min, input.max]).toEqual(['-10', '100'])
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, '-10')
    input.dispatchEvent(new Event('input', { bubbles: true }))
  })
  await f.submit()
  expect(f.save.mock.calls[0][0].params).toMatchObject({
    evaluation_delay_minutes: 1, return_5m_min_pct: -10,
  })
  expect(f.save.mock.calls[0][0].params).not.toHaveProperty('s05_entry')
  await f.render(f.save.mock.calls[0][0])
  expect(host.querySelector<HTMLInputElement>('[aria-label="最近5分钟涨幅下限（%）"]')!.value).toBe('-10')
})

it('renders sixteen fields in the specified order with three desktop columns', async () => {
  await fixture(signal(.5))
  const grid = host.querySelector('[aria-label="尾盘走强参数"]')!
  expect(grid).not.toBeNull()
  expect(grid.className).toContain('sm:grid-cols-3')
  expect([...grid.querySelectorAll('label > span')].map(node => node.textContent)).toEqual([
    '启动时间（北京时间）', '确认延迟（秒）', '当日股票池上涨占比下限（%）',
    '当日成交额下限（亿）', '当日换手率下限（%）', '当日振幅上限（%）',
    '当日涨幅下限（%）', '当日涨幅上限（%）', '当日量比下限',
    '1分钟MA5连增数量', '5分钟MA5连增数量', '结束时间（北京时间）',
    '最近5分钟涨幅下限（%）', '最近15分钟涨幅下限（%）',
    '日内位置下限（%）', '开盘缺口上限（%）',
  ])
  const numbers = [...grid.querySelectorAll<HTMLInputElement>('input[type="number"]')]
  expect(numbers.slice(1, 8).every(input =>
    input.min === (input.getAttribute('aria-label') === '当日涨幅下限（%）' ? '-10' : '0.1')
    && input.max === '100')).toBe(true)
  expect(numbers.slice(8, 10).every(input => input.min === '1' && input.max === '100')).toBe(true)
  expect(numbers.slice(10, 12).every(input => input.min === '-10' && input.max === '100')).toBe(true)
  expect(numbers.slice(12).every(input => input.min === '0.1' && input.max === '100')).toBe(true)
  expect(grid.querySelectorAll('label > p')).toHaveLength(16)
  expect(host.querySelector('[aria-label="尾盘走强分阶段计算说明"]')).toBeNull()
  expect(grid.textContent).toContain('启动+15秒')
  expect(grid.textContent).toContain('在确认前重试')
  expect(grid.textContent).toContain('重试至结束')
  expect(host.textContent).toContain('未达标则结束当天任务')
  expect(grid.textContent).toContain('启动分钟的当日累计成交额')
  expect(grid.textContent).toContain('N个MA5值比较N−1次')
})

it.each([
  ['最近5分钟涨幅下限（%）', 'return_5m_min_pct', '-10.01'],
  ['最近15分钟涨幅下限（%）', 'return_15m_min_pct', '100.01'],
  ['日内位置下限（%）', 'position_min_pct', '0'],
  ['开盘缺口上限（%）', 'gap_max_pct', '100.01'],
])('validates and persists null for %s', async (label, key, invalid) => {
  const f = await fixture(signal(1))
  const change = async (value: string) => act(async () => {
    const input = host.querySelector<HTMLInputElement>(`[aria-label="${label}"]`)!
    expect(input).not.toBeNull()
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, value)
    input.dispatchEvent(new Event('input', { bubbles: true }))
  })
  await change(invalid)
  await f.submit()
  expect(f.save).not.toHaveBeenCalled()
  await change('')
  await f.submit()
  expect(f.save.mock.calls[0][0].params).toHaveProperty(key, null)
  await f.render(f.save.mock.calls[0][0])
  expect(host.querySelector<HTMLInputElement>(`[aria-label="${label}"]`)!.value).toBe('')
})

it('rejects confirmation beyond close while accepting the exact close boundary', async () => {
  const f = await fixture({ ...signal(15), params: { ...signal(15).params, time: '14:45:01' } })
  await f.submit()
  expect(f.save).not.toHaveBeenCalled()
  expect(host.textContent).toContain('14:45:00')
  await f.render({ ...signal(15), params: { ...signal(15).params, time: '14:45:00' } })
  await f.submit()
  expect(f.save.mock.calls[0][0].params).toEqual({ ...LATE_SESSION_DEFAULTS, time: '14:45:00', evaluation_delay_minutes: 15 })
})

it('summarizes the stored delay without forcing a different value on save', () => {
  expect(LATE_SESSION_DEFAULTS.evaluation_delay_minutes).toBe(0.5)
  for (const seconds of [30, 60, 90, 120, 900]) {
    const text = lateSessionSummary(signal(seconds / 60)).join(' ')
    expect(text).toContain(`确认延迟 ${seconds}秒`)
    expect(text).not.toContain('编辑保存后改为')
  }
})
