import { useEffect, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Activity, Clock, LineChart, Lock, RefreshCw } from 'lucide-react'
import { api, type IndexInstrument, type IndexKlinePeriod, type NullableMinuteKlineRow } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { useCapabilities, useCapabilityMatrix } from '@/lib/useSharedQueries'
import { routeCapUsable } from '@/lib/capability-labels'
import { INDEX_KLINE_PERIODS, KLINE_PERIOD_LABELS } from '@/lib/klinePeriods'
import { hasPermission } from '@/lib/accountSession'
import { StockDailyKChart } from '@/components/StockDailyKChart'
import { EChartsIntraday } from '@/components/EChartsIntraday'
import { PageHeader } from '@/components/PageHeader'
import { DatePicker } from '@/components/DatePicker'
import { SettingsModal } from '@/components/data/SettingsModal'
import { RepairDailyPanel } from '@/components/data/RepairDailyPanel'
import { ActiveJobCard } from '@/components/data/ActiveJobCard'

function defaultRange() {
  const now = new Date()
  return { start: '', end: now.toISOString().slice(0, 10) }
}

function fmtPct(v: number | null | undefined) {
  if (v == null || Number.isNaN(Number(v))) return '--'
  return `${Number(v).toFixed(2)}%`
}

function fmtNum(v: number | null | undefined, digits = 2) {
  if (v == null || Number.isNaN(Number(v))) return '--'
  return Number(v).toFixed(digits)
}

const TERMINAL_STATES = new Set(['succeeded', 'failed', 'cancelled', 'interrupted'])

