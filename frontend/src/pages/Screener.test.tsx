// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { Screener } from './Screener'

const { summary, detail, dayView, run, runAll, reload, batchMutate, oldPreferences,
  monitorList, monitorSave, monitorDelete, monitorToggle, monitorTasks, datedTasks, rules, poolState, dataState, clockState, reorderPool, addToPool, calendar } = vi.hoisted(() => ({
  summary: vi.fn(), detail: vi.fn(), dayView: vi.fn(), run: vi.fn(), runAll: vi.fn(),
  reload: vi.fn(), batchMutate: vi.fn(),
  oldPreferences: { screener_auto_run: false as boolean | undefined },
  monitorList: vi.fn(), monitorSave: vi.fn(), monitorDelete: vi.fn(),
  monitorToggle: vi.fn(), monitorTasks: vi.fn(), datedTasks: vi.fn(),
  rules: [] as any[], poolState: { ids: ['sample', 'minute'] },
  dataState: { latest: '2026-10-23' as string | null },
  clockState: { data: {} as { market_date?: string; market_time?: string; is_trading_day?: boolean | null } },
  reorderPool: vi.fn(), addToPool: vi.fn(), calendar: vi.fn(),
}))
vi.mock('@/lib/api', () => ({
  genRuleId: () => 'monitor-rule',
  api: {
    screenerStrategies: async () => ({ presets: [
      { id: 'sample', name: '样例', asset_types: ['stock'], timeframes: ['1d'] },
      { id: 'minute', name: '分钟样例', asset_types: ['stock'], timeframes: ['1m'] },
      { id: 's02', name: 'S02', asset_types: ['stock'], timeframes: ['1m'] },
      { id: 'custom_mvxxcp88', name: '自定义S02', asset_types: ['stock'], timeframes: ['1m'],
        source: 'custom', result_sections: 'signals' },
      { id: 'custom_mvxxcs01', name: 's01', asset_types: ['stock'], timeframes: ['1m'],
        source: 'custom', result_sections: 'signals', minute_execution: 'overnight_capacity_v1', optional_triggers: true },
      { id: 'custom_mvxxcs05', name: 's05', asset_types: ['stock'], timeframes: ['1m'],
        source: 'custom', result_sections: 'signals' },
      { id: 'added', name: '新增样例', asset_types: ['stock'], timeframes: ['1d'] },
      { id: 'composite', name: '叠加样例', asset_types: ['stock'], timeframes: ['1d'] },
    ] }),
    screenerCachedSummary: summary,
    screenerCachedResult: detail,
    screenerDayView: dayView,
    screenerRunPreset: run,
    screenerRunAll: runAll,
    strategyReload: reload,
    watchlistList: async () => ({ symbols: [] }),
    monitorRulesList: monitorList,
    monitorRuleSave: monitorSave,
    monitorRuleDelete: monitorDelete,
    strategyMonitoringSet: monitorToggle,
    strategyMonitoring: monitorTasks,
    strategyMonitoringDay: datedTasks,
    minuteRefreshStatus: async () => ({}),
    watchlistGroups: async () => ({ groups: [] }),
    strategyGet: async () => ({ params_defaults: { position_limit: 5, take_profit_pct: .06, stop_loss_pct: .05 } }),
    lotBatches: async () => ({ batches: [], next_number: '2026110101' }),
    lotExpiryOptions: calendar,
    marketSnapshot: async () => ({ as_of: null, rows: [] }),
  },
}))
vi.mock('@/lib/useSharedQueries', () => ({
  usePreferences: () => ({ data: oldPreferences }),
  useDataStatus: () => ({ data: { enriched: { latest_date: dataState.latest, earliest_date: '2026-09-01' } } }),
  useCapabilities: () => ({ data: {} }),
  useQuoteStatus: () => ({ data: clockState.data }),
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
  DatePicker: ({ value, onChange, min, max }: any) => <input aria-label="测试日期" min={min} max={max} value={value} onChange={e => onChange(e.target.value)} />,
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
vi.mock('@/components/screener/StrategySettingsDialog', () => ({
  StrategySettingsDialog: ({ strategyId, onSaved }: any) => strategyId
    ? <button onClick={() => onSaved(5)}>保存测试参数</button> : null,
}))

Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
let root: Root
let host: HTMLDivElement
let client: QueryClient

beforeEach(() => {
  rules.length = 0
  poolState.ids = ['sample', 'minute']
  dataState.latest = '2026-10-23'
  clockState.data = {}
  calendar.mockReset().mockImplementation(async base => ({
    base_date: base, next_trading_day: null, five_trading_days: null,
  }))
  monitorList.mockReset().mockImplementation(async () => ({ rules: [...rules] }))
  monitorSave.mockReset().mockImplementation(async rule => {
    rules.push(rule)
    return { rule }
  })
  monitorDelete.mockReset().mockResolvedValue({})
  monitorTasks.mockReset().mockResolvedValue({ strategies: [] })
  datedTasks.mockReset()
  monitorToggle.mockReset().mockResolvedValue({ enabled: true })
  run.mockReset()
  runAll.mockReset()
  dayView.mockReset().mockImplementation((id, day) => detail(id, undefined, day, 'stock', '1m'))
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

it('queries historical signals immediately even while the card summary remains pending', async () => {
  poolState.ids = ['custom_mvxxcp88']
  dataState.latest = '2026-11-06'
  clockState.data = { market_date: '2026-11-08', market_time: '2026-11-08T10:00:00+08:00', is_trading_day: false }
  summary.mockImplementation(() => new Promise(() => {}))
  detail.mockResolvedValue({ result: null })
  dayView.mockImplementation(async (id, day) => ({
    result: { as_of: day, strategy: id, rows: [], total: 0, elapsed_ms: 0,
      section_mode: 'signals', sections: { sell: [], select: [{ symbol: '600001.SH' }], buy: [] } },
    updated_at: 1,
  }))
  await render()
  await act(async () => button('自定义S02').click())
  await flush()
  expect(dayView).toHaveBeenCalledWith('custom_mvxxcp88', '2026-11-06', 'stock', '1m')
  expect(host.querySelector('[role="tablist"]')?.textContent).toContain('选 · 1')
  expect(detail).not.toHaveBeenCalled()
  summary.mockClear()
  await setDate('2026-06-22')
  expect(dayView).toHaveBeenCalledWith('custom_mvxxcp88', '2026-06-22', 'stock', '1m')
  expect(summary).not.toHaveBeenCalled()
})

it('reads current S02 directly without a summary and never polls its completed result', async () => {
  poolState.ids = ['custom_mvxxcp88']
  dataState.latest = '2026-11-06'
  clockState.data = { market_date: '2026-11-06', market_time: '2026-11-06T14:40:00+08:00', is_trading_day: true }
  summary.mockImplementation(() => new Promise(() => {}))
  detail.mockImplementation(async (id, _ext, day) => ({
    result: { as_of: day, strategy: id, rows: [], total: 0, elapsed_ms: 0,
      section_mode: 'signals', section_statuses: { buy: 'rejected' },
      execution_status: { state: 'success' },
      sections: { sell: [], select: [{ symbol: '600001.SH' }], buy: [] } },
    updated_at: 1,
  }))
  await render()
  await act(async () => button('自定义S02').click())
  await flush()
  expect(detail).toHaveBeenCalledWith('custom_mvxxcp88', undefined, '2026-11-06', 'stock', '1m')
  expect(host.querySelector('[role="tablist"]')?.textContent).toContain('选 · 1')
  const reads = detail.mock.calls.length
  await act(async () => {
    window.dispatchEvent(new Event('focus'))
  })
  await flush()
  expect(detail.mock.calls.length).toBe(reads)
})

it('keeps current S02 tabs available with explicit missing archive state and no recalculation', async () => {
  poolState.ids = ['custom_mvxxcp88']
  dataState.latest = '2026-11-18'
  clockState.data = { market_date: '2026-11-18', market_time: '2026-11-18T16:30:00+08:00',
    is_trading_day: true }
  summary.mockResolvedValue({ as_of: '2026-11-18', results: {}, today_ever_counts: {} })
  detail.mockResolvedValue({ result: null, updated_at: null })
  await render()
  await act(async () => button('自定义S02').click())
  await flush()
  expect(host.querySelector('[role="tablist"]')?.textContent).toContain('卖 · 未归档')
  expect(host.textContent).not.toContain('点击策略卡片查看选股结果')
  for (const tab of ['选', '买', '卖']) {
    await act(async () => [...host.querySelectorAll<HTMLButtonElement>('[role="tab"]')]
      .find(item => item.textContent?.startsWith(tab))!.click())
    await flush()
    expect(host.textContent).toContain('该交易日归档尚未生成')
  }
  expect(run).not.toHaveBeenCalled()
  expect(runAll).not.toHaveBeenCalled()
})

it('allows five years of saved history independently of enriched market coverage', async () => {
  clockState.data = { market_time: '2026-11-08T10:00:00+08:00' }
  summary.mockResolvedValue({ as_of: '2026-10-23', results: {}, today_ever_counts: {} })
  await render()
  expect(host.querySelector<HTMLInputElement>('[aria-label="测试日期"]')?.min).toBe('2021-11-08')
})

it('clears the displayed historical result when its saved view is deleted', async () => {
  const { QK } = await import('@/lib/queryKeys')
  poolState.ids = ['custom_mvxxcp88']
  dataState.latest = '2026-11-06'
  clockState.data = { market_time: '2026-11-08T10:00:00+08:00' }
  summary.mockResolvedValue({ as_of: '2026-11-06', results: {}, today_ever_counts: {} })
  dayView.mockResolvedValue({ result: { as_of: '2026-11-06', strategy: 'custom_mvxxcp88',
    rows: [], total: 0, elapsed_ms: 0, section_mode: 'signals',
    sections: { sell: [], select: [{ symbol: 'SAVED' }], buy: [] } } })
  await render()
  await act(async () => button('自定义S02').click())
  await flush()
  expect(host.querySelector('[role="tablist"]')?.textContent).toContain('选 · 1')
  await act(async () => client.setQueryData(QK.screenerDayView('custom_mvxxcp88', '2026-11-06'),
    { result: null, today_ever_rows: null, strategy_ids_by_symbol: {}, updated_at: null }))
  await flush()
  expect(host.querySelector('[role="tablist"]')?.textContent).toContain('选 · 未归档')
  expect(host.textContent).not.toContain('选 · 1')
  expect(host.textContent).not.toContain('SAVED')
  expect(host.textContent).toContain('该交易日归档尚未生成')
})

it.each([
  ['custom_mvxxcs01', 's01'],
  ['custom_mvxxcp88', '自定义S02'],
])('explains a missing historical signal archive for %s after choosing its date', async (id, name) => {
  poolState.ids = [id]
  dataState.latest = '2026-11-18'
  clockState.data = { market_date: '2026-11-18', market_time: '2026-11-18T16:30:00+08:00',
    is_trading_day: true }
  summary.mockResolvedValue({ as_of: '2026-11-18', results: {}, today_ever_counts: {} })
  dayView.mockResolvedValue({ result: null, updated_at: null })
  await render()
  await act(async () => button(name).click())
  await setDate('2026-11-17')
  expect(dayView).toHaveBeenCalledWith(id, '2026-11-17', 'stock', '1m')
  expect(host.textContent).toContain('该交易日归档尚未生成')
  expect(host.textContent).not.toContain('点击策略卡片查看选股结果')
  expect(host.querySelector('[role="tablist"]')?.textContent).toContain('卖 · 未归档')
  for (const tab of ['选', '买', '卖']) {
    await act(async () => [...host.querySelectorAll<HTMLButtonElement>('[role="tab"]')]
      .find(item => item.textContent?.startsWith(tab))!.click())
    await flush()
    expect(host.textContent).toContain('该交易日归档尚未生成')
  }
  expect(run).not.toHaveBeenCalled()
  expect(runAll).not.toHaveBeenCalled()
})

it('removes the repeated S02 result heading while retaining active-tab bulk actions', async () => {
  poolState.ids = ['custom_mvxxcp88']
  summary.mockResolvedValue({ as_of: '2026-10-23', results: {}, today_ever_counts: {} })
  dayView.mockResolvedValue({ result: {
    as_of: '2026-10-23', strategy: 'custom_mvxxcp88', rows: [], total: 0, elapsed_ms: 0,
    computed_at: 1792715346000, section_mode: 'signals',
    sections: { sell: [], select: [{ symbol: '600529.SH' }], buy: [] },
  } })
  await render()
  await act(async () => button('自定义S02').click())
  await flush()
  expect(host.querySelector('[role="tablist"]')).not.toBeNull()
  expect(host.querySelector('h2')).toBeNull()
  await act(async () => host.querySelectorAll<HTMLButtonElement>('[role="tab"]')[1].click())
  expect(host.querySelector('[aria-label="批量加入自选"]')).not.toBeNull()
  expect(host.querySelector('[aria-label="批量加持仓"]')).not.toBeNull()
  const toolbar = host.querySelector('[aria-label="每日策略工具栏"]')
  expect(toolbar).not.toBeNull()
  expect(toolbar!.contains(host.querySelector('[role="tablist"]'))).toBe(true)
  expect(toolbar!.contains(host.querySelector('[aria-label="批量加入自选"]'))).toBe(true)
  expect(toolbar!.contains(host.querySelector('[aria-label="批量加持仓"]'))).toBe(true)
  expect(toolbar!.contains(host.querySelector('[title="列表配置"]'))).toBe(true)
  expect(host.querySelector('[role="tablist"]')?.nextElementSibling?.textContent)
    .toContain('历史保存结果 · 未保存阶段进展')
  expect(run).not.toHaveBeenCalled()
})

it('gives S01 archived signal tabs, next selection and bulk actions even without a summary', async () => {
  poolState.ids = ['custom_mvxxcs01']
  calendar.mockImplementation(async base => ({ base_date: base, next_trading_day: '2026-10-26' }))
  summary.mockResolvedValue({ as_of: '2026-10-23', results: {}, today_ever_counts: {} })
  dayView.mockImplementation(async (id, day) => ({ result: {
    as_of: day, strategy: id, rows: [], total: 0, elapsed_ms: 0, section_mode: 'signals',
    sections: { sell: [{ symbol: '600001.SH' }],
      select: [{ symbol: '600002.SH' }, { symbol: '600003.SH' }], buy: [] },
  } }))
  await render()
  await act(async () => button('s01').click())
  await flush()
  expect(dayView).toHaveBeenCalledWith('custom_mvxxcs01', '2026-10-23', 'stock', '1m')
  expect(button('次日选股')).toBeDefined()
  expect(host.querySelector('h2')).toBeNull()
  const toolbar = host.querySelector('[aria-label="每日策略工具栏"]')!
  expect(toolbar).not.toBeNull()
  expect(toolbar.contains(host.querySelector('[role="tablist"]'))).toBe(true)
  await act(async () => host.querySelectorAll<HTMLButtonElement>('[role="tab"]')[1].click())
  expect(toolbar.contains(host.querySelector('[aria-label="批量加入自选"]'))).toBe(true)
  await act(async () => host.querySelector<HTMLButtonElement>('[aria-label="批量加入自选"]')!.click())
  await flush()
  await act(async () => document.querySelector<HTMLButtonElement>('[role="menuitem"]')!.click())
  expect(batchMutate).toHaveBeenCalledWith({ symbols: ['600002.SH', '600003.SH'], groupId: null }, expect.anything())
  await act(async () => host.querySelectorAll<HTMLButtonElement>('[role="tab"]')[2].click())
  expect(host.querySelector('[role="tabpanel"]')?.textContent).toContain('该交易日没有触发入场触发器的股票')
  await setDate('2026-10-22')
  expect(dayView).toHaveBeenCalledWith('custom_mvxxcs01', '2026-10-22', 'stock', '1m')
  expect(run).not.toHaveBeenCalled()
})

it.each(['custom_mvxxcs01', 'custom_mvxxcp88'])('toggles %s without data, date changes or immediate execution', async sid => {
  poolState.ids = [sid]
  dataState.latest = null
  monitorTasks.mockResolvedValue({ strategies: [{
    strategy_id: sid, enabled: false, as_of: '2026-11-02', tasks: {
      select: { state: 'disabled', windows: [{ start: '20:00:00', end: '21:00:00' }] },
      buy: { state: 'disabled', windows: [{ start: '14:30:00', end: '14:55:00' }] },
      sell: { state: 'disabled', windows: [{ start: '09:30:00', end: '11:30:00' }] },
    },
  }] })
  await render()
  const name = sid.endsWith('cs01') ? 's01' : '自定义S02'
  await act(async () => button(name).click())
  await flush()
  expect(host.textContent).toContain('20:00:00～21:00:00')
  await act(async () => monitor(name).click())
  await flush()
  expect(monitorToggle).toHaveBeenCalledWith(sid, true)
  expect(monitorSave).not.toHaveBeenCalled()
  expect(run).not.toHaveBeenCalled()
  expect(runAll).not.toHaveBeenCalled()
})

it('cancels custom S02 critical monitoring through the strategy lifecycle', async () => {
  poolState.ids = ['custom_mvxxcp88']
  rules.push({ id: 'critical', type: 'strategy', strategy_id: 'custom_mvxxcp88',
    enabled: true, asset_type: 'stock', _system_task: true })
  await render()
  await setDate('2026-10-22')
  await act(async () => monitor('自定义S02').click())
  await flush()
  expect(monitorToggle).toHaveBeenCalledWith('custom_mvxxcp88', false)
  expect(monitorDelete).not.toHaveBeenCalled()
  expect(run).not.toHaveBeenCalled()
  expect(host.querySelector<HTMLInputElement>('[aria-label="测试日期"]')!.value).toBe('2026-10-22')
})
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

it.each(['00:00:00', '09:23:00'])('uses the current trading day at %s before today daily data exists', async time => {
  dataState.latest = '2026-10-30'
  clockState.data = { market_date: '2026-11-02', market_time: `2026-11-02T${time}+08:00`, is_trading_day: true }
  await render()
  const date = host.querySelector<HTMLInputElement>('[aria-label="测试日期"]')!
  expect(date.value).toBe('2026-11-02')
  expect(date.max).toBe('2026-11-02')
  expect(run).not.toHaveBeenCalled()
  expect(runAll).not.toHaveBeenCalled()
})

it('uses the trading clock even when no enriched date is available', async () => {
  dataState.latest = null
  clockState.data = { market_date: '2026-11-02', market_time: '2026-11-02T00:00:00+08:00', is_trading_day: true }
  await render()
  expect(host.querySelector<HTMLInputElement>('[aria-label="测试日期"]')?.value).toBe('2026-11-02')
})

it('rolls the default view into the new trading day without executing', async () => {
  dataState.latest = '2026-10-30'
  clockState.data = { market_date: '2026-11-01', market_time: '2026-11-01T23:59:59+08:00', is_trading_day: false }
  await render()
  expect(host.querySelector<HTMLInputElement>('[aria-label="测试日期"]')?.value).toBe('2026-10-30')
  clockState.data = { market_date: '2026-11-02', market_time: '2026-11-02T00:00:00+08:00', is_trading_day: true }
  await act(async () => root.render(<QueryClientProvider client={client}><Screener /></QueryClientProvider>))
  await flush()
  expect(host.querySelector<HTMLInputElement>('[aria-label="测试日期"]')?.value).toBe('2026-11-02')
  expect(run).not.toHaveBeenCalled()
  expect(runAll).not.toHaveBeenCalled()
})

it('keeps an explicitly chosen historical date when the trading clock refreshes', async () => {
  dataState.latest = '2026-10-30'
  clockState.data = { market_date: '2026-11-02', market_time: '2026-11-02T09:23:00+08:00', is_trading_day: true }
  await render()
  await setDate('2026-10-22')
  clockState.data = { market_date: '2026-11-03', market_time: '2026-11-03T00:00:00+08:00', is_trading_day: true }
  await act(async () => root.render(<QueryClientProvider client={client}><Screener /></QueryClientProvider>))
  await flush()
  const date = host.querySelector<HTMLInputElement>('[aria-label="测试日期"]')!
  expect(date.value).toBe('2026-10-22')
  expect(date.max).toBe('2026-11-03')
})

it('shows S05 tasks for the chosen date even when an earlier date responds late', async () => {
  const id = 'custom_mvxxcs05', today = '2026-11-27', past = '2026-11-26'
  poolState.ids = [id]
  clockState.data = { market_date: today, is_trading_day: true }
  const schedule = (day: string) => ({
    strategy_id: id, as_of: day, enabled: true, historical: day !== today,
    tasks: {
      select: { state: 'no_receipt', windows: [] },
      buy: { state: 'no_receipt', branch_code: 4, branch_date: day, windows: [] },
      sell: { state: 'expired', branch_code: 2, branch_date: '2026-11-25',
        windows: [{ start: '09:43:00', end: '13:55:00', signal_name: '分支2出场' }],
        receipt_source: 'scheduler' },
    },
  })
  let completeToday!: (value: unknown) => void
  datedTasks.mockImplementation((_id, day) => day === today
    ? new Promise(resolve => { completeToday = resolve }) : Promise.resolve(schedule(day)))
  dayView.mockResolvedValue({ result: null })
  await render()
  await act(async () => button('s05').click())
  await flush()
  await setDate(past)
  await flush()
  expect(datedTasks).toHaveBeenCalledWith(id, past)
  await act(async () => completeToday(schedule(today)))
  await flush()
  const tasks = host.querySelector('[aria-label="策略定时任务"]')?.textContent
  expect(tasks).toContain('历史定时任务 · 北京时间 · 2026-11-26')
  expect(tasks).toContain('2026-11-25')
  expect(tasks).toContain('09:43:00～13:55:00')
  expect(tasks).toContain('不安排买入')
  expect(tasks).toContain('已结束')
  expect(tasks).not.toContain(today)
  expect(run).not.toHaveBeenCalled()
})

it('enables monitoring without changing the viewed date or starting a calculation', async () => {
  dataState.latest = '2026-10-30'
  clockState.data = { market_date: '2026-11-02', market_time: '2026-11-02T09:23:00+08:00', is_trading_day: true }
  run.mockResolvedValue({ as_of: '2026-11-02', strategy: 'minute', rows: [], total: 0 })
  await render()
  await setDate('2026-09-01')
  await act(async () => monitor('分钟样例').click())
  await flush()
  expect(monitorToggle).toHaveBeenCalledWith('minute', true)
  expect(run).not.toHaveBeenCalled()
  expect(host.querySelector<HTMLInputElement>('[aria-label="测试日期"]')?.value).toBe('2026-09-01')
})

it('rechecks the server calendar at Beijing midnight even without a quote update', async () => {
  dataState.latest = '2026-10-30'
  clockState.data = { market_date: '2026-11-01', market_time: '2026-11-01T23:59:59+08:00', is_trading_day: false }
  const schedule = vi.spyOn(window, 'setTimeout')
  await render()
  const invalidate = vi.spyOn(client, 'invalidateQueries')
  const midnight = schedule.mock.calls.find(([, delay]) => delay === 1001)
  expect(midnight).toBeDefined()
  await act(async () => { (midnight![0] as () => void)() })
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ['quote-status'] })
  schedule.mockRestore()
})

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

it('ignores a late monitoring save from the previous date and keeps the selected date', async () => {
  summary.mockImplementation(async (day: string) => ({ as_of: day, results: {}, today_ever_counts: {} }))
  let finish!: (value: any) => void
  monitorToggle.mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
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
  'selects %s and enables monitoring without immediate execution', async (name, id) => {
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
  expect(monitorToggle).toHaveBeenCalledWith(id, true)
  expect(run).not.toHaveBeenCalled()
})

it.each([
  ['2026-11-01T12:00:00+08:00', '2026-10-30'],
  ['2026-10-06T12:00:00+08:00', '2026-09-30'],
])('keeps the selected historical date when enabling monitoring on %s', async (now, latest) => {
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
  expect(host.querySelector<HTMLInputElement>('[aria-label="测试日期"]')!.value).toBe('2026-09-01')
  expect(monitorToggle).toHaveBeenCalledWith('sample', true)
  expect(run).not.toHaveBeenCalled()
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

it('waits for monitoring save and prevents duplicate activation', async () => {
  summary.mockImplementation(async (day: string) => ({ as_of: day, results: {}, today_ever_counts: {} }))
  let finish!: (value: any) => void
  monitorToggle.mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
  run.mockResolvedValue({ as_of: '2026-10-23', strategy: 'minute', rows: [], total: 0 })
  await render()
  await act(async () => { monitor('分钟样例').click(); monitor('分钟样例').click() })
  expect(monitorToggle).toHaveBeenCalledTimes(1)
  expect(run).not.toHaveBeenCalled()
  await act(async () => finish({}))
  await flush()
  expect(run).not.toHaveBeenCalled()
})

it('keeps a saved result when enabling monitoring', async () => {
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
  expect(monitorToggle).toHaveBeenCalledWith('sample', true)
  expect(run).not.toHaveBeenCalled()
})

it('does not select the enabled strategy again after a delayed monitoring save', async () => {
  summary.mockImplementation(async (day: string) => ({ as_of: day, results: {}, today_ever_counts: {} }))
  let finish!: (value: any) => void
  monitorToggle.mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
  run.mockResolvedValue({ as_of: '2026-10-23', strategy: 'sample',
    rows: [{ symbol: 'PREVIOUS' }], total: 1 })
  await render()
  await act(async () => monitor('样例').click())
  await act(async () => button('分钟样例').click())
  await setDate('2026-10-22')
  await act(async () => finish({}))
  await flush()
  expect(run).not.toHaveBeenCalled()
  expect(host.querySelector<HTMLInputElement>('[aria-label="测试日期"]')!.value).toBe('2026-10-22')
  expect(host.textContent).not.toContain('PREVIOUS')
})

it('does not execute when monitoring save fails and allows retry', async () => {
  summary.mockImplementation(async (day: string) => ({ as_of: day, results: {}, today_ever_counts: {} }))
  monitorToggle.mockRejectedValueOnce(new Error('保存失败'))
  run.mockResolvedValue({ as_of: '2026-10-23', strategy: 'sample', rows: [], total: 0 })
  await render()
  await act(async () => monitor('样例').click())
  await flush()
  expect(run).not.toHaveBeenCalled()
  await act(async () => monitor('样例').click())
  await flush()
  expect(monitorToggle).toHaveBeenCalledTimes(2)
  expect(run).not.toHaveBeenCalled()
})

it('allows explicit activation without available trading data', async () => {
  dataState.latest = null
  await render()
  await act(async () => monitor('样例').click())
  await flush()
  expect(monitorSave).not.toHaveBeenCalled()
  expect(monitorToggle).toHaveBeenCalledWith('sample', true)
  expect(run).not.toHaveBeenCalled()
})

it('cancels existing monitoring without calculation or changing a historical date', async () => {
  rules.push({ id: 'sample-rule', type: 'strategy', strategy_id: 'sample', enabled: true, asset_type: 'stock' })
  summary.mockImplementation(async (day: string) => ({ as_of: day, results: {}, today_ever_counts: {} }))
  await render()
  await setDate('2026-10-22')
  await act(async () => monitor('样例').click())
  await flush()
  expect(monitorToggle).toHaveBeenCalledWith('sample', false)
  expect(monitorDelete).not.toHaveBeenCalled()
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

it('shows independent signal-tab counts and unknown sell archives in the result heading', async () => {
  detail.mockResolvedValue({ result: {
    as_of: '2026-10-23', strategy: 'sample', rows: [], total: 0, section_mode: 'signals',
    sections: { sell: [], select: [{ symbol: 'SELECT1' }, { symbol: 'SELECT2' }], buy: [] },
    section_errors: { sell: '缺少前交易日买入信号归档' },
  }, today_ever_rows: {} })
  await render()
  await act(async () => button('样例').click())
  await flush()
  const heading = () => [...host.querySelectorAll('h2')].map(node => node.textContent).join('')
  expect(heading()).toContain('前日买入信号暂不可确认')
  await act(async () => host.querySelectorAll<HTMLButtonElement>('[role="tab"]')[1].click())
  expect(heading()).toContain('选股 2 只')
  expect(heading()).not.toContain('命中 0')
  await act(async () => host.querySelectorAll<HTMLButtonElement>('[role="tab"]')[2].click())
  expect(heading()).toContain('买入信号 0 只')
})

it.each([
  ['2026-11-04', '2026-11-05'],
  ['2026-11-06', '2026-11-09'],
  ['2026-09-30', '2026-10-08'],
])('allows the confirmed next trading day %s → %s without changing the default', async (today, next) => {
  dataState.latest = today
  calendar.mockImplementation(async base => ({ base_date: base, next_trading_day: next }))
  await render()
  const date = host.querySelector<HTMLInputElement>('[aria-label="测试日期"]')!
  expect(calendar).toHaveBeenCalledWith(today)
  expect(date.value).toBe(today)
  expect(date.max).toBe(next)
  await act(async () => button('下个交易日').click())
  await flush()
  expect(date.value).toBe(next)
  expect(run).not.toHaveBeenCalled()
  expect(runAll).not.toHaveBeenCalled()
})

it.each(['unknown', 'failed', 'mismatched'])('keeps the date limit at today when the calendar is %s', async mode => {
  dataState.latest = '2026-11-04'
  if (mode === 'failed') calendar.mockRejectedValue(new Error('calendar unavailable'))
  if (mode === 'mismatched') calendar.mockResolvedValue({
    base_date: '2026-11-03', next_trading_day: '2026-11-05',
  })
  await render()
  expect(host.querySelector<HTMLInputElement>('[aria-label="测试日期"]')?.max).toBe('2026-11-04')
  expect(button('下个交易日')).toBeUndefined()
  await setDate('2026-11-05')
  expect(host.querySelector<HTMLInputElement>('[aria-label="测试日期"]')?.value).toBe('2026-11-04')
})

function installNextSessionS02() {
  dataState.latest = '2026-11-04'
  clockState.data = { market_date: '2026-11-04', market_time: '2026-11-04T15:30:00+08:00', is_trading_day: true }
  poolState.ids.push('custom_mvxxcp88')
  calendar.mockImplementation(async base => ({ base_date: base, next_trading_day: '2026-11-05' }))
  summary.mockImplementation(async (day: string) => ({
    as_of: day, results: { custom_mvxxcp88: { as_of: day, total: 1 } }, today_ever_counts: {},
  }))
  detail.mockImplementation(async (id: string, _ext: string, day: string) => ({
    result: { as_of: day, strategy: id, rows: [], total: 0, section_mode: 'signals',
      selection_only: day === '2026-11-05',
      sections: { sell: [{ symbol: `${day}-SELL`, entry_date: day === '2026-11-05' ? '2026-11-04' : '2026-11-03' }],
        select: [{ symbol: `${day}-SELECT` }], buy: [] } }, today_ever_rows: {},
  }))
}

it('queries S02 next-session sell and select archives separately and restores current-day results', async () => {
  installNextSessionS02()
  await render()
  await act(async () => button('自定义S02').click())
  await flush()
  expect(host.querySelector('[role="tabpanel"]')?.textContent).toContain('2026-11-04-SELL')
  await act(async () => button('下个交易日').click())
  await flush()
  await flush()
  expect(detail).toHaveBeenCalledWith('custom_mvxxcp88', undefined, '2026-11-05', 'stock', '1m')
  expect(host.querySelector('[role="tabpanel"]')?.textContent).toContain('2026-11-05-SELL')
  expect(host.querySelector('[role="tabpanel"]')?.textContent).not.toContain('2026-11-04-SELL')
  await act(async () => host.querySelectorAll<HTMLButtonElement>('[role="tab"]')[1].click())
  expect(host.querySelector('[role="tabpanel"]')?.textContent).toContain('2026-11-05-SELECT')
  await setDate('2026-11-04')
  await flush()
  expect(host.querySelector('[role="tabpanel"]')?.textContent).toContain('2026-11-04-SELL')
  await act(async () => host.querySelectorAll<HTMLButtonElement>('[role="tab"]')[1].click())
  expect(host.querySelector('[role="tabpanel"]')?.textContent).toContain('2026-11-04-SELECT')
  expect(run).not.toHaveBeenCalled()
  expect(runAll).not.toHaveBeenCalled()
  expect(reload).not.toHaveBeenCalled()
})

it('keeps the loaded S02 archive when the same next-session date is selected again', async () => {
  installNextSessionS02()
  await render()
  await act(async () => button('自定义S02').click())
  await flush()
  await act(async () => button('下个交易日').click())
  await flush()
  await flush()
  expect(host.querySelector('[role="tabpanel"]')?.textContent).toContain('2026-11-05-SELL')
  await act(async () => button('下个交易日').click())
  await flush()
  expect(host.querySelector('[role="tabpanel"]')?.textContent).toContain('2026-11-05-SELL')
  expect(host.textContent).not.toContain('该交易日归档尚未生成')
  expect(run).not.toHaveBeenCalled()
  expect(runAll).not.toHaveBeenCalled()
})

it('ignores a late current-day archive after switching S02 to the next session', async () => {
  installNextSessionS02()
  let finish!: (value: any) => void
  detail.mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
  await render()
  await act(async () => button('自定义S02').click())
  await flush()
  await setDate('2026-11-05')
  await flush()
  await act(async () => finish({ result: { as_of: '2026-11-04', strategy: 'custom_mvxxcp88',
    rows: [{ symbol: 'LATE-CURRENT' }], total: 1 }, today_ever_rows: {} }))
  await flush()
  expect(host.querySelector('[role="tabpanel"]')?.textContent).toContain('2026-11-05-SELL')
  expect(host.textContent).not.toContain('LATE-CURRENT')
})

it('does not reload or execute when saving settings in a next-session query', async () => {
  installNextSessionS02()
  await render()
  await setDate('2026-11-05')
  const refresh = button('重载')
  expect(refresh.disabled).toBe(true)
  await act(async () => refresh.click())
  await act(async () => button('自定义S02').parentElement!.querySelector<HTMLButtonElement>('button[title="策略设置"]')!.click())
  await act(async () => button('保存测试参数').click())
  await flush()
  expect(run).not.toHaveBeenCalled()
  expect(runAll).not.toHaveBeenCalled()
  expect(reload).not.toHaveBeenCalled()
})

it('explains an absent next-session archive without offering current-day computation', async () => {
  installNextSessionS02()
  summary.mockImplementation(async (day: string) => ({ as_of: day, results: {}, today_ever_counts: {} }))
  detail.mockResolvedValue({ result: null })
  await render()
  await setDate('2026-11-05')
  await act(async () => button('自定义S02').click())
  await flush()
  expect(host.textContent).toContain('该交易日归档尚未生成')
  expect(host.textContent).toContain('卖／选／买')
  expect(host.textContent).not.toContain('请先运行盘后管道')
  expect(run).not.toHaveBeenCalled()
})

it.each([1, 2])('keeps selected signal tab %s when a newer archive record arrives', async index => {
  installNextSessionS02()
  await render()
  await act(async () => button('自定义S02').click())
  await flush()
  await act(async () => host.querySelectorAll<HTMLButtonElement>('[role="tab"]')[index].click())
  detail.mockImplementation(async (id: string, _ext: string, day: string) => ({
    result: { as_of: day, strategy: id, record_id: 'new-record', rows: [], total: 0,
      section_mode: 'signals', sections: { sell: [{ symbol: '600001.SH' }],
        select: [{ symbol: '600002.SH' }], buy: [{ symbol: '600003.SH' }] } },
    today_ever_rows: {},
  }))
  await act(async () => { await client.invalidateQueries({ queryKey: ['screener-cached', 'strategy'] }) })
  await flush()
  expect(host.querySelector('[aria-selected="true"]')?.textContent).toContain(index === 1 ? '选' : '买')
  expect(run).not.toHaveBeenCalled()
  expect(runAll).not.toHaveBeenCalled()
})
