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
    const input = host.querySelector<HTMLInputElement>('input[aria-label="当日涨幅上限（%）"]')
    expect(input).not.toBeNull()
    expect(input!.value).toBe('3')
    expect(host.textContent).toContain('14:45')
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, '2.8')
      input!.dispatchEvent(new Event('input', { bubbles: true }))
    })
    await act(async () => [...host.querySelectorAll('button')].find(b => b.textContent?.includes('保存'))!.click())
    expect(save.mock.calls[0][0].params).toEqual({ ...signal.params, current_return_max: .028 })
  } finally {
    await act(async () => root.unmount())
    vi.restoreAllMocks()
  }
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

it('upgrades only the edit draft to volume decline and three clocks with the same stable exit ID', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.spyOn(api, 'customSignalsOptions').mockResolvedValue({ fields: [], operators: [], kinds: [] })
  vi.spyOn(api, 'strategyAiStatus').mockResolvedValue({ configured: false } as any)
  const save = vi.spyOn(api, 'customSignalSave').mockImplementation(async signal => ({ ok: true, signal }))
  const host = document.createElement('div')
  const root = createRoot(host)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const signal: any = {
    id: 'morning_liquidation_s01', name: 's01_早盘清仓', kind: 'exit', timeframe: 'intraday',
    template: 's01_morning_exit', enabled: true, conditions: [],
    params: { vwap_buffer: .0003, peak_profit_min: .02, peak_retrace_min: .005,
      return_15m_max: -.003, decline_3m: .002, volume_ratio_min: 1.5,
      guard_time: '11:26', sell_deadline: '13:30' },
  }
  try {
    await act(async () => root.render(<QueryClientProvider client={qc}><MemoryRouter>
      <CustomSignalDialog open signal={signal} onClose={() => {}} />
    </MemoryRouter></QueryClientProvider>))
    expect(host.querySelector('[aria-label="启动时间"]')?.textContent).toContain('11:15:00')
    expect(host.querySelector('[aria-label="止损时间"]')?.textContent).toContain('11:26:00')
    expect(host.querySelector('[aria-label="兜底时间"]')?.textContent).toContain('13:30:00')
    expect(host.querySelector('[aria-label="信号模板"]')?.textContent).toContain('S01放量下跌卖出')
    expect(host.querySelector('input[aria-label="低于早盘均价幅度（%）"]')).toBeNull()
    expect(host.querySelector('input[aria-label="早盘最高浮盈门槛（%）"]')).toBeNull()
    expect(host.querySelector('input[aria-label="早盘高点回落门槛（%）"]')).toBeNull()
    expect(host.querySelector('input[aria-label="最后15分钟涨幅上限（%）"]')).toBeNull()
    expect(host.querySelector<HTMLInputElement>('input[aria-label="3分钟跌幅门槛（%）"]')?.value).toBe('0.2')
    expect(host.querySelector<HTMLInputElement>('input[aria-label="最近3根/之前10根量比"]')?.value).toBe('1.5')
    expect(save).not.toHaveBeenCalled()
    expect(signal.params).not.toHaveProperty('start_time')
    await act(async () => [...host.querySelectorAll('button')].find(b => b.textContent?.includes('保存'))!.click())
    const saved = save.mock.calls[0][0]
    expect(saved.id).toBe(signal.id)
    expect(saved.params).toEqual({ decline_3m: .002, volume_ratio_min: 1.5,
      start_time: '11:15:00', stop_time: '11:26:00', fallback_time: '13:30:00' })
    expect(signal.params.guard_time).toBe('11:26')
  } finally { await act(async () => root.unmount()); qc.clear(); vi.restoreAllMocks() }
})
