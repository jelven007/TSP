// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api, type StrategyBacktestResult, type StrategyDetail } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { startBacktest, type BacktestTask } from '@/lib/backtestTask'
import { storage } from '@/lib/storage'
import { confirmAccount } from '@/lib/accountSession'
import { StrategyBacktest } from './StrategyBacktest'

const state = vi.hoisted(() => ({ task: null as BacktestTask | null, loadedViews: [] as string[] }))
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
vi.mock('./charts/StrategyNavChart', () => { state.loadedViews.push('nav'); return { StrategyNavChart: () => null } })
vi.mock('./charts/ReturnDistributionChart', () => { state.loadedViews.push('distribution'); return { ReturnDistributionChart: () => null } })
vi.mock('./components/TradeKlineModal', () => { state.loadedViews.push('trade'); return { TradeKlineModal: () => null } })
vi.mock('./components/PicksSymbolKlineModal', () => { state.loadedViews.push('picks'); return { PicksSymbolKlineModal: () => null } })
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
    customSignalsList: async () => ({ signals: [] }),
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
async function render(initializationDefaults?: import('@/lib/api').BusinessInitializationDefaults['backtest']) {
  await act(async () => root.render(
    <MemoryRouter><QueryClientProvider client={client}><StrategyBacktest initializationDefaults={initializationDefaults} /></QueryClientProvider></MemoryRouter>,
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
  vi.restoreAllMocks()
  client.clear()
  host.remove()
  localStorage.clear()
})
async function remountWithStrategy(id: string, cachedCatalog?: Awaited<ReturnType<typeof api.screenerStrategies>>) {
  await act(async () => root.unmount())
  client.clear()
  if (cachedCatalog) client.setQueryData(QK.screenerStrategies('stock', 'all'), cachedCatalog)
  storage.strategyBacktestLast.set({
    ...storage.strategyBacktestLast.get(null)!,
    selectedStrategy: id, params: { old: 1 }, overrides: { old: true },
    strategyConfigSignature: 'deleted-definition', result: { run_id: 'old-run' },
  })
  root = createRoot(host)
  await render()
}
function strategyFixture(id = 'example', source: StrategyDetail['source'] = 'builtin'): StrategyDetail {
  return {
    id, name: '示例策略', description: '', tags: [], source, execution_backend: 'polars_expr',
    asset_types: ['stock'], timeframes: ['1d'], version: '1', basic_filter: {}, params: [],
    params_defaults: {}, scoring: {}, scoring_directions: {}, entry_signals: [], exit_signals: [],
    minute_exit_trigger_supported_signals: [], stop_loss: null, take_profit: null, trailing_stop: null,
    trailing_take_profit_activate: null, trailing_take_profit_drawdown: null, max_hold_days: null,
    order_by: '', descending: false, limit: 0,
  }
}

it.each(['polars_expr', 'minute_filter'] as const)(
  'sends and saves separate participation for any custom strategy (%s)', async backend => {
    const custom = {
      ...strategyFixture('custom_unrelated', 'custom'), execution_backend: backend,
      timeframes: backend === 'minute_filter' ? ['1m'] : ['1d'],
    }
    vi.spyOn(api, 'screenerStrategies').mockResolvedValue({ presets: [custom], load_errors: [] })
    vi.spyOn(api, 'strategyGet').mockResolvedValue(custom)
    await remountWithStrategy(custom.id)
    const entry = host.querySelector<HTMLInputElement>('[aria-label="建仓分钟参与率 (%)"]')!
    const exit = host.querySelector<HTMLInputElement>('[aria-label="清仓分钟参与率 (%)"]')!
    expect(entry).not.toBeNull()
    expect(exit).not.toBeNull()
    expect(entry.value).toBe('10')
    expect(exit.value).toBe('10')
    const setValue = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!
    await act(async () => {
      setValue.call(entry, '20')
      entry.dispatchEvent(new Event('input', { bubbles: true }))
      setValue.call(exit, '5')
      exit.dispatchEvent(new Event('input', { bubbles: true }))
    })
    await click('运行回测')
    expect(startBacktest).toHaveBeenLastCalledWith(expect.objectContaining({
      strategy_id: custom.id, entry_participation_pct: .2, exit_participation_pct: .05,
      minute_price_rule: 'high_low_v1',
    }))
    state.task = completed(4)
    await render()
    expect(storage.strategyBacktestLast.get(null)).toMatchObject({
      entryParticipation: '20', exitParticipation: '5',
    })
  },
)

it.each([false, true])('restores frozen participation from a history task in a fresh form (failed=%s)', async failed => {
  const custom = strategyFixture('custom_unrelated', 'custom')
  vi.spyOn(api, 'screenerStrategies').mockResolvedValue({ presets: [custom], load_errors: [] })
  vi.spyOn(api, 'strategyGet').mockResolvedValue(custom)
  await remountWithStrategy(custom.id)
  expect(host.querySelector<HTMLInputElement>('[aria-label="建仓分钟参与率 (%)"]')!.value).toBe('10')
  const task = completed(41)
  task.result!.config.entry_participation_pct = .2
  task.result!.config.exit_participation_pct = .05
  task.result!.strategy_info.source = 'custom'
  if (failed) {
    task.error = '清仓截止后仍有余股'
    task.result!.error = task.error
  }
  state.task = task
  await render()
  expect(host.querySelector<HTMLInputElement>('[aria-label="建仓分钟参与率 (%)"]')!.value).toBe('20')
  expect(host.querySelector<HTMLInputElement>('[aria-label="清仓分钟参与率 (%)"]')!.value).toBe('5')
  expect(storage.strategyBacktestLast.get(null)).toMatchObject({
    entryParticipation: '20', exitParticipation: '5',
  })
})

it.each([false, true])('uses server defaults only when browser parameters are absent (saved=%s)', async saved => {
  await act(async () => root.unmount())
  client.clear()
  if (saved) {
    storage.strategyBacktestLast.set({
      ...storage.strategyBacktestLast.get(null)!,
      defaultsVersion: 5, fees: '3', initialCapital: '1000000', minimumCommission: '8',
    })
  } else {
    storage.strategyBacktestLast.remove()
  }
  root = createRoot(host)
  await render({
    last: { defaultsVersion: 5, selectedStrategy: 'example', fees: '7',
            initialCapital: '1000000', minimumCommission: '6' },
    quick_ranges: [{ id: 'range-1', enabled: true, unit: 'month', value: 2 }],
  })
  const feeLabel = Array.from(host.querySelectorAll('label')).find(el => el.textContent === '佣金 ‱')!
  expect(feeLabel.parentElement!.querySelector('input')!.value).toBe(saved ? '3' : '7')
  expect(host.textContent).toContain('2个月')
  expect(startBacktest).not.toHaveBeenCalled()
})

it.each(['custom_mvxxcs01', 'custom_mvxxcp88', 'custom_mvxxcs03', 'custom_any_name'])(
  'uses common saved-signal execution descriptions for any custom minute strategy (%s)', async id => {
    const custom: StrategyDetail = {
      ...strategyFixture(id, 'custom'), name: id, execution_backend: 'minute_filter',
      timeframes: ['1m'], entry_signals: ['csgi_late'], exit_signals: ['csgi_morning'],
      ...(id === 'custom_mvxxcs01' ? { minute_execution: 'overnight_capacity_v1', optional_triggers: true } : {}),
    }
    vi.spyOn(api, 'screenerStrategies').mockResolvedValue({ presets: [custom], load_errors: [] })
    vi.spyOn(api, 'strategyGet').mockResolvedValue(custom)
    vi.spyOn(api, 'customSignalsList').mockResolvedValue({ signals: [
      { id: 'late', name: '当前尾盘', kind: 'entry', enabled: true, timeframe: 'intraday',
        template: 'late_session_strength', conditions: [], params: {
          time: '14:50:00', evaluation_delay_minutes: .5, end_time: '15:00:00',
          change_min_pct: 3, change_max_pct: 5, pool_up_min_pct: 60,
          ma5_1m_count: null, ma5_5m_count: null,
        } },
      { id: 'morning', name: '当前早盘', kind: 'exit', enabled: true, timeframe: 'intraday',
        template: 'morning_liquidation', conditions: [], params: {
          start_time: '11:00:00', time: '11:25:00', fallback_time: '11:30:00',
          rise_pct: null, fall_pct: null, decline_3m_pct: .2, volume_ratio_min: 1.5,
        } },
    ] })
    await remountWithStrategy(id)
    expect(host.querySelector('[aria-label="建仓口径"]')?.textContent).toContain('尾盘走强信号触发时间 → 结束时间')
    expect(host.querySelector('[aria-label="清仓口径"]')?.textContent).toContain('早盘清仓信号触发时间 → 兜底时间')
    expect(host.textContent).toContain('14:50:30')
    expect(host.textContent).toContain('3分钟跌幅 ≥ 0.2%')
    expect(host.textContent).not.toContain('缺分区的日子自动跳过')
    await act(async () => [...host.querySelectorAll<HTMLButtonElement>('button')]
      .find(button => button.textContent?.includes('策略设置') && button.textContent?.includes('编辑'))!.click())
    await settle()
    const dialog = document.querySelector('[role="dialog"]')!
    expect(dialog.textContent).toContain('11:25:00')
    expect(dialog.textContent).toContain('3分钟跌幅 ≥ 0.2%')
    expect(dialog.textContent).not.toContain('现价相对原始昨收达到涨幅或跌幅阈值')
    expect(startBacktest).not.toHaveBeenCalled()
  },
)

it('does not load result charts or replay dialogs on an empty backtest entry', () => {
  expect(state.loadedViews).toEqual([])
  expect(startBacktest).not.toHaveBeenCalled()
})

it('shows current S03 scoring and supported morning risk configuration', async () => {
  const custom: StrategyDetail = {
    ...strategyFixture('custom_mvxxcs03', 'custom'), name: 's03', scoring_mode: 'strategy',
    execution_backend: 'minute_filter', timeframes: ['1m'], exit_signals: ['csgi_morning'],
  }
  vi.spyOn(api, 'screenerStrategies').mockResolvedValue({ presets: [custom], load_errors: [] })
  vi.spyOn(api, 'strategyGet').mockResolvedValue(custom)
  vi.spyOn(api, 'customSignalsList').mockResolvedValue({ signals: [{
    id: 'morning', name: '早盘清仓', kind: 'exit', enabled: true, timeframe: 'intraday',
    template: 'morning_liquidation', conditions: [], params: { time: '10:30:00', rise_pct: 6 },
  }] })
  await remountWithStrategy(custom.id)
  await act(async () => [...host.querySelectorAll<HTMLButtonElement>('button')]
    .find(button => button.textContent?.includes('策略设置') && button.textContent?.includes('编辑'))!.click())
  await settle()
  await act(async () => [...document.querySelectorAll<HTMLButtonElement>('[role="dialog"] button')]
    .find(button => button.textContent === '评分权重')!.click())
  expect(document.querySelector('[role="dialog"]')!.textContent)
    .toContain('技术40%、资金20%、情绪20%、基本面10%、消息10%')
  await act(async () => [...document.querySelectorAll<HTMLButtonElement>('[role="dialog"] button')]
    .find(button => button.textContent === '出场触发器')!.click())
  expect(document.querySelector('[role="dialog"]')!.textContent).toContain('支持环境过滤、固定/移动风控及最长持有')
})

it('keeps common custom minute controls while signal definitions are unavailable', async () => {
  const custom: StrategyDetail = {
    ...strategyFixture('custom_s03', 'custom'), execution_backend: 'minute_filter',
    timeframes: ['1m'], entry_signals: ['csgi_late_s03'], exit_signals: ['csgi_exit_s03'],
  }
  vi.spyOn(api, 'screenerStrategies').mockResolvedValue({ presets: [custom], load_errors: [] })
  vi.spyOn(api, 'strategyGet').mockResolvedValue(custom)
  vi.spyOn(api, 'customSignalsList').mockResolvedValue({ signals: [] })
  await remountWithStrategy(custom.id)
  expect(host.querySelector('[aria-label="建仓口径"]')).not.toBeNull()
  expect(host.querySelector('[aria-label="清仓口径"]')).not.toBeNull()
  expect(host.textContent).toContain('信号定义尚未确认')
})

it('clears stale generic weights when the strategy owns ranking', async () => {
  const custom: StrategyDetail = {
    ...strategyFixture('custom_s02', 'custom'), name: 's02', scoring_mode: 'strategy',
    execution_backend: 'minute_filter', timeframes: ['1m'],
  }
  vi.spyOn(api, 'screenerStrategies').mockResolvedValue({ presets: [custom], load_errors: [] })
  vi.spyOn(api, 'strategyGet').mockResolvedValue(custom)
  const save = vi.spyOn(api, 'strategySaveConfig').mockResolvedValue({ ok: true })
  await act(async () => root.unmount())
  client.clear()
  storage.strategyBacktestLast.set({
    ...storage.strategyBacktestLast.get(null)!, selectedStrategy: custom.id,
    overrides: { scoring: { change_pct: 1 }, scoring_directions: { change_pct: 'high' }, scoring_replace: true },
  })
  root = createRoot(host)
  await render()
  await act(async () => [...host.querySelectorAll<HTMLButtonElement>('button')]
    .find(button => button.textContent?.includes('策略设置') && button.textContent?.includes('编辑'))!.click())
  await act(async () => [...document.querySelectorAll<HTMLButtonElement>('[role="dialog"] button')]
    .find(button => button.textContent?.trim() === '评分权重')!.click())
  expect(document.querySelector('[role="dialog"]')?.textContent).toContain('排名由策略生成')
  expect(document.querySelector('[role="dialog"]')?.textContent).not.toContain('添加评分因子')
  await act(async () => [...document.querySelectorAll<HTMLButtonElement>('[role="dialog"] button')]
    .find(button => button.textContent?.trim() === '应用到策略')!.click())
  expect(save).toHaveBeenCalledWith(custom.id, expect.objectContaining({
    scoring: {}, scoring_directions: {}, scoring_replace: true,
  }))
  expect(startBacktest).not.toHaveBeenCalled()
})

it('enables a saved custom minute holding exit with minute liquidation and position mode', async () => {
  const custom: StrategyDetail = {
    ...strategyFixture('custom_s02', 'custom'), name: 's02', execution_backend: 'minute_filter',
    timeframes: ['1m'], exit_signals: ['csgi_morning_liquidation'],
  }
  vi.spyOn(api, 'screenerStrategies').mockResolvedValue({ presets: [custom], load_errors: [] })
  vi.spyOn(api, 'strategyGet').mockResolvedValue(custom)
  vi.spyOn(api, 'customSignalsList').mockResolvedValue({ signals: [{
    id: 'morning_liquidation', name: '早盘清仓', kind: 'exit', enabled: true,
    timeframe: 'intraday', template: 'morning_liquidation', conditions: [],
  }] })
  const save = vi.spyOn(api, 'strategySaveConfig')
  await remountWithStrategy('custom_s02')
  await act(async () => [...host.querySelectorAll<HTMLButtonElement>('button')]
    .find(button => button.textContent?.includes('策略设置') && button.textContent?.includes('编辑'))!.click())
  await act(async () => [...document.querySelectorAll<HTMLButtonElement>('[role="dialog"] button')]
    .find(button => button.textContent?.trim() === '出场触发器')!.click())
  await settle()
  const signal = [...document.querySelectorAll<HTMLButtonElement>('[role="dialog"] button')]
    .find(button => button.textContent?.includes('早盘清仓'))
  expect(signal).toBeDefined()
  expect(signal?.textContent).not.toContain('回测暂不支持')
  expect(signal?.disabled).toBe(false)
  expect(host.textContent).toContain('早盘清仓信号触发时间 → 兜底时间')
  expect([...host.querySelectorAll<HTMLButtonElement>('button')]
    .find(button => button.textContent?.trim() === '全量模拟')?.disabled).toBe(true)
  expect(save).not.toHaveBeenCalled()
})

it.each([false, true])('validates a saved deleted strategy before requesting detail (empty catalog: %s)', async empty => {
  let resolveCatalog!: (data: Awaited<ReturnType<typeof api.screenerStrategies>>) => void
  vi.spyOn(api, 'screenerStrategies').mockImplementation(() => new Promise(resolve => { resolveCatalog = resolve }))
  const detail = vi.spyOn(api, 'strategyGet')
  await remountWithStrategy('custom_mvxznhd9', { presets: [strategyFixture('custom_mvxznhd9', 'custom')], load_errors: [] })
  expect(detail).not.toHaveBeenCalled()
  expect(storage.strategyBacktestLast.get(null)?.selectedStrategy).toBe('custom_mvxznhd9')
  expect(Array.from(host.querySelectorAll('button')).find(el => el.textContent?.trim() === '运行回测')?.disabled).toBe(true)
  await act(async () => resolveCatalog({ presets: empty ? [] : [strategyFixture()], load_errors: [] }))
  await settle()
  expect(detail).not.toHaveBeenCalled()
  expect(storage.strategyBacktestLast.get(null)).toMatchObject({
    selectedStrategy: null, params: {}, overrides: {}, result: null,
    fees: '2', start: '2026-10-01', end: '2026-10-20',
  })
  expect(storage.strategyBacktestLast.get(null)?.strategyConfigSignature).toBeUndefined()
})

it('retains the saved strategy when the catalog request fails without requesting its detail', async () => {
  vi.spyOn(api, 'screenerStrategies').mockRejectedValue(new Error('catalog unavailable'))
  const detail = vi.spyOn(api, 'strategyGet')
  await remountWithStrategy('custom_mvxznhd9')
  expect(client.getQueryState(QK.screenerStrategies('stock', 'all'))?.status).toBe('error')
  expect(detail).not.toHaveBeenCalled()
  expect(storage.strategyBacktestLast.get(null)).toMatchObject({
    selectedStrategy: 'custom_mvxznhd9', params: { old: 1 }, overrides: { old: true },
  })
})

it('loads a valid saved strategy only after the catalog confirms it', async () => {
  let resolveCatalog!: (data: Awaited<ReturnType<typeof api.screenerStrategies>>) => void
  vi.spyOn(api, 'screenerStrategies').mockImplementation(() => new Promise(resolve => { resolveCatalog = resolve }))
  const detail = vi.spyOn(api, 'strategyGet')
  await remountWithStrategy('example')
  expect(detail).not.toHaveBeenCalled()
  await act(async () => resolveCatalog({ presets: [strategyFixture()], load_errors: [] }))
  await settle()
  expect(detail).toHaveBeenCalledExactlyOnceWith('example')
  expect(host.textContent).toContain('示例策略')
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

it.each(['custom_mvxxcs01', 'custom_mvxxcs03'])('fills missing result names in daily and trade views (%s)', async id => {
  const lookup = vi.spyOn(api, 'instrumentNames').mockResolvedValue({ names: { '600000.SH': '浦发银行' } })
  const task = completed(10)
  task.result!.strategy_info.id = id
  task.result!.trades.forEach(trade => { trade.name = '' })
  state.task = task
  await render()
  expect(lookup).toHaveBeenCalledExactlyOnceWith(['600000.SH'])
  expect(host.textContent).toContain('浦发银行')
  const tab = Array.from(host.querySelectorAll('button')).find(button => button.textContent?.startsWith('交易明细'))!
  await act(async () => tab.click())
  await settle()
  expect(host.querySelector('tbody')?.textContent).toContain('浦发银行')
  expect(task.result!.trades.every(trade => trade.name === '')).toBe(true)
})

it('preserves frozen names and skips directory lookup for named results', async () => {
  const lookup = vi.spyOn(api, 'instrumentNames').mockResolvedValue({ names: { '600000.SH': '新名称' } })
  state.task = completed(11)
  await render()
  expect(host.textContent).toContain('浦发银行')
  expect(lookup).not.toHaveBeenCalled()
})

it('keeps fresh liquidation defaults while S01 detail loads and submits the five-minute exit', async () => {
  const s01: StrategyDetail = {
    ...strategyFixture('custom_mvxxcs01', 'custom'), name: 's01', timeframes: ['1m'],
    execution_backend: 'minute_filter', minute_execution: 'overnight_capacity_v1', optional_triggers: true,
  }
  vi.spyOn(api, 'screenerStrategies').mockResolvedValue({ presets: [s01], load_errors: [] })
  let resolveDetail!: (detail: StrategyDetail) => void
  vi.spyOn(api, 'strategyGet').mockImplementation(() => new Promise(resolve => { resolveDetail = resolve }))
  await act(async () => root.unmount())
  client.clear()
  localStorage.clear()
  root = createRoot(host)
  await render()
  const exit = () => Array.from(host.querySelectorAll('label')).find(el => el.textContent === '清仓口径')!
    .parentElement!.parentElement!.querySelector('select')!
  expect(exit().value).toBe('signal_next_minute')
  await act(async () => Array.from(host.querySelectorAll('button'))
    .find(el => el.textContent?.startsWith('s01'))!.click())
  await settle()
  expect(exit().value).toBe('signal_next_minute')
  expect(Array.from(host.querySelectorAll('button')).find(el => el.textContent === '运行回测')?.disabled).toBe(true)
  await act(async () => resolveDetail(s01))
  await settle()
  expect(host.querySelector('[aria-label="清仓口径"]')?.textContent).toBe('未选择出场触发器')
  await click('运行回测')
  expect(startBacktest).toHaveBeenCalledWith(expect.objectContaining({
    strategy_id: s01.id, exit_fill: 'signal_next_minute', max_positions: 4, initial_capital: 1000000,
  }))
})

it.each([
  ['custom_mvxxcs01', 'close_t'],
  ['custom_mvxxcs01', 'open_t+1'],
  ['custom_mvxxcp88', 'close_t'],
  ['custom_mvxxcp88', 'open_t+1'],
  ['custom_mvxxcp88', 'signal_next_minute'],
])('fixes S01/S02 matching controls and new requests despite legacy settings (%s, %s)', async (id, legacyExit) => {
  const custom: StrategyDetail = {
    ...strategyFixture(id, 'custom'), name: id === 'custom_mvxxcs01' ? 's01' : 's02',
    execution_backend: 'minute_filter', timeframes: ['1m'],
    ...(id === 'custom_mvxxcs01' ? { minute_execution: 'overnight_capacity_v1', optional_triggers: true } : {}),
    exit_signals: ['csgi_morning_liquidation'],
  }
  vi.spyOn(api, 'screenerStrategies').mockResolvedValue({ presets: [custom], load_errors: [] })
  vi.spyOn(api, 'strategyGet').mockResolvedValue(custom)
  vi.spyOn(api, 'customSignalsList').mockResolvedValue({ signals: [{
    id: 'morning_liquidation', name: '早盘清仓', kind: 'exit', enabled: true,
    timeframe: 'intraday', template: 'morning_liquidation', conditions: [],
  }] })
  storage.strategyBacktestLast.set({
    ...storage.strategyBacktestLast.get(null)!, entryFill: 'open_t+1',
    exitFill: legacyExit as 'close_t' | 'open_t+1' | 'signal_next_minute',
  })
  await remountWithStrategy(id)
  const entry = host.querySelector('[aria-label="建仓口径"]')!
  const exit = host.querySelector('[aria-label="清仓口径"]')!
  expect(entry).not.toBeNull()
  expect(exit).not.toBeNull()
  expect(entry.textContent).toBe('未选择入场触发器')
  expect(exit.textContent).toBe('早盘清仓信号触发时间 → 兜底时间')
  expect(entry.className).toBe(exit.className)
  expect(entry.parentElement?.className).toBe(exit.parentElement?.className)
  expect(entry.querySelector('select')).toBeNull()
  expect(exit.querySelector('select')).toBeNull()
  expect(host.textContent).not.toContain('次交易日触发分钟收盘')
  await click('运行回测')
  expect(startBacktest).toHaveBeenLastCalledWith(expect.objectContaining({
    strategy_id: id, entry_fill: 'close_t', exit_fill: 'signal_next_minute', initial_capital: 1000000,
    minute_price_rule: 'high_low_v1',
    minute_fill: false, mode: 'position',
  }))
  state.task = completed(4)
  await render()
  expect(storage.strategyBacktestLast.get(null)).toMatchObject({
    entryFill: 'close_t', exitFill: 'signal_next_minute',
  })
})

it('falls back only after a strategy is confirmed to lack minute liquidation support', async () => {
  await act(async () => root.unmount())
  client.clear()
  localStorage.clear()
  root = createRoot(host)
  await render()
  await click('示例策略')
  await click('运行回测')
  expect(startBacktest).toHaveBeenCalledWith(expect.objectContaining({ exit_fill: 'close_t' }))
})

it('uses the requested migrated defaults and request units', async () => {
  await click('运行回测')

  expect(startBacktest).toHaveBeenCalledWith(expect.objectContaining({
    max_positions: 4,
    stamp_tax_pct: 0.001,
    slippage_bps: 10,
    minimum_commission: 5,
    transfer_fee_pct: 0.0005,
  }))
})

it('describes the daily five-minute entry capacity window', () => {
  expect(host.textContent).toContain('次日开盘起5分钟容量撮合')
})

it('describes the minute strategy post-confirmation entry capacity window', async () => {
  const minute = {
    ...strategyFixture('minute-example', 'custom'),
    execution_backend: 'minute_filter' as const,
    timeframes: ['1m'],
  }
  vi.spyOn(api, 'screenerStrategies').mockResolvedValue({ presets: [minute], load_errors: [] })
  vi.spyOn(api, 'strategyGet').mockResolvedValue(minute)

  await remountWithStrategy('minute-example')

  expect(host.querySelector('[aria-label="建仓口径"]')?.textContent).toBe('未选择入场触发器')
  expect(host.textContent).toContain('按每分钟K线的最高价成交')
})

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

it('uses common replay controls and clears stale parameters for parameter-free optional S01', async () => {
  const custom: StrategyDetail = {
    ...strategyFixture('custom_mvxxcs01', 'custom'), name: 's01',
    execution_backend: 'minute_filter', timeframes: ['1m'],
    minute_execution: 'overnight_capacity_v1', optional_triggers: true,
    entry_allocation: 'equal_selected_v1',
  }
  vi.spyOn(api, 'screenerStrategies').mockResolvedValue({ presets: [custom], load_errors: [] })
  vi.spyOn(api, 'strategyGet').mockResolvedValue(custom)
  await remountWithStrategy(custom.id)
  const labels = Array.from(host.querySelectorAll('label')).map(el => el.textContent)
  expect(labels).toContain('初始资金')
  expect(labels).toContain('最大持仓数')
  expect(labels).toContain('佣金 ‱')
  expect(labels).toContain('单笔最低佣金（元）')
  expect(labels).toContain('建仓口径')
  expect(labels).toContain('清仓口径')
  expect(labels).toContain('买入权重')
  expect(labels).toContain('最大总仓位(%)')
  expect(host.querySelector('[data-testid="overnight-execution"]')).toBeNull()
  async function setField(label: string, value: string) {
    const field = Array.from(host.querySelectorAll('label')).find(el => el.textContent === label)!
      .parentElement!.querySelector('input,select') as HTMLInputElement | HTMLSelectElement
    await act(async () => {
      const prototype = field.tagName === 'SELECT' ? HTMLSelectElement.prototype : HTMLInputElement.prototype
      Object.getOwnPropertyDescriptor(prototype, 'value')!.set!.call(field, value)
      field.dispatchEvent(new Event(field.tagName === 'SELECT' ? 'change' : 'input', { bubbles: true }))
    })
    await settle()
  }
  await setField('最大持仓数', '5')
  await setField('最大总仓位(%)', '80')
  await setField('买入权重', 'score_weight')
  expect(host.querySelector('[aria-label="清仓口径"]')?.textContent).toBe('未选择出场触发器')
  await click('运行回测')
  expect(startBacktest).toHaveBeenCalledWith(expect.objectContaining({
    strategy_id: custom.id, initial_capital: 1000000, params: {}, max_positions: 5,
    max_exposure_pct: .8, position_sizing: 'score_weight', exit_fill: 'signal_next_minute',
    entry_fill: 'close_t',
  }))
})

it.each(['overnight_capacity_v1', 'generic_morning_liquidation_v1', 'generic_morning_liquidation_resume_v2'])('retains and exports actual minute fills when the sell deadline fails (%s)', async execution => {
  const task = completed(3)
  task.result!.error = '11:29到期剩余100股'
  task.result!.stats = { minute_execution: execution, execution: { cash: 80000, deadline_violations: 1 } }
  task.result!.fills = [{
    date: '2026-10-01', symbol: '600000.SH', side: 'buy', bar: 887, datetime: '2026-10-01 14:47:00',
    shares: 100, price: 10, notional: 1000, fee: 5.01, minute_volume_shares: 2000, participation: .05,
  }]
  task.result!.entry_orders = [{
    symbol: '600000.SH',
    signal_time: '2026-10-01 14:30:00',
    window_start: '2026-10-01 14:31:00',
    target_shares: 1000,
    filled_shares: 500,
    cancelled_shares: 500,
    average_price: 10,
    status: 'partial',
  }]
  task.result!.signals = [{ date: '2026-10-02', symbol: '600000.SH', reason: 'time_guard', signal_bar: 682 }]
  task.error = task.result!.error
  task.result!.stats.execution.remaining_positions = [{ symbol: '600000.SH', remaining: 100 }]
  state.task = task
  await render()
  expect(host.textContent).toContain('11:29到期剩余100股')
  expect(host.textContent).toContain('分钟成交分片（1行）')
  expect(host.textContent).toContain('目标 1,000 股')
  expect(host.textContent).toContain('成交 500 股')
  expect(host.textContent).toContain('撤单 500 股')
  expect(host.textContent).toContain('600000.SH 剩余100股')
  expect(host.textContent).not.toContain('下方仍展示上一次成功结果')
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