export function Indices() {
  const qc = useQueryClient()
  const [searchParams, setSearchParams] = useSearchParams()
  const symbolParam = searchParams.get('symbol') ?? ''
  const [period, setPeriod] = useState<IndexKlinePeriod>('1d')
  const [range, setRange] = useState(() => defaultRange())
  const [selection, setSelection] = useState<{ context: string; day: string } | null>(null)
  const [hoverPrice, setHoverPrice] = useState<{ context: string; price: number | null } | null>(null)
  const [view, setView] = useState<'kline' | 'intraday'>('kline')
  const [visibleBars, setVisibleBars] = useState(60)
  const [currentBars, setCurrentBars] = useState(60)
  const [availableBars, setAvailableBars] = useState(0)
  const [zoomResetKey, setZoomResetKey] = useState(0)
  const [showRepair, setShowRepair] = useState(false)
  const [jobId, setJobId] = useState<string | null>(null)
  const canManage = hasPermission('data:manage')

  // 分时数据依赖分钟K批量数据 (kline.minute.batch)
  const caps = useCapabilities()
  const matrix = useCapabilityMatrix()
  const hasMinuteCap = routeCapUsable(matrix.data, 'minute') ?? !!caps.data?.capabilities?.['kline.minute.batch']
  const hasDailyCap = routeCapUsable(matrix.data, 'daily') ?? !!caps.data?.capabilities?.['kline.daily.batch']

  const catalog = useQuery({ queryKey: QK.indexCatalog, queryFn: api.indexCatalog, staleTime: Infinity })
  const topRows = catalog.data?.rows ?? []
  const selectedSymbol = topRows.some(row => row.symbol === symbolParam) ? symbolParam : topRows[0]?.symbol ?? ''
  const context = `${selectedSymbol}:${period}:${range.start}:${range.end}`
  const rangeError = !range.end ? '请选择结束日期'
    : range.start && range.start > range.end ? '结束日期不能早于开始日期' : null

  const selectIndex = (symbol: string) => {
    setSearchParams({ symbol })
  }

  const quotes = useQuery({
    queryKey: QK.indexPageQuotes(topRows.map(row => row.symbol).join(',')),
    queryFn: () => api.indexQuotes(topRows.map(row => row.symbol)),
    enabled: topRows.length > 0,
    placeholderData: (prev) => prev,
  })

  const daily = useQuery({
    queryKey: QK.indexKline(selectedSymbol, period, range.start, range.end),
    queryFn: async () => ({ ...await api.indexKline(selectedSymbol, period, range), context }),
    enabled: !!selectedSymbol && !rangeError,
    placeholderData: (prev) => prev,
  })
  useEffect(() => {
    if (
      !range.start
      && daily.data?.context === context
      && daily.data.available_start_date
    ) {
      setRange(current => current.start
        ? current
        : { ...current, start: daily.data!.available_start_date! })
    }
  }, [context, daily.data, range.start])
  const dataMatches = daily.data?.context === context && daily.data.symbol === selectedSymbol && daily.data.period === period && !rangeError
  const sourceRows = useMemo(() => dataMatches ? daily.data?.rows ?? [] : [], [dataMatches, daily.data?.rows])
  const tradeDate = (label: string) => sourceRows.find(row => row.date === label)?.trade_date ?? label
  const selectedDate = selection?.context === context && sourceRows.some(row => (row.trade_date ?? row.date) === selection.day)
    ? selection.day : sourceRows.at(-1)?.trade_date ?? sourceRows.at(-1)?.date ?? null
  const linkedPrice = hoverPrice?.context === context ? hoverPrice.price : null

  const minute = useQuery({
    queryKey: QK.indexMinute(selectedSymbol, selectedDate ?? ''),
    queryFn: () => api.indexMinute(selectedSymbol, selectedDate ?? undefined, true),
    enabled: !!selectedSymbol && !!selectedDate && hasMinuteCap,
  })

  const jobs = useQuery({
    queryKey: QK.pipelineJobs,
    queryFn: () => api.pipelineJobs(15),
    enabled: canManage,
    refetchInterval: 10_000,
  })
  const activeIndexJob = jobs.data?.jobs.find(job => job.kind === 'index_repair' && !TERMINAL_STATES.has(job.status))
  const trackedId = jobId ?? activeIndexJob?.id ?? null
  const job = useQuery({
    queryKey: QK.pipelineJob(trackedId ?? ''),
    queryFn: () => api.pipelineJob(trackedId!),
    enabled: canManage && !!trackedId,
    refetchInterval: q => q.state.data && TERMINAL_STATES.has(q.state.data.status) ? false : 1_000,
  })
  const isRunning = !!jobs.data?.active_id || !!activeIndexJob || (!!job.data && !TERMINAL_STATES.has(job.data.status))
  const stop = useMutation({
    mutationFn: () => api.pipelineJobCancel(trackedId!),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.pipelineJob(trackedId!) })
      qc.invalidateQueries({ queryKey: QK.pipelineJobs })
    },
  })
  const jobStatus = job.data?.status
  const completedJobId = job.data?.id
  useEffect(() => {
    if (jobStatus && TERMINAL_STATES.has(jobStatus)) {
      for (const queryKey of [QK.indexQuotes, ['index-kline'], ['index-daily'], ['index-minute'], QK.dataStatus, QK.pipelineJobs]) {
        void qc.invalidateQueries({ queryKey })
      }
      const timer = window.setTimeout(() => {
        setJobId(current => current === completedJobId ? null : current)
      }, 5_000)
      return () => window.clearTimeout(timer)
    }
  }, [jobStatus, completedJobId, qc])

  const adjustBars = (action: 'default' | 'increase' | 'decrease') => {
    const count = action === 'default' ? 60
      : Math.max(1, Math.min(availableBars, Math.round(currentBars * (action === 'increase' ? 1.5 : 0.5))))
    setVisibleBars(count)
    setZoomResetKey(key => key + 1)
  }

  const quoteBySymbol = useMemo(() => {
    const m = new Map<string, any>()
    for (const q of quotes.data?.rows ?? []) m.set(q.symbol, q)
    return m
  }, [quotes.data?.rows])
  const selectedQuote = selectedSymbol ? quoteBySymbol.get(selectedSymbol) : null
  const selectedQuoteValue = selectedQuote?.last_price ?? selectedQuote?.price ?? selectedQuote?.close
  const selectedQuotePct = selectedQuote?.change_pct ?? selectedQuote?.pct

  const selectedInfo = topRows.find(r => r.symbol === selectedSymbol) || daily.data?.index_info
  const minuteRows: NullableMinuteKlineRow[] = minute.data?.symbol === selectedSymbol
    && minute.data?.date === selectedDate && dataMatches
    ? minute.data.rows : []
  const selectedIdx = selectedDate ? sourceRows.findIndex(r => (r.trade_date ?? r.date) === selectedDate) : -1
  const prevClose = minute.data?.prev_close ?? (
    period === '1d' && selectedIdx > 0 ? sourceRows[selectedIdx - 1].close : undefined
  )

  const renderIndexItem = (item: IndexInstrument) => {
    const q = quoteBySymbol.get(item.symbol)
    const pct = q?.change_pct ?? q?.pct
    const current = q?.last_price ?? q?.price ?? q?.close
    const active = item.symbol === selectedSymbol
    return (
      <button
        key={item.symbol}
        aria-pressed={active}
        onClick={() => selectIndex(item.symbol)}
        className={`w-full rounded-btn px-2 py-2 text-left transition-colors ${active ? 'bg-accent/15 text-foreground' : 'hover:bg-elevated text-secondary'}`}
      >
        <div className="flex items-center justify-between gap-2">
          <span className="truncate text-xs font-medium">{item.name || item.symbol}</span>
          <span className={`text-[10px] font-mono ${Number(pct ?? 0) >= 0 ? 'text-bull' : 'text-bear'}`}>{fmtPct(pct)}</span>
        </div>
        <div className="mt-0.5 flex items-center justify-between text-[10px] font-mono text-muted">
          <span>{item.symbol}</span>
          <span>{fmtNum(current)}</span>
        </div>
      </button>
    )
  }

  return (
    <div className="h-full overflow-auto bg-base">
      <PageHeader
        title="指数"
        subtitle="七只指数 · 日K / 周K / 月K与分时"
        right={
          <div className="flex items-center gap-2">
          {canManage && <button
            onClick={() => setShowRepair(true)}
            className="inline-flex items-center gap-1.5 rounded-btn bg-accent px-3 py-1.5 text-xs font-medium text-white hover:bg-accent/90 disabled:opacity-50"
          >
            <RefreshCw className="h-3.5 w-3.5" />
            数据修正
          </button>}
          </div>
        }
      />

      {job.data && <div className="px-4 pt-4 space-y-2">
        <ActiveJobCard job={job.data} />
        {!TERMINAL_STATES.has(job.data.status) && <button disabled={stop.isPending || job.data.status === 'cancelling'}
          onClick={() => stop.mutate()} className="rounded-btn border border-danger/30 px-3 py-1.5 text-xs text-danger disabled:opacity-40">
          {job.data.status === 'cancelling' ? '正在取消…' : '停止修正'}
        </button>}
        {stop.isError && <div role="alert" className="text-xs text-danger">{stop.error.message}</div>}
      </div>}
      {job.isError && <div role="alert" className="px-4 pt-3 text-xs text-danger">修正进度加载失败，请重试</div>}
      <div className="grid grid-cols-1 lg:grid-cols-[13rem_minmax(0,1fr)] gap-4 p-4">
        <aside className="self-start rounded-card border border-border bg-surface p-3">
          <div className="mb-2 px-1 text-[11px] uppercase tracking-wider text-muted">指数列表</div>
          {catalog.isLoading && <div className="text-xs text-muted">指数加载中…</div>}
          {catalog.isError && <div role="alert" className="text-xs text-danger">指数列表加载失败 <button onClick={() => catalog.refetch()}>重试</button></div>}
          <div className="grid grid-cols-2 gap-1 sm:grid-cols-3 lg:grid-cols-1">
            {topRows.map(renderIndexItem)}
          </div>
        </aside>

        <main className="min-w-0 rounded-card border border-border bg-surface p-3">
          <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
            <div className="min-w-0">
              <div className="flex flex-wrap items-center gap-2">
                <Activity className="h-4 w-4 text-accent" />
                <h2 className="truncate text-sm font-semibold text-foreground">
                  {selectedInfo?.name || selectedSymbol || '未选择指数'}
                </h2>
                {selectedSymbol && <span className="font-mono text-xs text-muted">{selectedSymbol}</span>}
                {selectedSymbol && <span className="font-mono text-xs text-foreground">{fmtNum(selectedQuoteValue)}</span>}
                {selectedSymbol && <span className={`font-mono text-xs ${Number(selectedQuotePct ?? 0) >= 0 ? 'text-bull' : 'text-bear'}`}>{fmtPct(selectedQuotePct)}</span>}
              </div>
              {quotes.isError && <div className="mt-1 text-xs text-danger">行情加载失败，请重试</div>}
            </div>
            <div className="flex flex-wrap items-center gap-2 text-xs">
              <div aria-label="图表视图" className="inline-flex items-center rounded border border-border/60 bg-base/60 p-0.5">
                <button type="button" aria-pressed={view === 'intraday'} onClick={() => setView('intraday')}
                  className={`inline-flex h-6 items-center gap-1 rounded px-2.5 text-[11px] ${view === 'intraday' ? 'bg-accent/20 text-accent' : 'text-muted hover:bg-elevated'}`}>
                  <Clock className="h-3 w-3" />分时
                </button>
                <label className={`inline-flex h-6 items-center gap-1 rounded px-2 text-[11px] ${view === 'kline' ? 'bg-accent/20 text-accent' : 'text-muted'}`}>
                  <LineChart className="h-3 w-3" />K线
                  <select aria-label="K线周期" value={period} onFocus={() => setView('kline')} onClick={() => setView('kline')}
                    onChange={event => {
                      const next = event.target.value as IndexKlinePeriod
                      setPeriod(next); setRange(defaultRange()); setVisibleBars(60); setCurrentBars(60)
                      setAvailableBars(0); setZoomResetKey(k => k + 1); setView('kline')
                    }} className="h-6 rounded border-0 bg-transparent outline-none">
                    {INDEX_KLINE_PERIODS.map(value => <option key={value} value={value} className="bg-surface">{KLINE_PERIOD_LABELS[value]}</option>)}
                  </select>
                </label>
              </div>
              {view === 'kline' && <div className="inline-flex flex-wrap items-center gap-2">
                <div className="inline-flex items-center rounded border border-border/60 bg-base/60 p-0.5">
                  {([
                    ['默认', 'default'], ['放大', 'increase'], ['缩小', 'decrease'],
                  ] as const).map(([label, action]) => <button key={action} type="button"
                    disabled={action !== 'default' && availableBars === 0}
                    onClick={() => adjustBars(action)}
                    className="h-6 rounded px-2.5 text-[11px] text-muted hover:bg-elevated disabled:opacity-40">{label}</button>)}
                </div>
                <span className="text-[10px] text-muted" aria-live="polite">
                  {availableBars > 0 ? `${Math.min(currentBars, availableBars)}根` : '—'}
                </span>
                <div className="inline-flex shrink-0 items-center gap-2">
                  <DatePicker
                    aria-label="开始日期"
                    value={range.start}
                    onChange={start => setRange(r => ({ ...r, start }))}
                    max={range.end}
                    clearable
                    align="left"
                  />
                  <span className="text-muted/70 text-[10px]">~</span>
                  <DatePicker
                    aria-label="K线结束日期"
                    value={range.end}
                    onChange={end => setRange(r => ({ ...r, end }))}
                    min={range.start}
                    clearable
                  />
                </div>
              </div>}
            </div>
          </div>
          {view === 'kline' && period !== '1d' && <div className="mb-3 text-right text-[10px] text-muted">
            点选周期联动最后交易日分时；当前周期可能尚未结束
          </div>}
          {rangeError && <div role="alert" className="text-xs text-danger">{rangeError}</div>}
          <div className={`grid grid-cols-1 gap-3 ${view === 'kline' ? 'xl:grid-cols-2' : ''}`}>
              {view === 'kline' && <div className="min-w-0">
                <StockDailyKChart
                  externalData={{ rows: sourceRows, loading: daily.isLoading || daily.isPlaceholderData, error: daily.isError }}
                  height={400}
                  showLimitMarkers={false}
                  showMarkerToggle={false}
                  period={period}
                  symbol={selectedSymbol}
                  linkedPrice={linkedPrice}
                  onDateClick={(date) => {
                    setSelection({ context, day: tradeDate(date) })
                    setHoverPrice(null)
                  }}
                  visibleBars={visibleBars}
                  zoomResetKey={zoomResetKey}
                  onVisibleBarsChange={setCurrentBars}
                  onAvailableBarsChange={setAvailableBars}
                />
              </div>}
              <div className="min-w-0 border-t border-border pt-3 xl:border-t-0" style={{ minHeight: 500 }}>
                <div className="mb-2 flex items-center gap-2 text-xs text-secondary"><Clock className="h-3.5 w-3.5" />分时 <span className="font-mono text-muted">{selectedDate ?? '请选择K线日期'}</span></div>
                {!hasMinuteCap ? (
                  <div className="flex h-full flex-col items-center justify-center gap-2 text-center">
                    <Lock className="h-5 w-5 text-muted" />
                    <div className="text-xs text-secondary">指数分时数据不可用</div>
                    <div className="text-[10px] text-muted">分钟K(批量)数据不可用</div>
                  </div>
                ) : (
                  <>
                    {minute.isLoading && <div className="py-2 text-xs text-muted">分时加载中…</div>}
                    {minute.isError && <div role="alert" className="py-2 text-xs text-danger">指数分时加载失败 <button onClick={() => minute.refetch()}>重试</button></div>}
                    {!minute.isLoading && !minute.isError && minuteRows.length === 0 && (
                      <div className="flex h-full items-center justify-center text-xs text-muted">
                        暂无分时数据
                      </div>
                    )}
                    {minuteRows.length > 0 && (
                      <EChartsIntraday
                        key={`${selectedSymbol}:${selectedDate}`}
                        data={minuteRows}
                        height={500}
                        prevClose={prevClose}
                        date={selectedDate ?? undefined}
                        showLimitLines={false}
                        showAvgLine={false}
                        onPriceHover={price => setHoverPrice({ context, price })}
                      />
                    )}
                  </>
                )}
              </div>
            </div>
        </main>
      </div>
      {showRepair && canManage && <SettingsModal title="数据修正" onClose={() => setShowRepair(false)}>
        <RepairDailyPanel scope="index" hasCap={hasDailyCap} hasMinuteCap={hasMinuteCap}
          isRunning={isRunning} latestDates={{}}
          onStart={id => { setJobId(id); setShowRepair(false) }} />
      </SettingsModal>}
    </div>
  )
}
