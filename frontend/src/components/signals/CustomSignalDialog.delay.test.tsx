// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, it, vi } from 'vitest'
import { CustomSignalDialog } from './CustomSignalDialog'
import { api, type CustomSignal } from '@/lib/api'
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

it.each(['', '0', '45', '930', '30.5'])('rejects invalid delay %s before submitting', async value => {
  const f = await fixture(signal(1.5))
  await f.setSeconds(value)
  await f.submit()
  expect(f.save).not.toHaveBeenCalled()
  expect(host.textContent).toContain('30～900秒')
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
