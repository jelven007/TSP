import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api, type ScreenerResult } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { StockPreviewDialog } from '@/components/StockPreviewDialog'
import { toNavItems, type NavItem } from '@/lib/listNav'

export type StrategyResultTab = 'sell' | 'select' | 'buy'
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
    ['float_cap_1430', '流通市值(亿)'], ['prev_d_amount20', '20日均额(亿)'],
    ['market_breadth', '市场上涨比例'], ['breadth_denominator', '市场分母'],
    ['m1_rising', '1分钟MA5连续6升'], ['m5_rising', '5分钟MA5连续3升']],
  buy: [...common, ['rank', '顺位'], ['return_1430', '14:30涨幅'], ['entry_nav', '买入前净值'],
    ['entry_budget', '含费预算'], ['filled_shares', '买入股数'], ['buy_price', '买入均价'],
    ['buy_fee', '买入费'], ['buy_time', '成交时间'], ['participation', '分钟容量上限']],
}
const signalColumns: Record<Tab, readonly (readonly [string, string])[]> = {
  sell: [...common, ['entry_date', '买入信号日期'], ['last_datetime', '触发时间'],
    ['close', '触发价'], ['change_pct', '触发时涨幅'], ['score', '信号评分']],
  select: [...common, ['selection_rank', '排序'], ['reference_date', '数值参考日'],
    ['reference_close', '参考价'], ['reference_change_pct', '参考日涨幅'],
    ['reference_float_cap', '流通市值(亿)'], ['amount_mean_20d', '20日均额(亿)'],
    ['change_pct', '当日涨幅'], ['score', '选股评分']],
  buy: [...common, ['last_datetime', '触发时间'], ['close', '触发价'],
    ['change_pct', '触发时涨幅'], ['score', '信号评分'], ['volume', '分钟成交量'], ['amount', '分钟成交额']],
}
const signalEmpty: Record<Tab, string> = {
  sell: '前一交易日没有触发入场触发器的股票',
  select: '该交易日没有满足股票范围、数值过滤和入场叠加条件的股票',
  buy: '该交易日没有触发入场触发器的股票',
}
const labels: Record<string, string> = {
  net_stop: '扣费净止损', gross_target: '毛止盈', time_guard: '时间兜底',
  suspended: '停牌等待复牌', pending: '等待卖出指标',
}
function format(key: string, value: unknown) {
  if (value == null) return '—'
  if (typeof value === 'boolean') return value ? '满足' : '未满足'
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) return '—'
    if (['previous_return', 'return_1430', 'market_breadth', 'participation', 'change_pct', 'reference_change_pct'].includes(key)) return `${(value * 100).toFixed(2)}%`
    if (['float_cap_1430', 'prev_d_amount20'].includes(key)) return (value / 1e8).toFixed(2)
    if (['shares', 'sold_shares', 'remaining', 'filled_shares', 'rank', 'selection_rank', 'breadth_denominator'].includes(key)) return value.toLocaleString('zh-CN')
    return value.toFixed(2)
  }
  return labels[String(value)] ?? String(value)
}

