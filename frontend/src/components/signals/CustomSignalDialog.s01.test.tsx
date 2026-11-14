// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { expect, it, vi } from 'vitest'
import { CustomSignalDialog } from './CustomSignalDialog'
import { api } from '@/lib/api'
import { SignalPicker } from '@/components/screener/SignalPicker'
import { QK } from '@/lib/queryKeys'

vi.mock('@/components/Modal', () => ({
  Modal: ({ children }: any) => <div>{children}</div>,
  useModalPortalContainer: () => document.body,
}))

it('edits S01 percentages in percent units and saves original parameter keys', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.spyOn(api, 'customSignalsOptions').mockResolvedValue({ fields: [], operators: [], kinds: [] })
  vi.spyOn(api, 'strategyAiStatus').mockResolvedValue({ configured: false } as any)
  const save = vi.spyOn(api, 'customSignalSave').mockImplementation(async signal => ({ ok: true, signal }))
  const host = document.createElement('div')
  const root = createRoot(host)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const signal: any = {
    id: 'late_session_strength_s01', name: '尾盘建仓_s01', kind: 'entry',
    timeframe: 'intraday', template: 's01_late_entry', enabled: true, conditions: [],
    params: { current_amount_min: 1e8, current_return_min: .02, current_return_max: .03,
      current_range_max: .0525, market_min_valid: 1000, market_positive_min: .65 },
  }
  try {
    await act(async () => root.render(<QueryClientProvider client={qc}><MemoryRouter>
      <CustomSignalDialog open signal={signal} onClose={() => {}} />
    </MemoryRouter></QueryClientProvider>))
    const input = host.querySelector<HTMLInputElement>('input[aria-label="涨幅上限（%）"]')
    expect(input).not.toBeNull()
    expect(input!.value).toBe('3')
    expect(host.textContent).toContain('14:45')
    expect(host.querySelector('option[value="s01_late_entry"]')!.textContent).toBe('尾盘走强_S01')
    const labels = [...host.querySelectorAll('label')].map(label => label.querySelector('span')?.textContent)
    const start = labels.indexOf('启动时间（北京时间）')
    expect(labels.slice(start, start + 8)).toEqual([
      '启动时间（北京时间）', '确认延时（秒）', '当日市场有效证券下限（只）',
      '当日市场上涨比例下限（%）', '累计成交额下限（亿元）', '振幅上限（%）',
      '涨幅下限（%）', '涨幅上限（%）',
    ])
    expect(host.querySelector<HTMLInputElement>('input[aria-label="确认延时（秒）"]')!.value).toBe('60')
    expect([...['min', 'max', 'step']].map(key =>
      host.querySelector<HTMLInputElement>('input[aria-label="确认延时（秒）"]')!.getAttribute(key)))
      .toEqual(['30', '900', '30'])
    expect(host.textContent).toContain('14:46:00')
    expect(host.textContent).toContain('成交额有效且非负，允许为0')
    expect(host.textContent).toContain('上一个交易日有收盘价且有效')
    expect(host.textContent).not.toContain('默认14:45:00截止，共225根')
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, '2.8')
      input!.dispatchEvent(new Event('input', { bubbles: true }))
    })
    await act(async () => [...host.querySelectorAll('button')].find(b => b.textContent?.includes('保存'))!.click())
    expect(save.mock.calls[0][0].params).toEqual({ ...signal.params, current_return_max: .028,
      time: '14:45:00', evaluation_delay_minutes: 1, market_validity: 'price_amount_prev_close_v2' })
    expect(signal.params).not.toHaveProperty('time')
  } finally {
    await act(async () => root.unmount())
    vi.restoreAllMocks()
  }
})

