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

async function fixture(signal?: any) {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.spyOn(api, 'customSignalsOptions').mockResolvedValue({ fields: [], operators: [], kinds: [] })
  vi.spyOn(api, 'strategyAiStatus').mockResolvedValue({ configured: false } as any)
  const save = vi.spyOn(api, 'customSignalSave').mockImplementation(async value => ({ ok: true, signal: value }))
  const host = document.createElement('div')
  const root = createRoot(host)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const render = async (value?: any) => {
    await act(async () => root.render(<QueryClientProvider client={qc}><MemoryRouter>
      <CustomSignalDialog open signal={value} onClose={() => {}} />
    </MemoryRouter></QueryClientProvider>))
  }
  await render(signal)
  if (!signal) {
    const template = host.querySelector<HTMLSelectElement>('select[aria-label="信号模板"]')!
    await act(async () => {
      template.value = 'morning_liquidation'
      template.dispatchEvent(new Event('change', { bubbles: true }))
    })
  }
  const input = (name: string) => host.querySelector<HTMLInputElement>(`input[aria-label="${name}"]`)
  const time = (name: string) => host.querySelector<HTMLButtonElement>(`button[aria-label="${name}"]`)
  const fill = async (name: string, value: string) => {
    if (time(name)) {
      await act(async () => time(name)!.click())
      const popup = document.querySelector(`[role="group"][aria-label="${name}选择"]`)!
      for (const [index, part] of value.split(':').entries()) {
        const option = popup.querySelector<HTMLButtonElement>(`[aria-label="${part} ${['时', '分', '秒'][index]}"]`)!
        expect(option).not.toBeNull()
        await act(async () => option.click())
      }
      await act(async () => [...popup.querySelectorAll('button')].find(button => button.textContent === '确定')!.click())
      return
    }
    const element = input(name)!
    expect(element).not.toBeNull()
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(element, value)
      element.dispatchEvent(new Event('input', { bubbles: true }))
    })
  }
  const submit = async () => {
    await act(async () => [...host.querySelectorAll('button')].find(b => b.textContent?.includes('保存'))!.click())
  }
  const close = async () => { await act(async () => root.unmount()); vi.restoreAllMocks() }
  return { host, save, input, time, fill, submit, render, close }
}

it('saves and reloads two independent return thresholds and both times', async () => {
  const f = await fixture()
  try {
    expect(f.time('止损时间')?.textContent).toBe('10:30:00')
    expect(f.time('兜底时间')?.textContent).toBe('10:45:00')
    expect(f.input('涨幅阈值（%）')?.value).toBe('6')
    expect(f.input('跌幅阈值（%）')?.value).toBe('6')
    await f.fill('涨幅阈值（%）', '8')
    await f.fill('跌幅阈值（%）', '2')
    await f.fill('止损时间', '10:15:32')
    await f.fill('兜底时间', '10:50:15')
    await f.submit()
    expect(f.save).toHaveBeenCalledWith(expect.objectContaining({
      params: { start_time: null, decline_3m_pct: null, volume_ratio_min: null,
        time: '10:15:32', fallback_time: '10:50:15', rise_pct: 8, fall_pct: 2 },
    }), { createOnly: true })
    await f.render(f.save.mock.calls[0][0])
    expect(f.input('跌幅阈值（%）')?.value).toBe('2')
    expect(f.time('兜底时间')?.textContent).toBe('10:50:15')
  } finally { await f.close() }
})

it('rejects fallback no later than stop and nonpositive fall thresholds', async () => {
  const f = await fixture()
  try {
    await f.fill('兜底时间', '10:30:00')
    await f.submit()
    expect(f.save).not.toHaveBeenCalled()
    expect(f.host.textContent).toContain('晚于止损时间')
    await f.fill('兜底时间', '10:45:00')
    await f.fill('跌幅阈值（%）', '0')
    await f.submit()
    expect(f.save).not.toHaveBeenCalled()
    expect(f.host.textContent).toContain('0.1～100.0')
  } finally { await f.close() }
})

it('converts legacy amplitude editing explicitly while preserving its chosen stop time', async () => {
  const f = await fixture({
    id: 'morning', name: '早盘清仓', kind: 'exit', timeframe: 'intraday',
    template: 'morning_liquidation', enabled: true, conditions: [],
    params: { time: '10:15:32', amplitude_pct: 5 },
  })
  try {
    expect(f.time('止损时间')?.textContent).toBe('10:15:32')
    expect(f.host.textContent).toContain('保存后')
    await f.submit()
    expect(f.save.mock.calls[0][0].params).toEqual({
      start_time: null, decline_3m_pct: null, volume_ratio_min: null,
      time: '10:15:32', fallback_time: '10:45:00', rise_pct: 6, fall_pct: 6,
    })
  } finally { await f.close() }
})

it('shows seven parameters in order and preserves all empty fields on save and reopen', async () => {
  const empty = { start_time: null, rise_pct: null, fall_pct: null, decline_3m_pct: null,
    volume_ratio_min: null, time: null, fallback_time: null }
  const f = await fixture({ id: 'optional', name: '早盘清仓', kind: 'exit', timeframe: 'intraday',
    template: 'morning_liquidation', enabled: true, conditions: [], params: empty })
  try {
    const grid = f.host.querySelector('[aria-label="早盘清仓参数"]')!
    expect(grid).not.toBeNull()
    expect(grid.classList.contains('sm:grid-cols-3')).toBe(true)
    expect([...grid.querySelectorAll('label > span')].map(span => span.textContent)).toEqual([
      '启动时间（北京时间）', '涨幅阈值（%）', '跌幅阈值（%）',
      '3分钟跌幅门槛（%）', '最近3根/之前10根量比下限', '止损时间（北京时间）', '兜底时间（北京时间）',
    ])
    expect(f.time('启动时间')?.textContent).toBe('选择时间')
    expect(f.input('3分钟跌幅门槛（%）')?.value).toBe('')
    await f.fill('3分钟跌幅门槛（%）', '0.09')
    await f.submit()
    expect(f.save).not.toHaveBeenCalled()
    await f.fill('3分钟跌幅门槛（%）', '100')
    await f.fill('最近3根/之前10根量比下限', '.1')
    await f.submit()
    expect(f.save.mock.calls[0][0].params).toEqual({ ...empty, decline_3m_pct: 100, volume_ratio_min: .1 })
    await f.fill('3分钟跌幅门槛（%）', '')
    await f.fill('最近3根/之前10根量比下限', '')
    await f.submit()
    expect(f.save.mock.calls[1][0].params).toEqual(empty)
    await f.render(f.save.mock.calls[1][0])
    expect(f.time('止损时间')?.textContent).toBe('选择时间')
    expect(f.time('兜底时间')?.textContent).toBe('选择时间')
    expect(f.input('涨幅阈值（%）')?.value).toBe('')
  } finally { await f.close() }
})
