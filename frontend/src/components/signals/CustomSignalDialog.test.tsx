// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, it, vi } from 'vitest'
import { CustomSignalDialog } from './CustomSignalDialog'
import { api } from '@/lib/api'
import { LATE_SESSION_DEFAULTS } from '@/lib/customSignals'

vi.mock('@/components/Modal', () => ({
  Modal: ({ children }: any) => <div>{children}</div>,
  useModalPortalContainer: () => document.body,
}))

let root: Root
let qc: QueryClient
afterEach(async () => {
  await act(async () => root?.unmount())
  qc?.clear()
  vi.restoreAllMocks()
})

it('creates and edits the late-session entry preset with all configurable defaults', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.spyOn(api, 'customSignalsOptions').mockResolvedValue({ fields: [], operators: [], kinds: [] })
  vi.spyOn(api, 'strategyAiStatus').mockResolvedValue({ configured: false } as any)
  const save = vi.spyOn(api, 'customSignalSave').mockImplementation(async signal => ({ ok: true, signal }))
  const onSaved = vi.fn()
  const host = document.createElement('div')
  root = createRoot(host)
  qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const render = (signal?: any) => root.render(<QueryClientProvider client={qc}><MemoryRouter>
    <CustomSignalDialog open defaultKind="entry" signal={signal} onClose={() => {}} onSaved={onSaved} />
  </MemoryRouter></QueryClientProvider>)
  await act(async () => render())
  const template = host.querySelector<HTMLSelectElement>('select[aria-label="信号模板"]')
  expect(template).not.toBeNull()
  await act(async () => {
    template!.value = 'late_session_strength'
    template!.dispatchEvent(new Event('change', { bubbles: true }))
  })
  expect(host.querySelector('button[aria-label="启动时间"]')?.textContent).toBe('14:30:00')
  expect([...host.querySelectorAll<HTMLInputElement>('input[type="number"]')].map(input => input.value))
    .toEqual(['30', '60', '', '', '', '', '', '', '7', '4', '', '', '', ''])
  expect(host.textContent).toContain('14:30:30')
  expect(host.textContent).toContain('启动后30秒确认')
  expect(host.textContent).not.toContain('启动后90秒')
  expect(host.textContent).toContain('K线截止 14:30:00')
  expect(host.textContent).toContain('N个MA5值比较N−1次')
  expect(host.textContent).toContain('取启动时最新完成5分K')
  expect(host.textContent).toContain('当日股票池')
  await act(async () => [...host.querySelectorAll('button')].find(b => b.textContent?.includes('保存'))!.click())
  expect(save).toHaveBeenCalledWith(expect.objectContaining({
    name: '尾盘走强', kind: 'entry', timeframe: 'intraday', template: 'late_session_strength',
    params: LATE_SESSION_DEFAULTS,
  }), { createOnly: true })
  const stored = save.mock.calls[0][0]
  expect(stored.params).toEqual(expect.objectContaining({
    change_min_pct: null, change_max_pct: null,
  }))
  await act(async () => render({ ...stored, params: { ...stored.params, ma5_1m_count: 9 } }))
  expect(host.querySelector<HTMLInputElement>('input[aria-label="1分钟MA5连增数量"]')?.value).toBe('9')
  await act(async () => render({ ...stored, params: { ...stored.params, evaluation_delay_minutes: 1.5 } }))
  expect(host.textContent).not.toContain('保存后改为')
  expect(host.textContent).toContain('14:31:30')
  await act(async () => render({ ...stored, params: {
    time: '14:35:00', pool_up_min_pct: 65, change_max_pct: 3.5, change_min_pct: 0.6,
    ma5_1m_count: 8, ma5_5m_count: 3,
  } }))
  expect(host.textContent).toContain('原为无延迟旧定义')
  expect(host.textContent).toContain('14:35:30')
  expect([...host.querySelectorAll<HTMLInputElement>('input[type="number"]')].map(input => input.value))
    .toEqual(['30', '65', '', '', '', '0.6', '3.5', '', '8', '3', '', '', '', ''])
  await act(async () => [...host.querySelectorAll('button')].find(b => b.textContent?.includes('保存'))!.click())
  expect(save.mock.calls.at(-1)?.[0].params).toEqual({
    ...LATE_SESSION_DEFAULTS,
    time: '14:35:00', pool_up_min_pct: 65, change_max_pct: 3.5, change_min_pct: 0.6,
    ma5_1m_count: 8, ma5_5m_count: 3, evaluation_delay_minutes: 0.5,
  })
  await act(async () => render({ ...stored, params: { ...stored.params, time: '15:00:00' } }))
  const savedCount = save.mock.calls.length
  await act(async () => [...host.querySelectorAll('button')].find(b => b.textContent?.includes('保存'))!.click())
  expect(save.mock.calls.length).toBe(savedCount)
  expect(host.textContent).toContain('09:30:00～11:30:00、13:00:00～14:59:30')
  await act(async () => render({ ...stored, params: { ...stored.params, time: '14:59:31' } }))
  await act(async () => [...host.querySelectorAll('button')].find(b => b.textContent?.includes('保存'))!.click())
  expect(save.mock.calls.length).toBe(savedCount)
  await act(async () => render({ ...stored, params: { ...stored.params, time: '14:58:00' } }))
  expect(host.textContent).toContain('14:58:30')
  await act(async () => [...host.querySelectorAll('button')].find(b => b.textContent?.includes('保存'))!.click())
  expect(save.mock.calls.at(-1)?.[0].params).toEqual({ ...stored.params, time: '14:58:00' })
})

