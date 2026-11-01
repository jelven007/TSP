// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { Screener } from './Screener'

const { summary, detail, run, runAll, reload, batchMutate, oldPreferences,
  monitorList, monitorSave, monitorDelete, rules, poolState, dataState, reorderPool, addToPool } = vi.hoisted(() => ({
  summary: vi.fn(), detail: vi.fn(), run: vi.fn(), runAll: vi.fn(),
  reload: vi.fn(), batchMutate: vi.fn(),
  oldPreferences: { screener_auto_run: false as boolean | undefined },
  monitorList: vi.fn(), monitorSave: vi.fn(), monitorDelete: vi.fn(),
  rules: [] as any[], poolState: { ids: ['sample', 'minute'] },
  dataState: { latest: '2026-10-23' as string | null },
  reorderPool: vi.fn(), addToPool: vi.fn(),
}))
vi.mock('@/lib/api', () => ({
  genRuleId: () => 'monitor-rule',
  api: {
    screenerStrategies: async () => ({ presets: [
      { id: 'sample', name: '样例', asset_types: ['stock'], timeframes: ['1d'] },
      { id: 'minute', name: '分钟样例', asset_types: ['stock'], timeframes: ['1m'] },
      { id: 's02', name: 'S02', asset_types: ['stock'], timeframes: ['1m'] },
      { id: 'added', name: '新增样例', asset_types: ['stock'], timeframes: ['1d'] },
      { id: 'composite', name: '叠加样例', asset_types: ['stock'], timeframes: ['1d'] },
    ] }),
    screenerCachedSummary: summary,
    screenerCachedResult: detail,
    screenerRunPreset: run,
    screenerRunAll: runAll,
    strategyReload: reload,
    watchlistList: async () => ({ symbols: [] }),
    monitorRulesList: monitorList,
    monitorRuleSave: monitorSave,
    monitorRuleDelete: monitorDelete,
    minuteRefreshStatus: async () => ({}),
    watchlistGroups: async () => ({ groups: [] }),
    strategyGet: async () => ({ params_defaults: { position_limit: 5, take_profit_pct: .06, stop_loss_pct: .05 } }),
    lotBatches: async () => ({ batches: [], next_number: '2026110101' }),
  },
}))
vi.mock('@/lib/useSharedQueries', () => ({
  usePreferences: () => ({ data: oldPreferences }),
  useDataStatus: () => ({ data: { enriched: { latest_date: dataState.latest, earliest_date: '2026-09-01' } } }),
  useCapabilities: () => ({ data: {} }),
  useQuoteStatus: () => ({ data: {} }),
}))
vi.mock('@/lib/useSharedMutations', () => ({ useWatchlistBatchAdd: () => ({ mutate: batchMutate }) }))
vi.mock('@/components/screener/StrategyLotBatchDialog', () => ({
  StrategyLotBatchDialog: ({ rows }: any) => <div role="dialog" aria-label="策略新增批次">{JSON.stringify(rows)}</div>,
}))
vi.mock('@/lib/useStrategyPool', () => ({
  useStrategyPool: () => ({ pool: poolState.ids, prune: vi.fn(), reorderPool, addToPool }),
}))
vi.mock('@/lib/screener-columns', () => ({
  SCREENER_BUILTIN_COLUMNS: [], SCREENER_COLUMN_GROUPS: [],
  buildExtColumnsParam: () => '', loadScreenerColumnConfig: async () => [], saveScreenerColumnConfig: vi.fn(),
}))
vi.mock('@/components/DatePicker', () => ({
  DatePicker: ({ value, onChange }: any) => <input aria-label="测试日期" value={value} onChange={e => onChange(e.target.value)} />,
}))
vi.mock('@/components/screener/ScreenerTable', () => ({
  ScreenerTable: ({ rows }: any) => <div data-testid="rows">{JSON.stringify(rows)}</div>,
}))
vi.mock('@/components/Toast', () => ({ toast: vi.fn() }))
vi.mock('@/components/StockPreviewDialog', () => ({ StockPreviewDialog: () => null }))
vi.mock('@/components/screener/StrategyPoolDialog', () => ({
  StrategyPoolDialog: ({ onConfirm }: any) =>
    <button onClick={() => onConfirm([...poolState.ids, 'added'])}>加入新增样例</button>,
}))
vi.mock('@/components/screener/StrategyBuilderDialog', () => ({
  StrategyBuilderDialog: ({ open, onSavedId }: any) => open
    ? <button onClick={() => onSavedId('added', false)}>保存新策略</button> : null,
}))
vi.mock('@/components/screener/CompositeStrategyDialog', () => ({
  CompositeStrategyDialog: ({ open, onSavedId }: any) => open
    ? <button onClick={() => onSavedId('composite')}>保存叠加策略</button> : null,
}))

Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
let root: Root
let host: HTMLDivElement
let client: QueryClient

