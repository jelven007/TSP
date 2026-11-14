// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, it, vi } from 'vitest'
import { CustomSignalDialog } from './CustomSignalDialog'
import { api, type CustomSignal } from '@/lib/api'

Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
let root: Root
let host: HTMLDivElement
afterEach(async () => {
  await act(async () => root?.unmount())
  host?.remove()
  vi.restoreAllMocks()
})

const definition = {
  id: 'morning_liquidation', name: '早盘清仓', kind: 'exit' as const, timeframe: 'intraday' as const,
  template: 'morning_liquidation' as const, conditions: [], enabled: true,
  params: { time: '10:30:00', fallback_time: '10:45:00', rise_pct: 6, fall_pct: 6 },
}

async function fixture(initialSignal: CustomSignal = definition) {
  vi.spyOn(HTMLElement.prototype, 'offsetParent', 'get').mockImplementation(function (this: HTMLElement) {
    return this.hidden ? null : document.body
  })
  vi.spyOn(api, 'customSignalsOptions').mockResolvedValue({ fields: [], operators: [], kinds: [] })
  vi.spyOn(api, 'strategyAiStatus').mockResolvedValue({ configured: false } as any)
  const save = vi.spyOn(api, 'customSignalSave').mockImplementation(async signal => ({ ok: true, signal }))
  const close = vi.fn()
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const render = async (signal: CustomSignal = initialSignal) => {
    await act(async () => root.render(<QueryClientProvider client={qc}><MemoryRouter>
      <CustomSignalDialog open signal={signal} onClose={close} />
    </MemoryRouter></QueryClientProvider>))
  }
  await render()
  const time = (name: string) => document.querySelector<HTMLButtonElement>(`button[aria-label="${name}"]`)!
  const select = async (name: string, value: string) => {
    expect(time(name)).not.toBeNull()
    await act(async () => time(name).click())
    const popup = document.querySelector(`[role="group"][aria-label="${name}选择"]`)!
    for (const [index, part] of value.split(':').entries()) {
      const option = popup.querySelector<HTMLButtonElement>(`[aria-label="${part} ${['时', '分', '秒'][index]}"]`)!
      expect(option).not.toBeNull()
      await act(async () => option.click())
    }
    await act(async () => [...popup.querySelectorAll('button')].find(button => button.textContent === '确定')!.click())
  }
  return { save, close, render, time, select }
}

it('uses the shared second picker for both clocks and reloads their saved exact seconds', async () => {
  const f = await fixture()
  expect(f.time('止损时间')?.textContent).toBe('10:30:00')
  expect(f.time('兜底时间')?.textContent).toBe('10:45:00')
  expect(document.querySelector('input[type="time"]')).toBeNull()
  await f.select('止损时间', '10:15:32')
  await f.select('兜底时间', '10:50:15')
  expect(f.save).not.toHaveBeenCalled()
  await act(async () => [...document.querySelectorAll('button')].find(button => button.textContent?.includes('保存'))!.click())
  expect(f.save).toHaveBeenCalledWith(expect.objectContaining({
    params: { time: '10:15:32', fallback_time: '10:50:15', rise_pct: 6, fall_pct: 6 },
  }), { createOnly: false })
  await f.render(f.save.mock.calls[0][0] as typeof definition)
  expect(f.time('止损时间')?.textContent).toBe('10:15:32')
  expect(f.time('兜底时间')?.textContent).toBe('10:50:15')
})

it('rejects a fallback at the same second as stop after using the shared picker', async () => {
  const f = await fixture()
  await f.select('兜底时间', '10:30:00')
  await act(async () => [...document.querySelectorAll('button')].find(button => button.textContent?.includes('保存'))!.click())
  expect(f.save).not.toHaveBeenCalled()
  expect(document.querySelector('[role="dialog"]')?.textContent).toContain('晚于止损时间')
})

it('selects and reloads a functional second-precision late-session start', async () => {
  const f = await fixture({
    id: 'late_session_strength', name: '尾盘建仓', kind: 'entry', timeframe: 'intraday',
    template: 'late_session_strength', conditions: [], enabled: true,
    params: { time: '14:30:00', change_min_pct: 0.4, change_max_pct: 4,
      pool_up_min_pct: 60, ma5_1m_count: 7, ma5_5m_count: 4, evaluation_delay_minutes: 1.5 },
  })
  expect(f.time('启动时间')?.textContent).toBe('14:30:00')
  expect(document.querySelector('input[type="time"]')).toBeNull()
  await act(async () => f.time('启动时间').click())
  expect(document.querySelector('[aria-label="00 秒"]')).not.toBeNull()
  await act(async () => document.querySelector<HTMLButtonElement>('[aria-label="35 分"]')!.click())
  await act(async () => document.activeElement?.dispatchEvent(
    new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }),
  ))
  expect(f.close).not.toHaveBeenCalled()
  expect(f.time('启动时间').textContent).toBe('14:30:00')
  await f.select('启动时间', '14:30:20')
  expect(f.save).not.toHaveBeenCalled()
  expect(document.querySelector('[role="dialog"]')?.textContent).toContain('14:31:50')
  expect(document.querySelector('[role="dialog"]')?.textContent).toContain('K线截止 14:30:00')
  await act(async () => [...document.querySelectorAll('button')].find(button => button.textContent?.includes('保存'))!.click())
  expect(f.save).toHaveBeenCalledWith(expect.objectContaining({
    params: { time: '14:30:20', change_min_pct: 0.4, change_max_pct: 4,
      pool_up_min_pct: 60, ma5_1m_count: 7, ma5_5m_count: 4, evaluation_delay_minutes: 1.5 },
  }), { createOnly: false })
  await f.render(f.save.mock.calls[0][0])
  expect(f.time('启动时间')?.textContent).toBe('14:30:20')
  await f.select('启动时间', '14:58:00')
  await act(async () => [...document.querySelectorAll('button')].find(button => button.textContent?.includes('保存'))!.click())
  expect(f.save.mock.calls[1][0]).toEqual(expect.objectContaining({
    params: expect.objectContaining({ time: '14:58:00' }),
  }))
  await f.render(f.save.mock.calls[1][0])
  expect(f.time('启动时间')?.textContent).toBe('14:58:00')
  await f.select('启动时间', '14:58:31')
  const savedCount = f.save.mock.calls.length
  await act(async () => [...document.querySelectorAll('button')].find(button => button.textContent?.includes('保存'))!.click())
  expect(f.save.mock.calls.length).toBe(savedCount)
  expect(document.querySelector('[role="dialog"]')?.textContent).toContain('13:00:00～14:58:30')
})