it('keeps changed seconds only in the draft until save and restores saved values on reopen', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.spyOn(api, 'customSignalsOptions').mockResolvedValue({ fields: [], operators: [], kinds: [] })
  vi.spyOn(api, 'strategyAiStatus').mockResolvedValue({ configured: false } as any)
  const save = vi.spyOn(api, 'customSignalSave').mockImplementation(async signal => ({ ok: true, signal }))
  const signal: any = {
    id: 'late_session_strength_s01', name: '尾盘建仓_s01', kind: 'entry',
    timeframe: 'intraday', template: 's01_late_entry', enabled: true, conditions: [],
    params: { time: '14:44:20', evaluation_delay_minutes: 2, current_amount_min: 1e8,
      current_return_min: .02, current_return_max: .03, current_range_max: .0525,
      market_min_valid: 1000, market_positive_min: .65 },
  }
  const container = document.createElement('div')
  const rendered = createRoot(container)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const render = async (open: boolean) => act(async () => rendered.render(
    <QueryClientProvider client={qc}><MemoryRouter>
      <CustomSignalDialog open={open} signal={signal} onClose={() => {}} />
    </MemoryRouter></QueryClientProvider>))
  try {
    await render(true)
    const input = () => container.querySelector<HTMLInputElement>('input[aria-label="确认延时（秒）"]')!
    expect(input().value).toBe('120')
    expect(container.textContent).toContain('14:46:20')
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input(), '90')
      input().dispatchEvent(new Event('input', { bubbles: true }))
    })
    await render(false)
    await render(true)
    expect(input().value).toBe('120')
    expect(save).not.toHaveBeenCalled()
    await act(async () => [...container.querySelectorAll('button')].find(b => b.textContent?.includes('保存'))!.click())
    expect(save.mock.calls[0][0].params).toEqual({ ...signal.params, market_validity: 'price_amount_prev_close_v2' })
  } finally { await act(async () => rendered.unmount()); qc.clear(); vi.restoreAllMocks() }
})

it('displays the renamed S01 morning template and preserves its parameters on save', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.spyOn(api, 'customSignalsOptions').mockResolvedValue({ fields: [], operators: [], kinds: [] })
  vi.spyOn(api, 'strategyAiStatus').mockResolvedValue({ configured: false } as any)
  const save = vi.spyOn(api, 'customSignalSave').mockImplementation(async signal => ({ ok: true, signal }))
  const signal: any = {
    id: 'morning_liquidation_s01', name: '早盘清仓_s01', kind: 'exit', enabled: true,
    template: 's01_morning_exit', timeframe: 'intraday', conditions: [],
    params: { vwap_buffer: .001, peak_profit_min: .005, peak_retrace_min: .003,
      return_15m_max: 0, decline_3m: .003, volume_ratio_min: 1.5,
      guard_time: '11:22', sell_deadline: '13:30' },
  }
  const host = document.createElement('div')
  const root = createRoot(host)
  const qc = new QueryClient()
  try {
    await act(async () => root.render(<QueryClientProvider client={qc}><MemoryRouter>
      <CustomSignalDialog open signal={signal} onClose={() => {}} />
    </MemoryRouter></QueryClientProvider>))
    expect(host.querySelector('option[value="s01_morning_exit"]')!.textContent).toBe('早盘清仓_S01')
    await act(async () => [...host.querySelectorAll('button')].find(b => b.textContent?.includes('保存'))!.click())
    expect(save.mock.calls[0][0].params).toEqual(signal.params)
  } finally { await act(async () => root.unmount()); qc.clear(); vi.restoreAllMocks() }
})

it('keeps S01 bindings out of generic minute trigger choices', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  const host = document.createElement('div')
  const root = createRoot(host)
  const qc = new QueryClient({ defaultOptions: { queries: { staleTime: Infinity } } })
  qc.setQueryData(QK.customSignals, { signals: [
    { id: 'late_session_strength_s01', name: '尾盘建仓_s01', kind: 'entry', enabled: true,
      timeframe: 'intraday', template: 's01_late_entry', conditions: [] },
    { id: 'late', name: '普通尾盘', kind: 'entry', enabled: true,
      timeframe: 'intraday', template: 'late_session_strength', conditions: [] },
  ] })
  try {
    await act(async () => root.render(<QueryClientProvider client={qc}>
      <SignalPicker signals={[]} kind="entry" onChange={() => {}}
        options={{ builtinSignals: [], allowIntraday: true }} />
    </QueryClientProvider>))
    expect(host.textContent).toContain('普通尾盘')
    expect(host.textContent).not.toContain('尾盘建仓_s01')
  } finally { await act(async () => root.unmount()); qc.clear() }
})