beforeEach(() => {
  rules.length = 0
  poolState.ids = ['sample', 'minute']
  dataState.latest = '2026-10-23'
  monitorList.mockReset().mockImplementation(async () => ({ rules: [...rules] }))
  monitorSave.mockReset().mockImplementation(async rule => {
    rules.push(rule)
    return { rule }
  })
  monitorDelete.mockReset().mockResolvedValue({})
  run.mockReset()
  runAll.mockReset()
})

afterEach(async () => {
  await act(async () => root?.unmount())
  client?.clear()
  host?.remove()
  vi.clearAllMocks()
  oldPreferences.screener_auto_run = false
  vi.useRealTimers()
})

async function flush() {
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 30)) })
}
async function render() {
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await act(async () => root.render(<QueryClientProvider client={client}><Screener /></QueryClientProvider>))
  await flush()
}
function button(text: string) {
  return [...host.querySelectorAll('button')].find(b => b.textContent?.trim().startsWith(text)
    || [...b.querySelectorAll('span')].some(span => span.textContent === text))!
}
function monitor(text: string) {
  return button(text).parentElement!.querySelector<HTMLButtonElement>(
    'button[title="开启策略监控"], button[title="取消策略监控"]')!
}
async function setDate(day: string) {
  await act(async () => {
    const input = host.querySelector<HTMLInputElement>('input[aria-label="测试日期"]')!
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, day)
    input.dispatchEvent(new Event('input', { bubbles: true }))
  })
  await flush()
}
async function waitForRows(day: string) {
  for (let attempt = 0; attempt < 20; attempt++) {
    if (host.querySelector('[data-testid="rows"]')?.textContent?.includes(day)) return
    await flush()
  }
  expect(host.querySelector('[data-testid="rows"]')?.textContent).toContain(day)
}

it('restores A after B from date-specific saved records without recalculating', async () => {
  summary.mockImplementation(async (day: string) => ({
    as_of: day, results: { sample: { total: 1, as_of: day } }, today_ever_counts: {},
  }))
  detail.mockImplementation(async (_sid: string, _ext: string, day: string) => ({
    result: { as_of: day, strategy: 'sample', rows: [{ symbol: day }], total: 1 },
    today_ever_rows: {}, strategy_ids_by_symbol: {},
  }))
  await render()
  await act(async () => button('样例').click())
  await flush()
  await setDate('2026-10-22')
  await waitForRows('2026-10-22')
  expect(host.querySelector('[data-testid="rows"]')?.textContent).toContain('2026-10-22')
  await setDate('2026-10-23')
  await waitForRows('2026-10-23')
  expect(host.querySelector('[data-testid="rows"]')?.textContent).toContain('2026-10-23')
  expect(run).not.toHaveBeenCalled()
  expect(summary).toHaveBeenCalledWith('2026-10-22', 'stock', 'all')
})

it('ignores a late run from the previous date and keeps the selected date', async () => {
  summary.mockImplementation(async (day: string) => ({ as_of: day, results: {}, today_ever_counts: {} }))
  let finish!: (value: any) => void
  run.mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
  await render()
  await act(async () => button('样例').click())
  await act(async () => monitor('样例').click())
  await flush()
  await setDate('2026-10-22')
  await act(async () => finish({
    as_of: '2026-10-23', rows: [{ symbol: 'LATE' }], strategy: 'sample', total: 1,
  }))
  await flush()
  expect(host.querySelector<HTMLInputElement>('input[aria-label="测试日期"]')!.value).toBe('2026-10-22')
  expect(host.textContent).not.toContain('LATE')
})

it.each([true, undefined])('does not run strategies on entry or date change with legacy auto-run %s', async value => {
  oldPreferences.screener_auto_run = value
  summary.mockImplementation(async (day: string) => ({ as_of: day, results: {}, today_ever_counts: {} }))
  runAll.mockResolvedValue({ as_of: '2026-10-23', results: {} })
  await render()
  await flush()
  await setDate('2026-10-22')
  expect(runAll).not.toHaveBeenCalled()
  expect(run).not.toHaveBeenCalled()
})

