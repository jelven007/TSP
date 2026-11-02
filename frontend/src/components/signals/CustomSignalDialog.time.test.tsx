// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, it, vi } from 'vitest'
import { CustomSignalDialog } from './CustomSignalDialog'
import { api } from '@/lib/api'

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

async function fixture() {
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
  const render = async (signal = definition) => {
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
  }))
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
