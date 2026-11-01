// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, it, vi } from 'vitest'
import { StrategySettingsDialog } from './StrategySettingsDialog'

const { get, save, reset } = vi.hoisted(() => ({ get: vi.fn(), save: vi.fn(), reset: vi.fn() }))
vi.mock('@/lib/api', () => ({ api: {
  strategyGet: get, strategySaveConfig: save, strategyResetConfig: reset,
  watchlistGroups: vi.fn(async () => ({ groups: [] })),
} }))
vi.mock('@/components/ScoringEditor', () => ({ ScoringEditor: () => null }))
vi.mock('./OverlayFilterEditor', () => ({ OverlayFilterEditor: () => null }))
vi.mock('./SignalPicker', () => ({ SignalPicker: () => null }))
vi.mock('@/components/signals/SignalTriggerActions', () => ({ SignalTriggerActions: () => null }))

Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
let root: Root
let host: HTMLDivElement
let client: QueryClient
const defaults = {
  name: '原始名称', description: '', basic_filter: { enabled: true, price_min: 5, exclude_new_days: 30 },
  params_defaults: {}, scoring: {}, scoring_directions: {}, overlay_filter: [],
  entry_signals: [], exit_signals: [], stop_loss: -.08, take_profit: .1,
  trailing_stop: null, trailing_take_profit_activate: null, trailing_take_profit_drawdown: null,
  max_hold_days: 10, display_limit: null, entry_risk: {}, composite_children: null,
}
const detail = { id: 'a', source: 'builtin', execution_backend: 'polars_expr',
  asset_types: ['stock'], params: [], ...defaults, name: '策略 A',
  stop_loss: -.06, take_profit: .12, trailing_stop: -.03,
  trailing_take_profit_activate: .08, trailing_take_profit_drawdown: .02,
  definition_defaults: defaults }
afterEach(async () => {
  await act(async () => root?.unmount())
  host?.remove()
  client?.clear()
  vi.clearAllMocks()
})
async function render(id = 'a', onClose = vi.fn()) {
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await update(id, onClose)
}
async function update(id: string, onClose = vi.fn()) {
  await act(async () => root.render(<QueryClientProvider client={client}>
    <StrategySettingsDialog strategyId={id} onClose={onClose} />
  </QueryClientProvider>))
}
const input = (label: string) => document.querySelector<HTMLInputElement>(`input[aria-label="${label}"]`)!
const click = async (text: string) => act(async () => {
  [...document.querySelectorAll('button')].find(b => b.textContent?.trim() === text)!.click()
})

it('displays percentages once and saves every effective exit control unchanged', async () => {
  get.mockResolvedValue(detail)
  save.mockResolvedValue({ ok: true })
  await render()
  expect(input('固定止损').value).toBe('6')
  expect(input('固定止盈').value).toBe('12')
  expect(input('移动止损').value).toBe('3')
  expect(input('移动止盈启动').value).toBe('8')
  expect(input('移动止盈回撤').value).toBe('2')
  expect(input('总市值最小值')).toBeTruthy()
  expect(input('上市天数').value).toBe('30')
  await click('保存设置')
  expect(save).toHaveBeenCalledWith('a', expect.objectContaining({
    stop_loss: -.06, take_profit: .12, trailing_stop: -.03,
    trailing_take_profit_activate: .08, trailing_take_profit_drawdown: .02,
  }))
})

it('restoring strategy defaults only changes the draft and can be cancelled', async () => {
  get.mockResolvedValue(detail)
  const onClose = vi.fn()
  await render('a', onClose)
  await click('恢复策略原始值')
  expect(input('固定止损').value).toBe('8')
  expect(input('固定止盈').value).toBe('10')
  expect(reset).not.toHaveBeenCalled()
  expect(save).not.toHaveBeenCalled()
  await act(async () => document.querySelector<HTMLButtonElement>('button[aria-label="关闭"]')!.click())
  expect(onClose).toHaveBeenCalled()
  expect(save).not.toHaveBeenCalled()
})

it('ignores a stale strategy request after switching strategy', async () => {
  let resolveA!: (value: unknown) => void
  get.mockImplementation((id: string) => id === 'a'
    ? new Promise(resolve => { resolveA = resolve })
    : Promise.resolve({ ...detail, id: 'b', name: '策略 B', stop_loss: -.04 }))
  await render('a')
  await update('b')
  await act(async () => resolveA(detail))
  expect(document.querySelector('#strategy-settings-title')?.textContent).toBe('策略 B')
  expect(input('固定止损').value).toBe('4')
})