it.each([['样例', 'sample', '1d'], ['分钟样例', 'minute', '1m']])(
  'selects %s without executing and runs only after enabling monitoring', async (name, id, timeframe) => {
  oldPreferences.screener_auto_run = true
  summary.mockImplementation(async (day: string) => ({ as_of: day, results: {}, today_ever_counts: {} }))
  runAll.mockResolvedValue({ as_of: '2026-10-23', results: {} })
  run.mockResolvedValue({ as_of: '2026-10-23', rows: [], total: 0, strategy: id })
  await render()
  expect(runAll).not.toHaveBeenCalled()
  expect(run).not.toHaveBeenCalled()
  await act(async () => button(name).click())
  await flush()
  expect(run).not.toHaveBeenCalled()
  expect(runAll).not.toHaveBeenCalled()
  await act(async () => monitor(name).click())
  await flush()
  expect(monitorSave).toHaveBeenCalledWith(expect.objectContaining({ strategy_id: id, enabled: true }))
  expect(run).toHaveBeenCalledTimes(1)
  expect(run).toHaveBeenCalledWith(id, undefined, '2026-10-23', undefined, 'stock', timeframe)
})

it.each([
  ['2026-11-01T12:00:00+08:00', '2026-10-30'],
  ['2026-10-06T12:00:00+08:00', '2026-09-30'],
])('uses the server latest available trading date at monitoring start on %s', async (now, latest) => {
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(new Date(now))
  dataState.latest = latest
  summary.mockImplementation(async (day: string) => ({ as_of: day, results: {}, today_ever_counts: {} }))
  run.mockResolvedValue({ as_of: latest, strategy: 'sample', rows: [], total: 0 })
  await render()
  await setDate('2026-09-01')
  await act(async () => button('样例').click())
  expect(run).not.toHaveBeenCalled()
  await act(async () => monitor('样例').click())
  await flush()
  expect(host.querySelector<HTMLInputElement>('[aria-label="测试日期"]')!.value).toBe(latest)
  expect(run).toHaveBeenCalledWith('sample', undefined, latest, undefined, 'stock', '1d')
})

it('does not calculate on entry or historical date changes for an already monitored S02', async () => {
  poolState.ids.push('s02')
  rules.push({ id: 's02-rule', type: 'strategy', strategy_id: 's02', enabled: true, asset_type: 'stock' })
  summary.mockImplementation(async (day: string) => ({ as_of: day, results: {}, today_ever_counts: {} }))
  runAll.mockResolvedValue({ as_of: '2026-10-23', results: {} })
  await render()
  await setDate('2026-10-22')
  expect(run).not.toHaveBeenCalled()
  expect(runAll).not.toHaveBeenCalled()
})

it('waits for monitoring save and prevents double-click execution', async () => {
  summary.mockImplementation(async (day: string) => ({ as_of: day, results: {}, today_ever_counts: {} }))
  let finish!: (value: any) => void
  monitorSave.mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
  run.mockResolvedValue({ as_of: '2026-10-23', strategy: 'minute', rows: [], total: 0 })
  await render()
  await act(async () => { monitor('分钟样例').click(); monitor('分钟样例').click() })
  expect(monitorSave).toHaveBeenCalledTimes(1)
  expect(run).not.toHaveBeenCalled()
  await act(async () => finish({}))
  await flush()
  expect(run).toHaveBeenCalledTimes(1)
})

it('executes an explicitly enabled strategy even when a saved result exists', async () => {
  summary.mockImplementation(async (day: string) => ({
    as_of: day, results: { sample: { total: 1, as_of: day } }, today_ever_counts: {},
  }))
  detail.mockResolvedValue({ result: { as_of: '2026-10-23', strategy: 'sample',
    rows: [{ symbol: 'SAVED' }], total: 1 }, today_ever_rows: {}, strategy_ids_by_symbol: {} })
  run.mockResolvedValue({ as_of: '2026-10-23', strategy: 'sample',
    rows: [{ symbol: 'NEW' }], total: 1 })
  await render()
  await act(async () => button('样例').click())
  await flush()
  expect(run).not.toHaveBeenCalled()
  await act(async () => monitor('样例').click())
  await flush()
  expect(run).toHaveBeenCalledTimes(1)
  expect(run).toHaveBeenCalledWith('sample', undefined, '2026-10-23', undefined, 'stock', '1d')
})

