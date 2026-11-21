// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, it, vi } from 'vitest'
import { CustomSignalDialog } from './CustomSignalDialog'
import { api, type CustomSignal, type LateSessionStrengthParams } from '@/lib/api'
import { LATE_SESSION_DEFAULTS, sortSignalsByName } from '@/lib/customSignals'
import { defaultEntryGroups } from '@/lib/entryGroups'

vi.mock('@/components/Modal', () => ({
  Modal: ({ children }: any) => <div>{children}</div>, useModalPortalContainer: () => document.body,
}))
let root: Root
let qc: QueryClient
afterEach(async () => {
  await act(async () => root?.unmount())
  qc?.clear()
  vi.restoreAllMocks()
  document.body.innerHTML = ''
})

it('sorts names naturally without changing cached signal order', () => {
  const signals = ['s10_入场', 's2_入场', 's1_入场'].map(name => ({ name, id: name } as CustomSignal))
  expect(sortSignalsByName(signals).map(s => s.name)).toEqual(['s1_入场', 's2_入场', 's10_入场'])
  expect(signals[0].name).toBe('s10_入场')
})

it('edits A/B independently, saves and reopens without changing the clocks', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.spyOn(api, 'customSignalsOptions').mockResolvedValue({ fields: [], operators: [], kinds: [] })
  vi.spyOn(api, 'strategyAiStatus').mockResolvedValue({ configured: false } as any)
  const save = vi.spyOn(api, 'customSignalSave').mockImplementation(async signal => ({ ok: true, signal }))
  const original: CustomSignal = {
    id: 'late_session_strength_s03', name: 's03_早盘建仓', kind: 'entry', enabled: true,
    timeframe: 'intraday', template: 'late_session_strength', conditions: [],
    params: { ...LATE_SESSION_DEFAULTS, time: '10:00:00', end_time: '10:30:00',
      entry_groups: defaultEntryGroups() },
  }
  const host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const render = (signal: CustomSignal, open = true) => root.render(
    <QueryClientProvider client={qc}><MemoryRouter>
      <CustomSignalDialog open={open} signal={signal} onClose={() => {}} />
    </MemoryRouter></QueryClientProvider>)
  await act(async () => render(original))
  expect(host.textContent).toContain('两组为“或”关系')
  const input = (label: string) => host.querySelector<HTMLInputElement>(`input[aria-label="${label}"]`)!
  const change = async (label: string, value: string) => act(async () => {
    const el = input(label)
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(el, value)
    el.dispatchEvent(new Event('input', { bubbles: true }))
  })
  expect(input('A组 综合分下限').value).toBe('55')
  await change('A组 综合分下限', '58')
  expect((original.params as LateSessionStrengthParams).entry_groups!.a.score_min).toBe(55)
  const submit = async () => act(async () =>
    [...host.querySelectorAll('button')].find(b => b.textContent?.includes('保存'))!.click())
  await submit()
  expect(save).toHaveBeenCalledTimes(1)
  const saved = save.mock.calls[0][0]
  expect(saved.params).toMatchObject({
    time: '10:00:00', evaluation_delay_minutes: .5, end_time: '10:30:00',
    entry_groups: { a: { score_min: 58 }, b: { score_min: 70 } },
  })
  await act(async () => render(saved, false))
  await act(async () => render(saved))
  expect(input('A组 综合分下限').value).toBe('58')
  await change('A组 五维排名下限', '21')
  await submit()
  expect(save).toHaveBeenCalledTimes(1)
  expect(host.textContent).toContain('下限不得大于上限')
})
