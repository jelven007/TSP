// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, it, vi } from 'vitest'
import { StrategySettingsDialog } from './StrategySettingsDialog'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'

vi.mock('@/components/ScoringEditor', () => ({ ScoringEditor: () => null }))
vi.mock('./OverlayFilterEditor', () => ({ OverlayFilterEditor: () => null }))
vi.mock('@/components/signals/CustomSignalDialog', () => ({ CustomSignalDialog: () => null }))
vi.mock('@/pages/backtest/AddFactorSignalDialog', () => ({ AddFactorSignalDialog: () => null }))
Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
let root: Root
let host: HTMLDivElement
let client: QueryClient
afterEach(async () => {
  await act(async () => root?.unmount())
  host?.remove()
  client?.clear()
  vi.restoreAllMocks()
})
async function click(label: string) {
  const button = document.querySelector<HTMLButtonElement>(`button[aria-label="${label}"]`)
    ?? [...document.querySelectorAll('button')].find(b => b.textContent?.trim() === label)
  expect(button).toBeTruthy()
  await act(async () => button!.click())
}
it('saves existing entry and exit selections with strategy settings and reloads both', async () => {
  let detail: any = {
    id: 'sample', name: '示例策略', source: 'custom', execution_backend: 'minute_filter',
    asset_types: ['stock'], params: [], params_defaults: {}, basic_filter: { enabled: true, price_min: 5 },
    entry_signals: ['signal_ma_golden_cross'], exit_signals: [], scoring: {},
  }
  vi.spyOn(api, 'strategyGet').mockImplementation(async () => detail)
  vi.spyOn(api, 'watchlistGroups').mockResolvedValue({ groups: [] } as any)
  vi.spyOn(api, 'customSignalsList').mockResolvedValue({ signals: [
    { id: 'late_session_strength', name: '尾盘走强', kind: 'entry', enabled: true, conditions: [],
      timeframe: 'intraday', template: 'late_session_strength' },
    { id: 'morning_liquidation', name: '早盘清仓', kind: 'exit', enabled: true, conditions: [],
      timeframe: 'intraday', template: 'morning_liquidation' },
  ] })
  const save = vi.spyOn(api, 'strategySaveConfig').mockImplementation(async (_sid, value) => {
    detail = { ...detail, ...value }
    return { ok: true }
  })
  const closed = vi.fn()
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await client.fetchQuery({ queryKey: QK.customSignals, queryFn: api.customSignalsList })
  const render = (id: string | null) => root.render(<QueryClientProvider client={client}>
    <MemoryRouter><StrategySettingsDialog strategyId={id} onClose={closed} /></MemoryRouter>
  </QueryClientProvider>)
  await act(async () => render('sample'))
  for (const [side, name] of [['入场', '尾盘走强'], ['出场', '早盘清仓']]) {
    await click(`选择已有${side}信号`)
    await act(async () => document.querySelector<HTMLInputElement>(`input[aria-label="${name}"]`)!.click())
    await click('应用选择')
  }
  expect(save).not.toHaveBeenCalled()
  await click('保存设置')
  expect(save).toHaveBeenCalledWith('sample', expect.objectContaining({
    entry_signals: ['signal_ma_golden_cross', 'csgi_late_session_strength'],
    exit_signals: ['csgi_morning_liquidation'],
  }))
  expect(closed).toHaveBeenCalledOnce()
  await act(async () => render(null))
  await act(async () => render('sample'))
  for (const [side, name] of [['入场', '尾盘走强'], ['出场', '早盘清仓']]) {
    await click(`选择已有${side}信号`)
    expect(document.querySelector<HTMLInputElement>(`input[aria-label="${name}"]`)!.checked).toBe(true)
    await click('取消')
  }
})