it('saves the morning exit preset and reloads configurable times and returns', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.spyOn(api, 'customSignalsOptions').mockResolvedValue({ fields: [], operators: [], kinds: [] })
  vi.spyOn(api, 'strategyAiStatus').mockResolvedValue({ configured: false } as any)
  const save = vi.spyOn(api, 'customSignalSave').mockImplementation(async signal => ({ ok: true, signal }))
  const host = document.createElement('div')
  root = createRoot(host)
  qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const render = (signal?: any) => root.render(<QueryClientProvider client={qc}><MemoryRouter>
    <CustomSignalDialog open signal={signal} onClose={() => {}} />
  </MemoryRouter></QueryClientProvider>)
  await act(async () => render())
  const template = host.querySelector<HTMLSelectElement>('select[aria-label="信号模板"]')!
  expect([...template.options].some(option => option.value === 'morning_liquidation')).toBe(true)
  await act(async () => {
    template.value = 'morning_liquidation'
    template.dispatchEvent(new Event('change', { bubbles: true }))
  })
  expect(host.querySelector('button[aria-label="止损时间"]')?.textContent).toBe('10:30:00')
  expect(host.querySelector<HTMLInputElement>('input[aria-label="涨幅阈值（%）"]')?.value).toBe('6')
  expect(host.textContent).toContain('下一个交易日')
  await act(async () => [...host.querySelectorAll('button')].find(b => b.textContent?.includes('保存'))!.click())
  expect(save).toHaveBeenCalledWith(expect.objectContaining({
    name: '早盘清仓', kind: 'exit', timeframe: 'intraday', template: 'morning_liquidation',
    conditions: [], params: { start_time: null, decline_3m_pct: null, volume_ratio_min: null,
      time: '10:30:00', fallback_time: '10:45:00', rise_pct: 6, fall_pct: 6,
      take_profit_pct: null, stop_loss_pct: null },
  }), { createOnly: true })
  const stored = save.mock.calls[0][0]
  await act(async () => render({ ...stored, params: { time: '10:15:32', fallback_time: '10:50:00', rise_pct: 5, fall_pct: 2 } }))
  expect(host.querySelector('button[aria-label="止损时间"]')?.textContent).toBe('10:15:32')
  expect(host.querySelector<HTMLInputElement>('input[aria-label="涨幅阈值（%）"]')?.value).toBe('5')
})