it('does not select the enabled strategy again after a delayed monitoring save', async () => {
  summary.mockImplementation(async (day: string) => ({ as_of: day, results: {}, today_ever_counts: {} }))
  let finish!: (value: any) => void
  monitorSave.mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
  run.mockResolvedValue({ as_of: '2026-10-23', strategy: 'sample',
    rows: [{ symbol: 'PREVIOUS' }], total: 1 })
  await render()
  await act(async () => monitor('样例').click())
  await act(async () => button('分钟样例').click())
  await setDate('2026-10-22')
  await act(async () => finish({}))
  await flush()
  expect(run).toHaveBeenCalledWith('sample', undefined, '2026-10-23', undefined, 'stock', '1d')
  expect(host.querySelector<HTMLInputElement>('[aria-label="测试日期"]')!.value).toBe('2026-10-22')
  expect(host.textContent).not.toContain('PREVIOUS')
})

it('does not execute when monitoring save fails and allows retry', async () => {
  summary.mockImplementation(async (day: string) => ({ as_of: day, results: {}, today_ever_counts: {} }))
  monitorSave.mockRejectedValueOnce(new Error('保存失败'))
  run.mockResolvedValue({ as_of: '2026-10-23', strategy: 'sample', rows: [], total: 0 })
  await render()
  await act(async () => monitor('样例').click())
  await flush()
  expect(run).not.toHaveBeenCalled()
  await act(async () => monitor('样例').click())
  await flush()
  expect(monitorSave).toHaveBeenCalledTimes(2)
  expect(run).toHaveBeenCalledTimes(1)
})

it('does not enable monitoring or execute before a trading date is available', async () => {
  dataState.latest = null
  await render()
  await act(async () => monitor('样例').click())
  await flush()
  expect(monitorSave).not.toHaveBeenCalled()
  expect(run).not.toHaveBeenCalled()
})

it('cancels existing monitoring without calculation or changing a historical date', async () => {
  rules.push({ id: 'sample-rule', type: 'strategy', strategy_id: 'sample', enabled: true, asset_type: 'stock' })
  summary.mockImplementation(async (day: string) => ({ as_of: day, results: {}, today_ever_counts: {} }))
  await render()
  await setDate('2026-10-22')
  await act(async () => monitor('样例').click())
  await flush()
  expect(monitorDelete).toHaveBeenCalledWith('sample-rule')
  expect(monitorSave).not.toHaveBeenCalled()
  expect(run).not.toHaveBeenCalled()
  expect(host.querySelector<HTMLInputElement>('[aria-label="测试日期"]')!.value).toBe('2026-10-22')
})

it('adds a new strategy to the pool without executing it', async () => {
  summary.mockImplementation(async (day: string) => ({ as_of: day, results: {}, today_ever_counts: {} }))
  await render()
  await act(async () => button('策略池').click())
  await act(async () => button('加入新增样例').click())
  await flush()
  expect(reorderPool).toHaveBeenCalledWith(['sample', 'minute', 'added'])
  expect(runAll).not.toHaveBeenCalled()
  expect(run).not.toHaveBeenCalled()
})

it.each([['创建策略 · AI', '保存新策略', 'added'], ['叠加策略', '保存叠加策略', 'composite']])(
  'only adds a strategy created through %s to the pool', async (entry, save, id) => {
    summary.mockImplementation(async (day: string) => ({ as_of: day, results: {}, today_ever_counts: {} }))
    await render()
    await act(async () => button(entry).click())
    await act(async () => button(save).click())
    await flush()
    expect(addToPool).toHaveBeenCalledWith(id)
    expect(runAll).not.toHaveBeenCalled()
    expect(run).not.toHaveBeenCalled()
  })

it('keeps manual batch refresh for both daily and minute strategies', async () => {
  summary.mockImplementation(async (day: string) => ({ as_of: day, results: {}, today_ever_counts: {} }))
  reload.mockResolvedValue({})
  runAll.mockResolvedValue({ as_of: '2026-10-23', results: {} })
  await render()
  expect(runAll).not.toHaveBeenCalled()
  const refresh = host.querySelector<HTMLButtonElement>('button[title="重新加载策略并运行全部策略，刷新当前符合条件的个股"]')
  expect(refresh).not.toBeNull()
  await act(async () => refresh!.click())
  await flush()
  await flush()
  expect(reload).toHaveBeenCalledTimes(1)
  expect(runAll).toHaveBeenCalledWith('2026-10-23', ['sample'], 'stock')
  expect(runAll).toHaveBeenCalledWith('2026-10-23', ['minute'], 'stock', '1m')
})