export function StrategyDailyResults({ result, positionLimit, onPreview, activeTab, onTabChange }: {
  result: ScreenerResult
  positionLimit?: number
  onPreview?: (symbol: string, name: string | undefined, navList: NavItem[]) => void
  activeTab?: Tab
  onTabChange?: (tab: Tab) => void
}) {
  const [localActive, setLocalActive] = useState<Tab>('sell')
  const active = activeTab ?? localActive
  const [preview, setPreview] = useState<{ symbol: string; name?: string; navList: NavItem[] } | null>(null)
  const section = result.sections ?? { sell: [], select: [], buy: [] }
  const signals = result.section_mode === 'signals'
  // 已保存结果按当时参数排序, 不用后来修改的策略设置覆盖历史口径。
  const saved = useQuery({
    queryKey: QK.screenerHistoryDetail(result.strategy ?? '', result.record_id ?? ''),
    queryFn: () => api.screenerResultHistoryDetail(result.strategy!, result.record_id!),
    enabled: !signals && positionLimit == null && !!result.record_id && !!result.strategy,
  })
  const current = useQuery({
    queryKey: QK.strategyDetail(result.strategy ?? ''),
    queryFn: () => api.strategyGet(result.strategy!),
    enabled: !signals && positionLimit == null && !result.record_id && !!result.strategy,
  })
  const configured = positionLimit ?? Number(saved.data?.summary.overrides?.position_limit
    ?? saved.data?.summary.params?.position_limit ?? current.data?.params_defaults?.position_limit ?? 5)
  const limit = Number.isFinite(configured) && configured > 0 ? configured : 5
  const selected = useMemo(() => {
    if (signals) return section.select.map((row, i) => ({ ...row, selection_rank: i + 1 }))
    const key = section.select.length > limit ? 'return_1430' : 'prev_d_amount20'
    const metric = (r: any) => typeof r[key] === 'number' && Number.isFinite(r[key]) ? r[key] : -Infinity
    return [...section.select].sort((a, b) => {
      const x = metric(a), y = metric(b)
      return (x === y ? 0 : x > y ? -1 : 1) || String(a.symbol).localeCompare(String(b.symbol))
    }).map((row, i) => ({ ...row, selection_rank: i + 1 }))
  }, [section.select, limit, signals])
  const rows = active === 'select' ? selected : section[active] ?? []
  const openStock = (row: any) => {
    const navList = toNavItems(rows.filter(item => typeof item.symbol === 'string' && item.symbol))
    if (onPreview) onPreview(row.symbol, row.name ?? undefined, navList)
    else setPreview({ symbol: row.symbol, name: row.name ?? undefined, navList })
  }
  const config = tabs.find(tab => tab.id === active)!
  const displayedColumns = signals ? signalColumns[active] : columns[active]
  const sectionError = result.section_errors?.[active]
  return <><section className="space-y-3 min-w-0">
    <div className="flex flex-wrap items-center justify-between gap-2">
      <div role="tablist" aria-label="每日策略结果" className="inline-flex rounded-btn border border-border p-1 gap-1">
        {tabs.map(tab => <button key={tab.id} role="tab" aria-selected={active === tab.id}
          aria-controls={`daily-${result.strategy}-${tab.id}`} id={`daily-tab-${result.strategy}-${tab.id}`}
          onClick={() => { setLocalActive(tab.id); onTabChange?.(tab.id) }}
          className={`px-4 py-1.5 rounded-btn text-sm ${active === tab.id ? 'bg-accent text-white' : 'text-secondary hover:bg-elevated'}`}>
          {tab.label} · {signals && result.section_errors?.[tab.id] ? '待确认' : section[tab.id]?.length ?? 0}
        </button>)}
      </div>
      <span className="text-xs text-muted">{result.as_of} · 每交易日一份结果</span>
    </div>
    <p className="text-xs text-muted">
      {signals
        ? '卖：前一交易日的买入信号。选：当日股票范围＋数值过滤＋入场叠加条件通过的全部股票。买：当日入场触发器触发的股票。'
        : <>持仓来自连续策略研究组合。选为全部达标股票，买为容量、资金与持仓上限筛选后的成交结果。
          {active === 'select' && ` 选按买入优先级排列：${section.select.length > limit ? '14:30涨幅' : '20日均成交额'}降序，同值按代码排序。`}</>}
    </p>
    {result.execution_error && <p role="alert" className="text-sm text-danger rounded-btn bg-danger/10 p-3">
      {result.execution_error}
    </p>}
    {sectionError && <p role="alert" className="text-sm text-danger rounded-btn bg-danger/10 p-3">{sectionError}</p>}
    <div role="tabpanel" id={`daily-${result.strategy}-${active}`}
      aria-labelledby={`daily-tab-${result.strategy}-${active}`} className="overflow-x-auto rounded-btn border border-border">
      {rows.length === 0 ? <p className="p-8 text-sm text-muted text-center">
        {sectionError ? '该页签结果暂不可确认' : signals ? signalEmpty[active] : config.empty}</p>
        : <table className="w-full text-xs whitespace-nowrap text-left">
          <thead className="bg-elevated text-secondary"><tr>
            {displayedColumns.map(([key, label]) => <th key={key} className="p-3 font-medium">{label}</th>)}
          </tr></thead>
          <tbody>{rows.map((row, i) => <tr key={`${row.symbol}-${i}`} className="border-t border-border">
            {displayedColumns.map(([key]) => <td key={key} className="p-3 num">
              {(key === 'symbol' || key === 'name') && typeof row.symbol === 'string' && row.symbol && row[key]
                ? <button type="button" title="查看个股详情"
                    className="text-accent hover:underline focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent"
                    onClick={() => openStock(row)}>{format(key, row[key])}</button>
                : format(key, row[key])}
            </td>)}
          </tr>)}</tbody>
        </table>}
    </div>
  </section>
    {!onPreview && <StockPreviewDialog symbol={preview?.symbol ?? null} name={preview?.name}
      navList={preview?.navList} onClose={() => setPreview(null)}
      onNavigate={(symbol, name) => setPreview(current => current && ({ ...current, symbol, name }))} />}
  </>
}
