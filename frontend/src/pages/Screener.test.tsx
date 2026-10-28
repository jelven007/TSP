// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, it, vi } from 'vitest'
import { Screener } from './Screener'

const { summary, detail, run, runAll, reload, oldPreferences } = vi.hoisted(() => ({
  summary: vi.fn(), detail: vi.fn(), run: vi.fn(), runAll: vi.fn(),
  reload: vi.fn(),
  oldPreferences: { screener_auto_run: false as boolean | undefined },
}))
vi.mock('@/lib/api', () => ({
  api: {
    screenerStrategies: async () => ({ presets: [
      { id: 'sample', name: '样例', asset_types: ['stock'], timeframes: ['1d'] },
      { id: 'minute', name: '分钟样例', asset_types: ['stock'], timeframes: ['1m'] },
    ] }),
    screenerCachedSummary: summary,
    screenerCachedResult: detail,
    screenerRunPreset: run,
    screenerRunAll: runAll,
    strategyReload: reload,
    watchlistList: async () => ({ symbols: [] }),
    monitorRulesList: async () => ({ rules: [] }),
    minuteRefreshStatus: async () => ({}),
    watchlistGroups: async () => ({ groups: [] }),
  },
}))
vi.mock('@/lib/useSharedQueries', () => ({
  usePreferences: () => ({ data: oldPreferences }),
  useDataStatus: () => ({ data: { enriched: { latest_date: '2026-10-23', earliest_date: '2026-10-20' } } }),
  useCapabilities: () => ({ data: {} }),
  useQuoteStatus: () => ({ data: {} }),
}))
vi.mock('@/lib/useSharedMutations', () => ({ useWatchlistBatchAdd: () => ({ mutate: vi.fn() }) }))
vi.mock('@/lib/useStrategyPool', () => ({
  useStrategyPool: () => ({ pool: ['sample', 'minute'], prune: vi.fn() }),
}))
vi.mock('@/lib/screener-columns', () => ({
  SCREENER_BUILTIN_COLUMNS: [], SCREENER_COLUMN_GROUPS: [],
  buildExtColumnsParam: () => '', loadScreenerColumnConfig: async () => [], saveScreenerColumnConfig: vi.fn(),
}))
vi.mock('@/components/DatePicker', () => ({
  DatePicker: ({ value, onChange }: any) => <input aria-label="测试日期" value={value} onChange={e => onChange(e.target.value)} />,
}))
vi.mock('@/components/screener/StrategyCard', () => ({
  StrategyCard: ({ name, onRun }: any) => <button onClick={onRun}>{name}</button>,
  loadCardSize: () => 'normal', cardWrapCls: () => '',
}))
vi.mock('@/components/screener/ScreenerTable', () => ({
  ScreenerTable: ({ rows }: any) => <div data-testid="rows">{JSON.stringify(rows)}</div>,
}))
vi.mock('@/components/Toast', () => ({ toast: vi.fn() }))
vi.mock('@/components/StockPreviewDialog', () => ({ StockPreviewDialog: () => null }))
vi.mock('@/components/screener/StrategyBuilderDialog', () => ({ StrategyBuilderDialog: () => null }))

Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
let root: Root
let host: HTMLDivElement
let client: QueryClient

afterEach(async () => {
  await act(async () => root?.unmount())
  client?.clear()
  host?.remove()
  vi.clearAllMocks()
  oldPreferences.screener_auto_run = false
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
  return [...host.querySelectorAll('button')].find(b => b.textContent === text)!
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

it('runs a minute strategy only after clicking its card', async () => {
  oldPreferences.screener_auto_run = true
  summary.mockImplementation(async (day: string) => ({ as_of: day, results: {}, today_ever_counts: {} }))
  runAll.mockResolvedValue({ as_of: '2026-10-23', results: {} })
  run.mockResolvedValue({ as_of: '2026-10-23', rows: [], total: 0, strategy: 'minute' })
  await render()
  expect(runAll).not.toHaveBeenCalled()
  expect(run).not.toHaveBeenCalled()
  await act(async () => button('分钟样例').click())
  await flush()
  expect(run).toHaveBeenCalledWith('minute', undefined, '2026-10-23', undefined, 'stock', '1m')
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
