// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, it, vi } from 'vitest'
import { CustomSignalDialog } from './CustomSignalDialog'
import { api, type CustomSignal } from '@/lib/api'
import { LATE_SESSION_DEFAULTS, MORNING_LIQUIDATION_DEFAULTS } from '@/lib/customSignals'

vi.mock('@/components/Modal', () => ({
  Modal: ({ children }: any) => <div role="dialog">{children}</div>,
  useModalPortalContainer: () => document.body,
}))
vi.mock('@/components/Toast', () => ({ toast: vi.fn() }))
Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
let root: Root
let host: HTMLDivElement
let qc: QueryClient
afterEach(async () => {
  await act(async () => root?.unmount())
  qc?.clear()
  host?.remove()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

const preset = (template: 'late_session_strength' | 'morning_liquidation'): CustomSignal => ({
  id: template, name: '已有信号', enabled: false, timeframe: 'intraday', template,
  kind: template === 'late_session_strength' ? 'entry' : 'exit', conditions: [],
  params: template === 'late_session_strength'
    ? { ...LATE_SESSION_DEFAULTS, pool_up_min_pct: 75 }
    : { ...MORNING_LIQUIDATION_DEFAULTS, rise_pct: 8 },
})

async function fixture(original: CustomSignal) {
  const stored = new Map([[original.id, structuredClone(original)]])
  const writes: { url: string; signal: CustomSignal }[] = []
  vi.spyOn(api, 'customSignalsOptions').mockResolvedValue({ fields: [], operators: [], kinds: [] })
  vi.spyOn(api, 'strategyAiStatus').mockResolvedValue({ configured: false } as any)
  vi.stubGlobal('fetch', vi.fn(async (url: string, init: RequestInit) => {
    const signal = JSON.parse(String(init.body)) as CustomSignal
    writes.push({ url, signal })
    if (url.includes('create_only=true') && stored.has(signal.id))
      return new Response(JSON.stringify({ detail: '信号标识已存在，请使用其他标识' }), { status: 409 })
    stored.set(signal.id, structuredClone(signal))
    return new Response(JSON.stringify({ ok: true, signal }))
  }))
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const close = vi.fn()
  const render = async (open: boolean, signal?: CustomSignal) => {
    await act(async () => root.render(<QueryClientProvider client={qc}><MemoryRouter>
      <CustomSignalDialog open={open} signal={signal} onClose={close} />
    </MemoryRouter></QueryClientProvider>))
  }
  const select = async (value: string) => {
    const input = host.querySelector<HTMLSelectElement>('select[aria-label="信号模板"]')!
    await act(async () => {
      input.value = value
      input.dispatchEvent(new Event('change', { bubbles: true }))
    })
  }
  const id = () => host.querySelector<HTMLInputElement>('input[placeholder="如 low_touches_ma5"]')!
  const changeId = async (value: string) => {
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(id(), value)
      id().dispatchEvent(new Event('input', { bubbles: true }))
    })
  }
  const submit = async () => {
    await act(async () => [...host.querySelectorAll('button')].find(b => b.textContent?.includes('保存'))!.click())
  }
  await render(true)
  return { stored, writes, close, render, select, id, changeId, submit }
}

it.each(['late_session_strength', 'morning_liquidation'] as const)(
  'creates two independent %s signals without overwriting the original', async template => {
    const original = preset(template)
    const f = await fixture(original)
    for (let i = 0; i < 2; i++) {
      if (i) { await f.render(false); await f.render(true) }
      await f.select(template)
      const id = f.id().value
      expect(id).toMatch(/^[a-z0-9_]{1,40}$/)
      await f.submit()
      expect(f.stored.get(original.id)).toEqual(original)
      expect(f.stored.size).toBe(i + 2)
      expect(f.writes.at(-1)?.url).toBe('/api/custom-signals?create_only=true')
      expect(f.stored.get(id)).toMatchObject({ template, enabled: true })
    }
  },
)

it('keeps editing on the original identity and resets it before the next creation', async () => {
  const original = preset('morning_liquidation')
  const f = await fixture(original)
  await f.render(true, original)
  expect(f.id().disabled).toBe(true)
  await f.submit()
  expect(f.writes[0].url).toBe('/api/custom-signals')
  expect(f.stored.size).toBe(1)
  expect(f.stored.get(original.id)).toEqual(original)
  await f.render(false)
  await f.render(true)
  expect(f.id().disabled).toBe(false)
  await f.select('morning_liquidation')
  await f.submit()
  expect(f.stored.size).toBe(2)
  expect(f.stored.get(original.id)).toEqual(original)
})

it('shows a duplicate-id error and preserves the draft for correction', async () => {
  const original = preset('morning_liquidation')
  const f = await fixture(original)
  await f.select('morning_liquidation')
  await f.changeId(original.id)
  await f.submit()
  expect(host.textContent).toContain('信号标识已存在')
  expect(f.close).not.toHaveBeenCalled()
  expect(f.stored.get(original.id)).toEqual(original)
  expect(f.id().value).toBe(original.id)
  await f.changeId('my_morning_exit')
  await f.submit()
  expect(f.stored.size).toBe(2)
  expect(f.close).toHaveBeenCalledTimes(1)
})

it('preserves a user-entered id when switching templates', async () => {
  const f = await fixture(preset('morning_liquidation'))
  await f.changeId('my_signal')
  await f.select('late_session_strength')
  await f.select('morning_liquidation')
  expect(f.id().value).toBe('my_signal')
  await f.submit()
  expect(f.stored.get('my_signal')).toMatchObject({ template: 'morning_liquidation' })
})
