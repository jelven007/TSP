// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { expect, it, vi } from 'vitest'
import { CustomSignalDialog } from './CustomSignalDialog'
import { api } from '@/lib/api'

vi.mock('@/components/Modal', () => ({
  Modal: ({ children }: any) => <div>{children}</div>,
  useModalPortalContainer: () => document.body,
}))

it('creates and edits the late-session entry preset with all configurable defaults', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.spyOn(api, 'customSignalsOptions').mockResolvedValue({ fields: [], operators: [], kinds: [] })
  vi.spyOn(api, 'strategyAiStatus').mockResolvedValue({ configured: false } as any)
  const save = vi.spyOn(api, 'customSignalSave').mockImplementation(async signal => ({ ok: true, signal }))
  const onSaved = vi.fn()
  const host = document.createElement('div')
  const root = createRoot(host)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
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
  expect(host.querySelector<HTMLInputElement>('input[aria-label="判断时间"]')?.value).toBe('14:30:00')
  expect([...host.querySelectorAll<HTMLInputElement>('input[type="number"]')].map(input => input.value))
    .toEqual(['0.4', '4', '60', '7', '4'])
  expect(host.textContent).toContain('当日股票池')
  await act(async () => [...host.querySelectorAll('button')].find(b => b.textContent?.includes('保存'))!.click())
  expect(save).toHaveBeenCalledWith(expect.objectContaining({
    name: '尾盘走强', kind: 'entry', timeframe: 'intraday', template: 'late_session_strength',
    params: { time: '14:30:00', change_min_pct: 0.4, change_max_pct: 4,
      pool_up_min_pct: 60, ma5_1m_count: 7, ma5_5m_count: 4 },
  }))
  const stored = save.mock.calls[0][0]
  await act(async () => render({ ...stored, params: { ...stored.params, ma5_1m_count: 9 } }))
  expect(host.querySelector<HTMLInputElement>('input[aria-label="1分钟 MA5 连增数量"]')?.value).toBe('9')
  await act(async () => root.unmount())
  vi.restoreAllMocks()
})

it('saves the morning exit preset and reloads configurable times and returns', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.spyOn(api, 'customSignalsOptions').mockResolvedValue({ fields: [], operators: [], kinds: [] })
  vi.spyOn(api, 'strategyAiStatus').mockResolvedValue({ configured: false } as any)
  const save = vi.spyOn(api, 'customSignalSave').mockImplementation(async signal => ({ ok: true, signal }))
  const host = document.createElement('div')
  const root = createRoot(host)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
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
    conditions: [], params: { time: '10:30:00', fallback_time: '10:45:00', rise_pct: 6, fall_pct: 6 },
  }))
  const stored = save.mock.calls[0][0]
  await act(async () => render({ ...stored, params: { time: '10:15:32', fallback_time: '10:50:00', rise_pct: 5, fall_pct: 2 } }))
  expect(host.querySelector('button[aria-label="止损时间"]')?.textContent).toBe('10:15:32')
  expect(host.querySelector<HTMLInputElement>('input[aria-label="涨幅阈值（%）"]')?.value).toBe('5')
  await act(async () => root.unmount())
  vi.restoreAllMocks()
})
