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
    screenerStrategies: async () => ({ presets: [
      { id: 'example', name: '示例策略', source: 'builtin', timeframes: ['1d'] },
      { id: 's01', name: 's01隔夜', source: 'builtin', timeframes: ['1m'] },
    ] }),
    strategyGet: async (id: string) => ({
      id, name: id === 's01' ? 's01隔夜' : '示例策略', source: 'builtin', timeframes: id === 'example' ? ['1d'] : ['1m'], asset_types: ['stock'],
      params: [], params_defaults: {}, basic_filter: {}, entry_signals: [], exit_signals: [], scoring: {},
      ...(id === 's01' ? { execution_backend: 'minute_filter', minute_execution: 'overnight_capacity_v1' } : {}),
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

it('shows the overnight capital and fee contract and disables independent full mode', async () => {
  await click('全量模拟')
  const picker = Array.from(host.querySelectorAll('button')).find(el => el.textContent?.startsWith('s01隔夜'))!
  await act(async () => picker.click())
  await settle()
  const summary = host.querySelector('[data-testid="overnight-execution"]')!
  expect(summary.textContent).toContain('14:47～14:49')
  expect(summary.textContent).toContain('现金100,000元')
  expect(summary.textContent).toContain('分钟参与率10%')
  expect(summary.textContent).toContain('最晚13:30；午休不成交')
  expect(Array.from(host.querySelectorAll('label')).some(el => el.textContent === '佣金 ‱')).toBe(false)
  const full = Array.from(host.querySelectorAll('button')).find(el => el.textContent?.includes('全量模拟'))!
  expect(full.disabled).toBe(true)
  const position = Array.from(host.querySelectorAll('button')).find(el => el.textContent === '仓位模拟')!
  expect(position.className).toContain('bg-accent')
  expect(host.textContent).toContain('需前两个交易日日线')
})

it('retains and exports actual minute fills when the sell deadline fails', async () => {
  const task = completed(3)
  task.result!.error = '11:29到期剩余100股'
  task.result!.stats = { minute_execution: 'overnight_capacity_v1', execution: { cash: 80000, deadline_violations: 1 } }
  task.result!.fills = [{
    date: '2026-10-01', symbol: '600000.SH', side: 'buy', bar: 887, datetime: '2026-10-01 14:47:00',
    shares: 100, price: 10, notional: 1000, fee: 5.01, minute_volume_shares: 2000, participation: .05,
  }]
  task.result!.signals = [{ date: '2026-10-02', symbol: '600000.SH', reason: 'time_guard', signal_bar: 682 }]
  state.task = task
  await render()
  expect(host.textContent).toContain('11:29到期剩余100股')
  expect(host.textContent).toContain('分钟成交分片（1行）')
  const create = vi.fn((_blob: Blob) => 'blob:minute-ledger')
  const revoke = vi.fn()
  Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: create })
  Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: revoke })
  let downloaded = ''
  const anchor = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
    downloaded = this.download
  })
  try {
    await click('下载完整成交与信号')
    expect(create).toHaveBeenCalledOnce()
    expect(create.mock.calls[0][0]).toBeInstanceOf(Blob)
    expect(downloaded).toBe('minute-execution-run-3.json')
    expect(revoke).toHaveBeenCalledWith('blob:minute-ledger')
  } finally {
    anchor.mockRestore()
  }
})
