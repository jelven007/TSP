import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { useVirtualizer } from '@tanstack/react-virtual'
import { useQuery } from '@tanstack/react-query'
import { ArrowDown, ArrowUp, ArrowUpDown } from 'lucide-react'
import { api, type ScreenerResult, type S05EntrySort } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { StockPreviewDialog } from '@/components/StockPreviewDialog'
import { toNavItems, type NavItem } from '@/lib/listNav'
import { getSignals, signalCls } from '@/lib/stock-table'
import { defaultStrategyResultColumns, resolveStrategyResultColumns, STRATEGY_RESULT_WIDTHS, type StrategyResultTab } from '@/lib/strategy-result-columns'
import type { ColumnConfig } from '@/lib/list-columns'
import { useTableSort } from '@/components/stock-table/useTableSort'
import { StrategyEntryProgress } from './StrategyEntryProgress'
import { selectionWindow } from '@/lib/selection-clock'
import { S05_BRANCH_LABELS, S05_SORT_LABELS, s05SortLabel } from '@/lib/customSignals'

export type { StrategyResultTab } from '@/lib/strategy-result-columns'
type Tab = StrategyResultTab
const tabs: { id: Tab; label: string; empty: string }[] = [
  { id: 'sell', label: '卖', empty: '该交易日没有需要卖出的股票' },
  { id: 'select', label: '选', empty: '该交易日没有满足选股条件的股票' },
  { id: 'buy', label: '买', empty: '该交易日没有满足资金、持仓和成交容量条件的买入' },
]
const common = [['symbol', '股票代码'], ['name', '名称']] as const
const columns: Record<Tab, readonly (readonly [string, string])[]> = {
  sell: [...common, ['shares', '需卖股数'], ['entry_price', '买入均价'], ['net_stop_quote', '扣费净止损价'],
    ['gross_target_quote', '毛止盈价'], ['guard_time', '兜底时间'], ['sell_deadline', '截止时间'],
    ['reason', '卖出指标'], ['sold_shares', '已卖股数'], ['remaining', '剩余股数'], ['status', '状态']],
  select: [...common, ['selection_rank', '买入优先级'], ['previous_return', '前日涨幅'], ['return_1430', '14:30涨幅'],
    ['float_cap_1430', '流通市值(亿)'],
    ['market_breadth', '市场上涨比例'], ['breadth_denominator', '市场分母'],
    ['m1_rising', '1分钟MA5连续6升'], ['m5_rising', '5分钟MA5连续3升']],
  buy: [...common, ['rank', '顺位'], ['return_1430', '14:30涨幅'], ['entry_nav', '买入前净值'],
    ['entry_budget', '含费预算'], ['filled_shares', '买入股数'], ['buy_price', '买入均价'],
    ['buy_fee', '买入费'], ['buy_time', '成交时间'], ['participation', '分钟容量上限']],
}
const signalEmpty: Record<Tab, string> = {
  sell: '前一交易日没有触发入场触发器的股票',
  select: '该交易日没有满足股票范围和数值过滤的股票',
  buy: '该交易日没有触发入场触发器的股票',
}
const labels: Record<string, string> = {
  net_stop: '扣费净止损', gross_target: '毛止盈', time_guard: '时间兜底',
  suspended: '停牌等待复牌', pending: '等待卖出指标',
}
const s02Branches: Record<string, string> = {
  tail: '尾段加速', low_gap: '低缺口资金趋势', board: '板块强势综合',
}
const s05Branches = Object.keys(s02Branches)
const s05ExitId = (row: Record<string, unknown>) => {
  // Older sell rosters carry the actual entry signal column instead of entry_branch.
  const branches = s05Branches.filter(branch =>
    row.entry_branch === branch || row[`csgi_${branch}_entry_s05`] === true)
  return branches.length === 1 ? `${branches[0]}_exit_s05` : undefined
}
const s05DefaultSort: Record<string, S05EntrySort> = {
  tail: 'return_5m', low_gap: 'fund_tech', board: 'four_dimension',
}
const s05NoMarketRoute = (result: ScreenerResult) =>
  result.strategy === 'custom_mvxxcs05'
  && result.selection_snapshot?.as_of === result.as_of
  && result.selection_snapshot.entry_branch === null
  && ['s05_market_route_v2', 's05_daily_branch_v3'].includes(result.selection_snapshot.entry_route?.schema ?? '')
  && result.selection_snapshot.entry_route?.matched.length === 0
  && !result.sections?.buy.length
const finiteNumber = (value: unknown): value is number =>
  typeof value === 'number' && Number.isFinite(value)
