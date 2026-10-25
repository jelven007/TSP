// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { StrategyBacktestResult } from '@/lib/api'
import type { BacktestTask } from '@/lib/backtestTask'
import { storage } from '@/lib/storage'
import { confirmAccount } from '@/lib/accountSession'
import { StrategyBacktest } from './StrategyBacktest'

const state = vi.hoisted(() => ({ task: null as BacktestTask | null }))
vi.mock('@/lib/backtestTask', () => ({
  useBacktestTask: () => state.task, tryReconnect: () => false,
  startBacktest: vi.fn(), stopBacktest: vi.fn(), attachBacktestJob: vi.fn(),
}))
vi.mock('@/lib/useSharedQueries', () => ({
  useDataStatus: () => ({ data: { enriched: { earliest_date: '2026-01-01' } }, isSuccess: true }),
  useCapabilities: () => ({ data: { capabilities: {} } }),
}))
vi.mock('@/lib/useCustomSignalNames', () => ({ useCustomSignalNames: () => ({}) }))
vi.mock('@/components/Toast', () => ({ toast: vi.fn() }))
vi.mock('./charts/StrategyNavChart', () => ({ StrategyNavChart: () => null }))
vi.mock('./charts/ReturnDistributionChart', () => ({ ReturnDistributionChart: () => null }))
vi.mock('./components/TradeKlineModal', () => ({ TradeKlineModal: () => null }))
vi.mock('./components/PicksSymbolKlineModal', () => ({ PicksSymbolKlineModal: () => null }))
vi.mock('./components/ResearchInputSummary', () => ({ ResearchInputSummary: () => null }))
vi.mock('@/lib/api', async importOriginal => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, api: {
    ...actual.api,
    screenerStrategies: async () => ({ presets: [{ id: 'example', name: '示例策略', source: 'builtin', timeframes: ['1d'] }] }),
    strategyGet: async () => ({
      id: 'example', name: '示例策略', source: 'builtin', timeframes: ['1d'], asset_types: ['stock'],
      params: [], params_defaults: {}, basic_filter: {}, entry_signals: [], exit_signals: [], scoring: {},
    }),
    factorColumns: async () => ({ columns: [] }),
    factorLibrary: async () => ({ factors: [] }),
    watchlistList: async () => ({ symbols: [] }),
    dataStatus: async () => ({ enriched: { earliest_date: '2026-01-01' } }),
  } }
})

let host: HTMLDivElement
let root: Root
let client: QueryClient
async function settle() {
  for (let i = 0; i < 5; i++) await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
}
async function render() {
  await act(async () => root.render(
    <MemoryRouter><QueryClientProvider client={client}><StrategyBacktest /></QueryClientProvider></MemoryRouter>,
  ))
  await settle()
}
beforeEach(async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  confirmAccount({
    auth_required: false, configured: false, has_users: false, legacy_migration_required: false,
    registration_enabled: false, email_verification_required: false, authenticated: true, user: null,
  })
  state.task = null
  localStorage.clear()
  storage.strategyBacktestLast.set({
    selectedStrategy: 'example', symbols: '', assetType: 'stock', fees: '2', start: '2026-10-01', end: '2026-10-20',
    matching: 'open_t+1', entryFill: 'open_t+1', exitFill: 'open_t+1', slippage: '5',
    maxPositions: '10', maxExposure: '100', initialCapital: '1000000', positionSizing: 'equal',
    mode: 'position', holdingDays: '5', result: null,
  })
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await render()
})
afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
  localStorage.clear()
})
async function click(text: string) {
  const button = Array.from(host.querySelectorAll('button')).find(el => el.textContent?.trim() === text)!
  expect(button).toBeDefined()
  await act(async () => button.click())
  await settle()
}
function completed(id: number): BacktestTask {
  const result: StrategyBacktestResult = {
    run_id: `run-${id}`, elapsed_ms: 1, config: { start: '2026-10-01', end: '2026-10-20' },
    stats: {}, equity_curve: [], drawdown_curve: [], benchmark_curve: [], per_symbol_stats: [],
    trades: Array.from({ length: 12 }, (_, index) => ({
      symbol: '600000.SH', name: '浦发银行', entry_date: `2026-10-${String(index + 1).padStart(2, '0')}`,
      exit_date: `2026-10-${String(index + 2).padStart(2, '0')}`, entry_price: 10, exit_price: 11,
      pnl_pct: 0.1, pnl_amount: 100, quantity: 100, entry_value: 1000, exit_value: 1100,
      duration: 1, exit_reason: 'signal',
    })),
    strategy_info: {
      id: 'example', name: '示例策略', description: '', entry_signals: [], exit_signals: [],
      stop_loss: null, take_profit: null, trailing_stop: null, trailing_take_profit_activate: null,
      trailing_take_profit_drawdown: null, score_min: null, score_max: null, max_hold_days: null, source: 'builtin',
    },
    error: null,
    provenance: {
      schema_version: 1, captured_at: '', dataset_release_id: null, dataset_release_created_at: null,
      dataset_release_reason: null, asset_type: 'stock', data_generation: null, release_generation: null,
      release_consistent: true, providers: {}, config_hash: '', strategy_hash: null, release_error: null,
    },
  }
  return { id, isPending: false, result, progress: null, error: null, reconnecting: false }
}

it('keeps the selected result page when editing the next run, and applies a new completed task', async () => {
  state.task = completed(1)
  await render()
  await click('交易明细 (12)')
  await click('下一页')
  const before = host.textContent!
  const feeLabel = Array.from(host.querySelectorAll('label')).find(el => el.textContent === '佣金 ‱')!
  const input = feeLabel.parentElement!.querySelector('input')!
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, '3')
    input.dispatchEvent(new Event('input', { bubbles: true }))
  })
  await settle()
  expect(host.textContent).toBe(before)
  expect(input.value).toBe('3')
  expect(storage.strategyBacktestLast.get(null)?.fees).toBe('2')
  state.task = completed(2)
  await render()
  expect(storage.strategyBacktestLast.get(null)).toMatchObject({ fees: '3', result: { run_id: 'run-2' } })
  const dailyTab = Array.from(host.querySelectorAll('button')).find(el => el.textContent?.startsWith('每日交易'))
  expect(dailyTab?.className).toContain('border-accent')
})