it('does not continue a manual refresh into minute tasks after the selected date changes', async () => {
  summary.mockImplementation(async (day: string) => ({ as_of: day, results: {}, today_ever_counts: {} }))
  reload.mockResolvedValue({})
  let finish!: (value: any) => void
  runAll.mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
  await render()
  await act(async () => host.querySelector<HTMLButtonElement>(
    'button[title="重新加载策略并运行全部策略，刷新当前符合条件的个股"]',
  )!.click())
  await flush()
  await setDate('2026-10-22')
  await act(async () => finish({ as_of: '2026-10-23', results: {} }))
  await flush()
  expect(runAll).toHaveBeenCalledTimes(1)
  expect(host.querySelector<HTMLInputElement>('input[aria-label="测试日期"]')!.value).toBe('2026-10-22')
})

it('uses the active result tab for both bulk actions, including after changing dates', async () => {
  summary.mockImplementation(async (day: string) => ({
    as_of: day, results: { sample: { total: 1, as_of: day } }, today_ever_counts: {},
  }))
  detail.mockImplementation(async (_sid: string, _ext: string, day: string) => ({
    result: { as_of: day, strategy: 'sample', rows: [{ symbol: 'WRONG' }], total: 1,
      sections: { sell: [{ symbol: `${day}-SELL` }], select: [{ symbol: `${day}-SELECT` }],
        buy: [{ symbol: `${day}-BUY`, filled_shares: 100, buy_price: 10 }] } },
    today_ever_rows: {}, strategy_ids_by_symbol: {},
  }))
  await render()
  await act(async () => button('样例').click())
  await flush()
  for (const [index, suffix] of [[0, 'SELL'], [1, 'SELECT']] as const) {
    await act(async () => host.querySelectorAll<HTMLButtonElement>('[role="tab"]')[index].click())
    await act(async () => host.querySelector<HTMLButtonElement>('[aria-label="批量加入自选"]')!.click())
    await flush()
    await act(async () => document.querySelector<HTMLButtonElement>('[role="menuitem"]')!.click())
    expect(batchMutate).toHaveBeenLastCalledWith(
      { symbols: [`2026-10-23-${suffix}`], groupId: null }, expect.anything(),
    )
  }
  await act(async () => host.querySelectorAll<HTMLButtonElement>('[role="tab"]')[2].click())
  const watchlist = host.querySelector<HTMLButtonElement>('[aria-label="批量加入自选"]')!
  const holdings = host.querySelector<HTMLButtonElement>('[aria-label="批量加持仓"]')
  expect(holdings).not.toBeNull()
  expect(watchlist.parentElement).toBe(holdings!.parentElement)
  await act(async () => holdings!.click())
  expect(host.querySelector('[role="dialog"]')?.textContent).toContain('2026-10-23-BUY')
  expect(host.querySelector('[role="dialog"]')?.textContent).not.toContain('WRONG')
  await setDate('2026-10-22')
  await flush()
  expect(host.querySelector('[role="dialog"]')).toBeNull()
  await act(async () => host.querySelector<HTMLButtonElement>('[aria-label="批量加入自选"]')!.click())
  await flush()
  await act(async () => document.querySelector<HTMLButtonElement>('[role="menuitem"]')!.click())
  expect(batchMutate).toHaveBeenLastCalledWith(
    { symbols: ['2026-10-22-SELL'], groupId: null }, expect.anything(),
  )
})

it('shows no bulk actions on an empty active tab even if another tab has buys', async () => {
  summary.mockImplementation(async (day: string) => ({
    as_of: day, results: { sample: { total: 1, as_of: day } }, today_ever_counts: {},
  }))
  detail.mockResolvedValue({ result: { as_of: '2026-10-23', strategy: 'sample', rows: [{ symbol: 'BUY' }],
    total: 1, sections: { sell: [], select: [], buy: [{ symbol: 'BUY' }] } }, today_ever_rows: {} })
  await render()
  await act(async () => button('样例').click())
  await flush()
  expect(host.querySelector('[aria-label="批量加入自选"]')).toBeNull()
  expect(host.querySelector('[aria-label="批量加持仓"]')).toBeNull()
})