function format(key: string, value: unknown) {
  if (value == null) return '—'
  if (key === 'entry_branch') return s02Branches[String(value)] ?? '—'
  if (key === 'suggested_sell_price') {
    if (typeof value === 'number') return Number.isFinite(value) && value > 0 ? value.toFixed(2) : '—'
    if (typeof value !== 'object') return '—'
    const { lower, upper } = value as { lower?: unknown; upper?: unknown }
    return typeof lower === 'number' && Number.isFinite(lower) && lower > 0
      && typeof upper === 'number' && Number.isFinite(upper) && upper >= lower
      ? `${lower.toFixed(2)}～${upper.toFixed(2)}` : '—'
  }
  if (typeof value === 'boolean') return value ? '满足' : '未满足'
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) return '—'
    if (['previous_return', 'return_1430', 'market_breadth', 'participation', 'change_pct', 'current_return', 'current_range', 'reference_change_pct', 'live_change_pct'].includes(key)) return `${(value * 100).toFixed(2)}%`
    if (key === 'atr14_previous_close') return `${(value * 100).toFixed(2)}%`
    if (['live_turnover', 'reference_turnover_rate'].includes(key)) return value >= 0 ? `${value.toFixed(2)}%` : '—'
    if (['volume', 'amount', 'suggested_lots_100k'].includes(key)) return value.toLocaleString('zh-CN', { maximumFractionDigits: 0 })
    if (key === 'float_cap_1430') return (value / 1e8).toFixed(2)
    if (['shares', 'sold_shares', 'remaining', 'filled_shares', 'rank', 'selection_rank', 'breadth_denominator', 'atr_rank', 's02_rank'].includes(key)) return value.toLocaleString('zh-CN')
    return value.toFixed(2)
  }
  return labels[String(value)] ?? String(value)
}

/** 标签及摘要只读取保存结果；切换卖/选/买不会在这里重算策略。 */
export function StrategyDailyResultToolbar({ result, activeTab, onTabChange, currentDate, inline = false }: {
  result: ScreenerResult
  activeTab: Tab
  onTabChange: (tab: Tab) => void
  currentDate?: string
  inline?: boolean
}) {
  const section = result.sections ?? { sell: [], select: [], buy: [] }
  const signals = result.section_mode === 'signals'
  const today = currentDate ?? new Date().toLocaleDateString('sv-SE', { timeZone: 'Asia/Shanghai' })
  const historical = result.as_of < today
  const breadthQuality = Object.values(result.entry_data_quality ?? {}).find(quality =>
    !quality.progress?.as_of || quality.progress.as_of === result.as_of)
  const breadth = breadthQuality?.progress?.steps.breadth?.up_pct ?? breadthQuality?.pool_up_pct
  const s03Inline = inline && result.strategy === 'custom_mvxxcs03'
  return <div className={s03Inline ? 'flex shrink-0 items-center gap-2'
    : inline ? 'flex min-w-0 flex-1 basis-[640px] flex-wrap items-center gap-2'
    : 'flex flex-wrap items-center justify-between gap-2'}>
    <div role="tablist" aria-label="每日策略结果" className="inline-flex shrink-0 rounded-btn border border-border p-0.5">
      {tabs.map(tab => <button key={tab.id} role="tab" aria-selected={activeTab === tab.id}
        aria-controls={`daily-${result.strategy}-${tab.id}`} id={`daily-tab-${result.strategy}-${tab.id}`}
        onClick={() => onTabChange(tab.id)}
        className={`h-7 px-3 rounded-btn text-xs ${activeTab === tab.id ? 'bg-accent text-white' : 'text-secondary hover:bg-elevated'}`}>
        {tab.label} · {tab.id === 'buy' && s05NoMarketRoute(result) ? '无分支'
          : signals && tab.id === 'buy' && result.monitor_tasks?.buy?.published === true
          ? section.buy?.length ?? 0
          : signals && result.section_statuses?.[tab.id] === 'unavailable' ? '未归档'
          : signals && result.section_statuses?.[tab.id] === 'failed' ? '失败'
          : signals && result.section_statuses?.[tab.id] === 'expired' ? '未确认'
          : signals && result.section_errors?.[tab.id] ? '待确认' : section[tab.id]?.length ?? 0}
      </button>)}
    </div>
    {result.strategy === 'custom_mvxxcs01' && <span aria-label="上涨占比" className="strategy-result-note shrink-0 whitespace-nowrap">
      上涨占比 · {typeof breadth === 'number' && Number.isFinite(breadth) ? `${breadth.toFixed(2)}%` : '—'}
    </span>}
    {signals && result.strategy === 'custom_mvxxcp88'
      ? <StrategyEntryProgress result={result} today={today} />
      : <span className={`strategy-result-note ${inline ? 'shrink-0 whitespace-nowrap' : ''}`}>{result.as_of} · 每交易日一份结果
        {signals && (historical ? ' · 历史保存结果' : result.as_of === today ? ' · 完成结果固定' : ' · 已保存预选')}</span>}
  </div>
}

/** 名单沿冻结字段展示；实时价格只作当前参考，信号名单不能当成实际成交。 */
export function StrategyDailyResults({ result, onPreview, activeTab, onTabChange, signalColumns, currentDate, hideToolbar = false }: {
  result: ScreenerResult
  positionLimit?: number
  onPreview?: (symbol: string, name: string | undefined, navList: NavItem[]) => void
  activeTab?: Tab
  onTabChange?: (tab: Tab) => void
  signalColumns?: ColumnConfig[]
  currentDate?: string
  hideToolbar?: boolean
}) {
  const [localActive, setLocalActive] = useState<Tab>(result.selection_only ? 'select' : 'sell')
  const active = activeTab ?? localActive
  const [preview, setPreview] = useState<{ symbol: string; name?: string; navList: NavItem[] } | null>(null)
  const section = useMemo(() => result.sections ?? { sell: [], select: [], buy: [] }, [result.sections])
  const signals = result.section_mode === 'signals'
  const s01 = result.strategy === 'custom_mvxxcs01'
  const s03 = result.strategy === 'custom_mvxxcs03'
  const s05 = result.strategy === 'custom_mvxxcs05'
  const s05SignalPage = s05 && active !== 'select' && signals
  const s05Projected = s05SignalPage && section[active].some(row => Object.hasOwn(row, 'signal_score'))
  const noMarketRoute = s05NoMarketRoute(result)
  const routedS02 = s05
    && result.selection_snapshot?.entry_routing === 's02_173004552_v1'
  const branch = (routedS02 ? result.selection_snapshot?.entry_branch : undefined)
    ?? section.buy[0]?.entry_branch
  const entryQuality = s05 && active === 'buy'
    ? Object.values(result.entry_data_quality ?? {}).find(quality => quality.entry_branch === branch && quality.sort_by)
    : undefined
  const archivedSort = entryQuality?.sort_by ?? section.buy[0]?.entry_sort_by
  const entrySort: S05EntrySort | undefined = !s05Projected && s05 && active === 'buy' && branch
    ? (archivedSort && Object.hasOwn(S05_SORT_LABELS, archivedSort) ? archivedSort : s05DefaultSort[String(branch)])
    : undefined
  const entrySortId = `builtin:entry_sort_${entrySort}`
  const fundWeight = entryQuality?.fund_weight_pct ?? section.buy[0]?.entry_sort_fund_weight_pct
  const showEntryBranch = active === 'sell'
    ? s05 && section.sell.some(row => row.entry_branch in s02Branches)
    : active === 'buy' && s05 && !!branch
  const calculateSellSuggestion = signals && active === 'sell' && (s03 || s05)
  const strategy = useQuery({
    queryKey: QK.strategyDetail(result.strategy ?? ''),
    queryFn: () => api.strategyGet(result.strategy!),
    enabled: calculateSellSuggestion && section.sell.length > 0,
    staleTime: 60_000,
  })
  const exitSignals = useQuery({
    queryKey: QK.customSignals,
    queryFn: () => api.customSignalsList(),
    enabled: calculateSellSuggestion && section.sell.length > 0,
    staleTime: 60_000,
  })
  const linkedExits = useMemo(() => exitSignals.data?.signals.filter(signal =>
    strategy.data?.exit_signals.includes(`csgi_${signal.id}`) && signal.enabled
      && signal.kind !== 'entry' && signal.template === 'morning_liquidation') ?? [],
    [exitSignals.data, strategy.data])
  const suggestedSellPrice = (row: Record<string, unknown>) => {
    const exitId = s05 ? s05ExitId(row) : undefined
    const exitSignal = s05 ? linkedExits.find(signal => signal.id === exitId) : linkedExits[0]
    const takeProfit = exitSignal?.params && 'take_profit_pct' in exitSignal.params
      ? exitSignal.params.take_profit_pct : null
    // 出场计算会用卖出触发价覆盖 close，原买入触发价保存在 entry_signal_price。
    const price = 'entry_signal_price' in row ? row.entry_signal_price
      : row.holding_exit ? null : row.close
    return typeof price === 'number' && Number.isFinite(price) && price > 0
      && typeof takeProfit === 'number' && Number.isFinite(takeProfit) && takeProfit > 0
      ? price * (1 + takeProfit / 100) : null
  }
  const today = currentDate ?? new Date().toLocaleDateString('sv-SE', { timeZone: 'Asia/Shanghai' })
  const historical = result.as_of < today
  const selected = useMemo(() => {
    if (signals) {
      const selectedRows = section.select.map((row, i) => ({
      ...row, selection_rank: row.selection_rank ?? i + 1,
      ...(result.strategy === 'custom_mvxxcp88' ? {
        reference_turnover_rate: 'reference_turnover_rate' in row ? row.reference_turnover_rate
          : typeof row.date === 'string' && row.date.slice(0, 10) === row.reference_date
            && row.reference_date !== result.as_of ? row.turnover_rate : null,
      } : {}),
      }))
      if (s03) return selectedRows.sort((a, b) => a.selection_rank - b.selection_rank)
      if (routedS02) return selectedRows.sort((a, b) =>
        (a.s02_rank ?? Infinity) - (b.s02_rank ?? Infinity) || String(a.symbol).localeCompare(String(b.symbol)))
      if (result.strategy !== 'custom_mvxxcp88') return selectedRows
      const metric = (row: (typeof selectedRows)[number]) => typeof row.atr14_previous_close === 'number'
        && Number.isFinite(row.atr14_previous_close) ? row.atr14_previous_close : -Infinity
      return selectedRows.sort((a, b) => {
        const x = metric(a), y = metric(b)
        return (x === y ? 0 : x > y ? -1 : 1) || String(a.symbol).localeCompare(String(b.symbol))
      })
    }
    const key = 'return_1430'
    const metric = (r: any) => typeof r[key] === 'number' && Number.isFinite(r[key]) ? r[key] : -Infinity
    return [...section.select].sort((a, b) => {
      const x = metric(a), y = metric(b)
      return (x === y ? 0 : x > y ? -1 : 1) || String(a.symbol).localeCompare(String(b.symbol))
    }).map((row, i) => ({ ...row, selection_rank: i + 1 }))
  }, [section.select, signals, result.strategy, result.as_of, routedS02, s03])
  const rows = useMemo(() => {
    if (active === 'select') return selected
    const saved = section[active] ?? []
    if (s05Projected) {
      const score = (value: unknown) => finiteNumber(value) ? value : -Infinity
      return [...saved].sort((a, b) =>
        score(b.signal_score) - score(a.signal_score)
        || (a.s02_rank ?? Infinity) - (b.s02_rank ?? Infinity)
        || String(a.symbol).localeCompare(String(b.symbol)))
    }
    if (entrySort && saved.some(row => finiteNumber(row.entry_sort_value))) {
      return [...saved].sort((a, b) =>
        (finiteNumber(b.entry_sort_value) ? b.entry_sort_value : -Infinity)
          - (finiteNumber(a.entry_sort_value) ? a.entry_sort_value : -Infinity)
        || (a.s02_rank ?? Infinity) - (b.s02_rank ?? Infinity)
        || String(a.symbol).localeCompare(String(b.symbol)))
    }
    if (!signals || !s03 || active !== 'buy') return saved
    return [...saved].sort((a, b) =>
      (a.entry_priority ?? Infinity) - (b.entry_priority ?? Infinity)
        || String(a.symbol).localeCompare(String(b.symbol)))
  }, [active, selected, section, signals, s03, entrySort, s05Projected])
  const fiveDimensions = result.selection_snapshot?.scoring_policy === 's03_five_dimensions_v1'
  const preselectionWindow = selectionWindow(result.strategy)
  const atrRanking = signals && !s01 && !s03 && !s05
    && (active === 'select' || rows.some(row => row.atr14_previous_close != null))
  const symbols = useMemo(() => [...new Set(rows.map(row => row.symbol)
    .filter((symbol): symbol is string => typeof symbol === 'string' && /^[0-9]{6}\.(SH|SZ|BJ)$/.test(symbol)))].sort(), [rows])
  const live = useQuery({
    queryKey: QK.strategyResultLive(result.strategy ?? '', result.as_of, active, symbols),
    queryFn: async () => {
      const batches = []
      // 编码后的完整股票列表可能超出网关 URL 上限，分批覆盖所有成员。
      for (let i = 0; i < symbols.length; i += 400) batches.push(symbols.slice(i, i + 400))
      const snapshots = await Promise.all(batches.map(batch => api.marketSnapshot({ realtime: true, symbols: batch })))
      return snapshots
    },
    enabled: signals && !historical && result.as_of === today && symbols.length > 0,
    refetchInterval: false,
    refetchOnWindowFocus: false,
    staleTime: 0,
  })
  const acceptedSnapshots = useMemo(() => {
    return !historical ? live.data?.filter(snapshot => snapshot.as_of === today
      || snapshot.source === 'previous_close' && snapshot.requested_as_of === today
        && snapshot.as_of != null && snapshot.as_of < today) ?? [] : []
  }, [live.data, historical, today])
  const liveRows = useMemo(() => new Map(acceptedSnapshots
    .flatMap(snapshot => snapshot.rows).map(row => [row.symbol, row])), [acceptedSnapshots])
  const closedQuoteDates = [...new Set(acceptedSnapshots.filter(snapshot => snapshot.source === 'previous_close')
    .map(snapshot => snapshot.as_of))].join('、')
  const liveValue = (key: string, symbol: string) => {
    const quote = liveRows.get(symbol)
    const price = quote?.raw_close
    const validPrice = typeof price === 'number' && Number.isFinite(price) && price > 0 ? price : null
    if (key === 'live_price') return validPrice
    if (key === 'live_change_pct') return quote?.change_pct
    if (key === 'live_turnover') return quote?.turnover_rate
    if (key === 'live_vol_ratio') return quote?.vol_ratio_5d
    return null
  }
  const baseSortColumns = signals
    ? resolveStrategyResultColumns(active, result.strategy,
      signalColumns ?? defaultStrategyResultColumns(active, result.strategy)).filter(col => col.visible && col.source.type === 'builtin'
      && (atrRanking || !['atr14_previous_close', 'atr_rank', 'atr_rank_score'].includes(col.source.key))
      && (fiveDimensions || !['tech_score', 'fund_score', 'emotion_score', 'fundamental_score', 'news_score'].includes(col.source.key)))
    : []
  const sortColumns: ColumnConfig[] = showEntryBranch ? [
    ...baseSortColumns.slice(0, 2),
    { id: 'builtin:entry_branch', source: { type: 'builtin', key: 'entry_branch' },
      label: '入场分支', visible: true, pinned: false, align: 'right' },
    ...(entrySort ? [{ id: entrySortId, source: { type: 'builtin' as const, key: 'entry_sort_value' },
      label: S05_SORT_LABELS[entrySort], visible: true, pinned: false, align: 'right' as const }] : []),
    ...baseSortColumns.slice(2).filter(col => !col.id.startsWith('builtin:atr')),
  ] : baseSortColumns
  const { sort, toggle, sortRows } = useTableSort<(typeof rows)[number]>((row, col) => {
    if (col.source.type !== 'builtin') return null
    const key = col.source.key
    const value = key === 'score' && s05SignalPage ? row.signal_score
      : key.startsWith('live_') ? liveValue(key, row.symbol) : row[key]
    if (typeof value === 'number') return Number.isFinite(value) ? value : null
    return typeof value === 'string' || typeof value === 'boolean' ? value : null
  })
  const activeSort = sortColumns.some(col => col.id === sort?.key) ? sort : null
  const defaultAtrSort = signals && !activeSort && active === 'select' && result.strategy === 'custom_mvxxcp88'
  const defaultEntrySort = !activeSort && !!entrySort && rows.some(row => finiteNumber(row.entry_sort_value))
  const defaultSignalSort = !activeSort && s05Projected && rows.some(row => finiteNumber(row.signal_score))
  const scoreBranch = active === 'buy' ? result.selection_snapshot?.branch_code : undefined
  const currentScoreRule = scoreBranch === 1 ? '近5分钟涨幅'
    : scoreBranch === 2 ? '70%资金分＋30%技术分' : scoreBranch === 3 ? '四维综合分' : null
  const displayedRows = sortRows(rows, sortColumns)
  const panelRef = useRef<HTMLDivElement>(null)
  const sectionRef = useRef<HTMLElement>(null)
  const [panelHeight, setPanelHeight] = useState<number>()
  const virtualized = displayedRows.length > 100
  const fillViewport = ['custom_mvxxcp88', 'custom_mvxxcs01'].includes(result.strategy ?? '') && virtualized
  useLayoutEffect(() => {
    if (!fillViewport || !panelRef.current || !sectionRef.current) return
    const updateHeight = () => {
      const panel = panelRef.current
      if (!panel) return
      // 占满工具栏／说明下方的可用页面高度，保留原滚动与虚拟渲染。
      const top = panel.getBoundingClientRect().top + (panel.closest('main')?.scrollTop ?? 0)
      const next = Math.max(32, window.innerHeight - top - 12)
      setPanelHeight(current => current === next ? current : next)
    }
    updateHeight()
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(updateHeight)
    observer?.observe(sectionRef.current)
    window.addEventListener('resize', updateHeight)
    return () => {
      observer?.disconnect()
      window.removeEventListener('resize', updateHeight)
    }
  }, [fillViewport, active])
  const virtualizer = useVirtualizer({
    count: virtualized ? displayedRows.length : 0,
    getScrollElement: () => panelRef.current,
    estimateSize: () => 32,
    overscan: 8,
    scrollMargin: 32,
  })
  const virtualRows = virtualized ? virtualizer.getVirtualItems() : []
  const visibleRows = virtualized ? virtualRows.map(item => ({
    row: displayedRows[item.index], index: item.index,
  })) : displayedRows.map((row, index) => ({ row, index }))
  const topPadding = virtualRows.length ? Math.max(0, virtualRows[0].start - 32) : 0
  const bottomPadding = virtualized
    ? virtualizer.getTotalSize() - ((virtualRows.at(-1)?.end ?? 32) - 32) : 0
  useEffect(() => {
    if (panelRef.current) panelRef.current.scrollTop = 0
  }, [result.as_of, result.strategy, active])
  const openStock = (row: any) => {
    const navList = toNavItems(displayedRows.filter(item => typeof item.symbol === 'string' && item.symbol))
    if (onPreview) onPreview(row.symbol, row.name ?? undefined, navList)
    else setPreview({ symbol: row.symbol, name: row.name ?? undefined, navList })
  }
  const config = tabs.find(tab => tab.id === active)!
  const displayedColumns = signals
    ? sortColumns
      .map(col => [col.source.type === 'builtin' ? col.source.key : '',
        col.source.type === 'builtin' && col.source.key === 'score'
          ? atrRanking && active !== 'select' ? '排名分数'
            : !fiveDimensions && active === 'select' && result.strategy === 'custom_mvxxcs03' ? '选股评分' : col.label
          : col.label] as const)
    : columns[active]
  const width = (key: string) => STRATEGY_RESULT_WIDTHS[key] ?? 110
  const sectionError = result.section_errors?.[active]
  const statusLabels = { success: '成功', waiting: '等待数据', failed: '失败',
    retrying: '重试中', deferred: '延迟处理', disabled: '停用', expired: '买入未确认，已过入场时限' }
  const executionStatus = result.execution_status
  const resultNotes = [
    s05SignalPage && active === 'sell' ? '信号评分沿用前一交易日的买入评分。' : null,
    s05SignalPage && active === 'buy' && currentScoreRule ? `信号评分：${currentScoreRule}降序` : null,
    s05 && result.selection_snapshot?.branch_code != null
      ? `交易日分支参数：${result.selection_snapshot.branch_code} · ${S05_BRANCH_LABELS[result.selection_snapshot.branch_code]} · ${result.as_of}`
      : null,
    noMarketRoute && active !== 'sell'
      ? '三种市场分支均未满足，本交易日不新买' : null,
    s05 && active === 'sell' && result.monitor_tasks?.sell?.completion_reason === 's05_no_daily_branch'
      ? '此日期的卖任务为旧规则归档，未按新规则执行；下方仍显示前一交易日买入关注名单' : null,
    routedS02 && active !== 'sell' && result.selection_snapshot?.entry_branch
      ? `当日分支：${s02Branches[result.selection_snapshot.entry_branch]} · 前50名参与入场计算`
      : null,
    entrySort ? `${archivedSort ? '买入排序' : '分支默认排序'}：${s05SortLabel(entrySort, fundWeight)}降序`
      : null,
    signals && result.as_of > today
      ? `${result.as_of} · 下一交易日归档查询：可查看已保存的卖／选／买，开启监控后，选名单在前一交易日${preselectionWindow}生成；买入信号等待该交易日运行。`
      : null,
    executionStatus && (executionStatus.state !== 'success' || executionStatus.reason)
      ? `${statusLabels[executionStatus.state]}${executionStatus.reason ? ` · ${executionStatus.reason}` : ''}${Number.isFinite(executionStatus.updated_at)
        ? ` · ${new Date(executionStatus.updated_at).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' })}` : ''}`
      : null,
    result.showing_last_success && result.last_success_at != null
      ? `当前展示上次成功结果 · 成功时间 ${new Date(result.last_success_at).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' })}`
      : null,
  ].filter(Boolean)
  return <><section ref={sectionRef} className="space-y-2 min-w-0">
    {!hideToolbar && <StrategyDailyResultToolbar result={result} activeTab={active} currentDate={today}
      onTabChange={tab => { setLocalActive(tab); onTabChange?.(tab) }} />}
    {resultNotes.length > 0 && <p role="status" className="strategy-result-note">{resultNotes.join(' · ')}</p>}
    {signals && result.strategy === 'custom_mvxxcp88' && !historical && closedQuoteDates
      && <p className="strategy-result-note">行情日期：{closedQuoteDates} · 休市，显示最近交易日收盘数据。</p>}
    {!['custom_mvxxcp88', 'custom_mvxxcs01', 'custom_mvxxcs03'].includes(result.strategy ?? '') && <details className="strategy-result-note">
      <summary className="strategy-result-disclosure">策略说明</summary>
      <div className="space-y-1 py-1">
    <p>
      {signals
        ? `卖：前一交易日成功计算的买入信号。选：开启监控后，在前一交易日${preselectionWindow}${result.strategy === 'custom_mvxxcs03'
          ? `读取S02在${selectionWindow('custom_mvxxcp88')}完成的预选名单，按五维综合分降序取前30；历史结果以保存口径为准`
          : '按股票范围和数值过滤生成，按策略评分降序保存'}。买：按入场触发器分阶段计算，完成后固定结果并同步下一交易日的卖名单。`
        : <>持仓来自连续策略研究组合。选为全部达标股票，买为容量、资金与持仓上限筛选后的成交结果。
          {active === 'select' && ' 选按14:30涨幅降序，同值按代码排序。'}</>}
    </p>
    {atrRanking && <p>
      {active === 'select'
        ? '预选按ATR14÷昨收降序排名，同值按代码升序。排名分数=100×(N-r)/(N-1)，N为可计算股票数，r为顺位；第一名100、末名0、单只50。缺完整日K时显示“—”，与选股评分分别保存。'
        : '实际命中按ATR14÷昨收降序排名，同值按触发涨幅降序、代码升序，排名分数为0～100分。'}
      {active === 'sell' && ' 卖页继承前一交易日买入信号的排名与分数。'}
    </p>}
    {fiveDimensions && <p>
      综合分＝技术×40%＋资金×20%＋情绪×20%＋基本面×10%＋消息×10%，各维度0～100分；
      同分按股票代码升序，最多30只。S02候选{result.selection_snapshot?.coverage?.candidate_total ?? '—'}只，
      可评分{result.selection_snapshot?.coverage?.scorable_total ?? '—'}只。
      评分缺失逐股记录，不补造分数；原有买卖触发器按保存配置执行。
    </p>}
    {signals && result.selection_snapshot && <p>
      数值参考日：{result.selection_snapshot.source_date} · 生成于
      {' '}{new Date(result.selection_snapshot.generated_at).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' })}
      {result.selection_only && !noMarketRoute && ' · 仅预选，买入信号尚未计算'}
    </p>}
    {signals && <p>
      {historical ? '历史结果按保存字段展示；现价等当前行情列不参与历史读取。'
        : closedQuoteDates ? `行情日期：${closedQuoteDates} · 休市，显示最近交易日收盘数据。`
        : '现价等行情列取当日最新数据，缺失时显示“—”。'}
      {active === 'select' && result.strategy === 'custom_mvxxcp88' && ' 参考日换手率取前一交易日，与右侧当日行情换手率分别显示。'}
      {s01 && active !== 'select' && ' 当日涨幅、振幅、累计成交额和评分取买入信号的已完成分钟快照。卖页继承前交易日保存字段，实际持仓出场另按出场触发器计算。'}
      {!s01 && active === 'sell' && (s05
        ? ' 分钟成交量、成交额来自保存信号的触发分钟；建议卖出价＝买入信号触发价×（1＋对应出场信号的止盈比例），缺少有效输入时显示“—”。'
        : ' 分钟成交量、成交额来自保存信号的触发分钟；建议卖出价为上一个交易日原始收盘价的94%～106%，缺少参考价时显示“—”。')}
      {!s01 && active === 'buy' && ' 建议购买量单位为手（100股），按保存的触发价计算10万元最大整手数，不含费用与滑点。'}</p>}
      </div>
    </details>}
    {signals && active === 'buy' && Object.entries(result.entry_data_quality ?? {})
      .filter(([, quality]) => (finiteNumber(quality.pool_up_min_pct) || result.strategy === 'custom_mvxxcp88')
        && ((quality.missing_symbols?.length ?? 0) > 0 || quality.stage === 'breadth_rejected')
        && (quality.breadth_policy === 'available_members_v1'
          || (finiteNumber(quality.pool_up_pct_lower) && finiteNumber(quality.pool_up_pct_upper))))
      .map(([signal, quality]) => <p key={signal} className="strategy-result-note">
        {quality.breadth_policy === 'available_members_v1' ? <>
          {!finiteNumber(quality.pool_up_pct) ? '无有效行情，等待数据' : `上涨比例 ${quality.pool_up_pct.toFixed(2)}%`}
          {' · '}{finiteNumber(quality.pool_up_min_pct)
            ? `阈值 ${quality.pool_up_min_pct.toFixed(2)}%` : '仅展示，不参与买入筛选'}
          {' · '}完整池 {quality.pool_total} 只 · 有效 {quality.pool_usable} 只 · 剔除 {quality.missing_symbols.length} 只
          {' · '}缺少有效指定分钟价格或昨收的股票不参与上涨占比计算
          {finiteNumber(quality.pool_up_min_pct) && quality.stage === 'breadth_rejected'
            && ' · 上涨占比未达阈值，市场条件未满足'}
        </> : <>上涨比例 {quality.pool_up_pct_lower!.toFixed(2)}%～{quality.pool_up_pct_upper!.toFixed(2)}%
        {' · '}完整池 {quality.pool_total} 只 · {quality.missing_symbols.length} 只行情未知
        {' · '}{!finiteNumber(quality.pool_up_min_pct) ? '仅展示，不参与买入筛选'
          : quality.pool_up_pct_lower! + 1e-10 >= quality.pool_up_min_pct
          ? '已按下界确认市场条件'
          : quality.stage === 'breadth_rejected' ? '上界未达阈值，市场条件未满足' : '区间跨过阈值，等待完整行情'}
        </>}
      </p>)}
    {result.execution_error && <p role="alert" className="strategy-result-note text-danger rounded-btn bg-danger/10 p-3">
      {result.execution_error}
    </p>}
    {sectionError && <p role="alert" className="strategy-result-note text-danger rounded-btn bg-danger/10 p-3">{sectionError}</p>}
    <div ref={panelRef} role="tabpanel" id={`daily-${result.strategy}-${active}`}
      aria-labelledby={`daily-tab-${result.strategy}-${active}`}
      className="overflow-auto rounded-btn border border-border"
      style={virtualized ? { maxHeight: fillViewport ? panelHeight : 480 } : undefined}>
      {rows.length === 0 ? <p className="p-8 text-sm text-muted text-center">
        {active === 'buy' && noMarketRoute ? '本交易日没有需要执行的买入分支'
          : sectionError ? '该页签结果暂不可确认' : signals ? signalEmpty[active] : config.empty}</p>
        : <table className="text-xs whitespace-nowrap text-left table-fixed"
            style={{ width: displayedColumns.reduce((sum, [key]) => sum + width(key), 0) }}>
          <colgroup>{displayedColumns.map(([key]) => <col key={key} style={{ width: width(key) }} />)}</colgroup>
          <thead className="bg-elevated text-secondary sticky top-0"><tr>
            {displayedColumns.map(([key, label]) => {
              const sortable = signals && !['live_signals', 'suggested_sell_price'].includes(key)
              const colId = sortColumns.find(col => col.source.type === 'builtin' && col.source.key === key)?.id
              const direction = activeSort?.key === colId ? activeSort?.dir
                : (defaultAtrSort && key === 'atr14_previous_close')
                  || (defaultEntrySort && key === 'entry_sort_value') || (defaultSignalSort && key === 'score') ? 'desc' : null
              const Icon = direction === 'asc' ? ArrowUp : direction === 'desc' ? ArrowDown : ArrowUpDown
              return <th key={key} data-column={key}
                aria-sort={sortable ? direction === 'asc' ? 'ascending' : direction === 'desc' ? 'descending' : 'none' : undefined}
                className="px-2 py-1.5 font-medium" style={{ width: width(key) }}>
                {sortable && colId ? <button type="button" aria-label={`${label}排序`}
                  className="inline-flex items-center gap-1 hover:text-accent focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent"
                  onClick={() => toggle(colId)}>
                  {label}<Icon aria-hidden="true" className={`h-3 w-3 shrink-0 ${direction ? 'text-accent' : 'text-muted'}`} />
                </button> : label}
              </th>
            })}
          </tr></thead>
          <tbody>
            {topPadding > 0 && <tr aria-hidden="true"><td colSpan={displayedColumns.length}
              style={{ height: topPadding, padding: 0, border: 0 }} /></tr>}
            {visibleRows.map(({ row, index: i }) => <tr key={`${row.symbol}-${i}`} data-row-index={i}
              className="border-t border-border" style={virtualized ? { height: 32 } : undefined}>
            {displayedColumns.map(([key]) => <td key={key} data-column={key} className="px-2 py-1.5 num overflow-hidden text-ellipsis">
              {key === 'live_signals' ? <span className="inline-flex gap-1" title={getSignals(liveRows.get(row.symbol) ?? {}).map(signal => signal.label).join('、')}>
                {getSignals(liveRows.get(row.symbol) ?? {}).map(signal => <span key={signal.label}
                  className={`rounded px-1 py-0.5 ${signalCls(signal.type)}`}>{signal.label}</span>)}
                {!getSignals(liveRows.get(row.symbol) ?? {}).length && '—'}
              </span> : key.startsWith('live_') ? format(key, liveValue(key, row.symbol))
              : (key === 'symbol' || key === 'name') && typeof row.symbol === 'string' && row.symbol && row[key]
                ? <button type="button" title="查看个股详情"
                    className="text-accent hover:underline focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent"
                    onClick={() => openStock(row)}>{format(key, row[key])}</button>
                : key === 'entry_sort_value' && entrySort === 'return_5m' && finiteNumber(row[key])
                  ? `${row[key].toFixed(2)}%`
                  : key === 'score' && s05SignalPage
                    ? (row.signal_score_rule === 'return_5m' && finiteNumber(row.signal_score)
                      ? `${row.signal_score.toFixed(2)}%` : format(key, row.signal_score))
                  : format(key, calculateSellSuggestion && key === 'suggested_sell_price'
                  ? suggestedSellPrice(row) : row[key])}
            </td>)}
          </tr>)}
            {bottomPadding > 0 && <tr aria-hidden="true"><td colSpan={displayedColumns.length}
              style={{ height: bottomPadding, padding: 0, border: 0 }} /></tr>}
          </tbody>
        </table>}
    </div>
  </section>
    {!onPreview && <StockPreviewDialog symbol={preview?.symbol ?? null} name={preview?.name}
      navList={preview?.navList} onClose={() => setPreview(null)}
      onNavigate={(symbol, name) => setPreview(current => current && ({ ...current, symbol, name }))} />}
  </>
}
