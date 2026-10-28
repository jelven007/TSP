import { Modal } from '@/components/Modal'
import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { motion, AnimatePresence } from 'framer-motion'
import {
  Database,
  Play,
  Square,
  Loader2,
  HardDrive,
  Clock,
  CheckSquare,
  Trash2,
  Plus,
  Wifi,
  SlidersHorizontal,
  AlertTriangle,
  Info,
} from 'lucide-react'
import { Link } from 'react-router-dom'
import { EndpointTestDialog } from '@/components/EndpointTestDialog'
import { api, type ExtDataConfig, type KlinePeriod } from '@/lib/api'
import {
  useCapabilities,
  useCapabilityMatrix,
  useSettings,
  usePreferences,
  useQuoteStatus,
  useQuoteInterval,
  useDataStatus,
} from '@/lib/useSharedQueries'
import { useToggleRealtimeQuotes, useUpdateQuoteInterval } from '@/lib/useSharedMutations'
import { MissingCapChip, routeCapUsable, routeProviderDisplay, type RouteCapId } from '@/lib/capability-labels'
import { KLINE_QUERY_PREFIXES, QK } from '@/lib/queryKeys'
import { PageHeader } from '@/components/PageHeader'
import { toast } from '@/components/Toast'
import { useAdjFactorSyncGate } from '@/components/AdjFactorSyncGate'
import { findDataSource } from '@/lib/dataSources'

// 拆分出的子组件
import { StatCard, type FieldTab, type CapLimitValue } from '@/components/data/StatCard'
import { ActiveJobCard } from '@/components/data/ActiveJobCard'
import { SectionTitle, HistoryRow } from '@/components/data/SectionTitle'
import { SettingsModal } from '@/components/data/SettingsModal'
import { StockCatalogDialog } from '@/components/data/StockCatalogDialog'
import { ScheduleOverview } from '@/components/data/ScheduleOverview'
import { KlineSyncConfig } from '@/components/data/KlineSyncConfig'
import { isLocalDerivedPeriod } from '@/lib/klinePeriods'
import { EnrichedRebuildPanel } from '@/components/data/EnrichedRebuildPanel'
import { RegimeConfigCard } from '@/components/data/RegimeConfigCard'
import { PipelineScopeConfig } from '@/components/data/PipelineScopeConfig'
import { PageSettingsModal, getCardVisibility, getCardOrder, type CardKey } from '@/components/data/PageSettingsModal'
import { QuoteConfigCard } from '@/components/data/QuoteConfigCard'
import { EnrichedSchemaModal } from '@/components/data/SchemaModal'
import { Skeleton } from '@/components/data/Skeleton'
import { DataReadiness } from '@/components/data/DataReadiness'
import { ExtDataStatCard } from '@/components/ext-data/ExtDataStatCard'
import { CreateExtDialog } from '@/components/ext-data/CreateExtDialog'
import { EditExtDialog } from '@/components/ext-data/EditExtDialog'

const PIPELINE_TERMINAL_STATES = new Set(['succeeded', 'failed', 'cancelled', 'interrupted'])
const STAGE_CARD: Record<string, string> = {
  sync_instruments: 'instruments',
  sync_daily: 'daily',
  extend_history: 'daily',
  sync_adj: 'adj_factor',
  compute_enriched: 'enriched',
  rebuild_enriched: 'enriched',
  sync_index: 'index_daily',
  sync_minute: 'minute',
  extend_minute: 'minute',
  sync_1m: 'minute',
  sync_5m: '5m',
  sync_10m: '10m',
  sync_15m: '15m',
  sync_1w: 'weekly',
  compute_regime: 'regime',
  // regime 软失败时入 skipped_stages 的是 'regime'(非 stage 名), 也映射到该卡片
  regime: 'regime',
}

export function Data() {
  const qc = useQueryClient()
  const [activeJobId, setActiveJobId] = useState<string | null>(null)
  const startTime = useRef<number | null>(null)
  const topRef = useRef<HTMLDivElement>(null)

  const caps = useCapabilities()
  const settings = useSettings()

  const status = useDataStatus({
    refetchInterval: (query) => {
      const data = query.state.data
      // 指标异步预热中 或 有活跃 job → 加快轮询到 2s
      if (activeJobId || data?.indicators_ready === false) return 2_000
      return 30_000
    },
  })

  // 环境(regime) 覆盖画像 —— 走独立接口(/api/regime/coverage), 不在 data/status 内。
  // 同步任务完成后刷新一次; 平时 30s 轮询与 status 对齐。
  const regimeCoverage = useQuery({
    queryKey: QK.regimeCoverage,
    queryFn: () => api.regimeCoverage(),
    refetchInterval: activeJobId ? false : 30_000,
  })

  const history = useQuery({
    queryKey: QK.pipelineJobs,
    queryFn: () => api.pipelineJobs(15),
    refetchInterval: activeJobId ? false : 60_000,
  })

  const job = useQuery({
    queryKey: QK.pipelineJob(activeJobId ?? ''),
    queryFn: () => api.pipelineJob(activeJobId!),
    enabled: !!activeJobId,
    refetchInterval: (q: any) => {
      const j = q.state.data
      return j && PIPELINE_TERMINAL_STATES.has(j.status) ? false : 1_000
    },
  })

  const startSync = useMutation({
    mutationFn: api.pipelineRun,
    onSuccess: ({ job_id }) => {
      setActiveJobId(job_id)
      startTime.current = Date.now()
    },
  })

  // 停止同步: 二次确认后调 cancel 端点 (协作式终止, 当前分块完成后线程自行退出)
  const [showStopConfirm, setShowStopConfirm] = useState(false)
  const stopSync = useMutation({
    mutationFn: () => api.pipelineJobCancel(activeJobId!),
    onSuccess: () => {
      setShowStopConfirm(false)
      qc.invalidateQueries({ queryKey: QK.pipelineJob(activeJobId!) })
    },
  })

  // 无除权因子能力时同步前置确认 (静默降级告知)
  const adjGate = useAdjFactorSyncGate()

  const [showClearConfirm, setShowClearConfirm] = useState(false)
  const clearData = useMutation({
    mutationFn: api.dataClear,
    onSuccess: () => {
      // 清库删除全部 parquet + alerts, 各页 (行情/自选/选股/指数/财务/个股/连板/监控) 缓存
      // 数据均已失效, 故广域失效所有 query 令其重取 —— 数据已清空, 全量刷新是正确行为而非误伤。
      qc.invalidateQueries()
      setShowClearConfirm(false)
    },
  })

  const [openSettings, setOpenSettings] = useState<string | null>(null)
  const [showStockCatalog, setShowStockCatalog] = useState(false)
  const [indexExtendValue, setIndexExtendValue] = useState(6)
  const [indexExtendUnit, setIndexExtendUnit] = useState<'month' | 'year'>('month')
  const [schemaTable, setSchemaTable] = useState<string | null>(null)
  const [showEndpointTest, setShowEndpointTest] = useState(false)
  const [showCreateExt, setShowCreateExt] = useState(false)
  const [editingExt, setEditingExt] = useState<ExtDataConfig | null>(null)
  const [indexBatchInput, setIndexBatchInput] = useState('100')

  const extConfigs = useQuery({
    queryKey: QK.extData,
    queryFn: api.extDataList,
  })

  const deleteExt = useMutation({
    mutationFn: (id: string) => api.extDataDelete(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: QK.extData }),
  })

  const syncIndexDaily = useMutation({
    mutationFn: () => api.syncIndexDaily(indexSyncDays),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.dataStatus })
      qc.invalidateQueries({ queryKey: QK.indexQuotes })
      qc.invalidateQueries({ queryKey: ['index-daily'] })
    },
  })

  const prefs = usePreferences()
  const klineConfigs = useQuery({ queryKey: QK.klineSyncConfigs, queryFn: api.klineSyncConfigs })

  // 数据源列表 + 当前数据源 (顶部"切换数据源"按钮展示)
  const dataSources = useQuery({
    queryKey: QK.dataSources,
    queryFn: api.dataSources,
    staleTime: 60_000,
  })
  const activeProvider = prefs.data?.daily_data_provider || 'rustdx'
  const activeDataSourceName = activeProvider === 'tickflow'
    ? 'TickFlow'
    : (findDataSource(dataSources.data, activeProvider)?.display_name || activeProvider)

  // —— 能力路由门控 (全项目统一判定) ——
  // usable = 生效源当前能否提供该能力 (含插件/自定义源; TickFlow 档位不足则不可用),
  // 区别于 useCapabilities 的 TickFlow 套餐视角。矩阵未加载时回退套餐视角, 避免首屏闪烁。
  const matrix = useCapabilityMatrix()
  const tfCaps = caps.data?.capabilities
  const usableOr = (id: RouteCapId, tfHas: boolean) => routeCapUsable(matrix.data, id) ?? tfHas
  // 合并视角 caps: 套餐限额键位 + 路由可用性覆盖, 卡片徽章/显隐/设置弹窗共用
  const mergedCaps = useMemo(() => {
    const m: Record<string, CapLimitValue> = { ...(tfCaps ?? {}) }
    const merge = (tfKey: string, usable: boolean) => { m[tfKey] = usable ? (m[tfKey] ?? true) : false }
    merge('adj_factor', usableOr('adj_factor', !!tfCaps?.['adj_factor']))
    merge('kline.daily.batch', usableOr('daily', !!tfCaps?.['kline.daily.batch']))
    merge('kline.minute.batch', usableOr('minute', !!tfCaps?.['kline.minute.batch']))
    merge('financial', usableOr('financial', !!tfCaps?.['financial']))
    return m
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tfCaps, matrix.data])

  const indexDailyBatchSize = prefs.data?.index_daily_batch_size ?? 100
  const autoSyncEnabled = prefs.data?.data_sync_auto_enabled ?? true
  const updateAutoSync = useMutation({
    mutationFn: (enabled: boolean) => api.updateDataSyncAuto(enabled),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.preferences })
      qc.invalidateQueries({ queryKey: QK.dataStatus })
    },
    onError: (error: Error) => toast(error.message),
  })

  useEffect(() => {
    setIndexBatchInput(String(indexDailyBatchSize))
  }, [indexDailyBatchSize])

  const updateIndexBatchSize = useMutation({
    mutationFn: (size: number) => api.updateIndexDailyBatchSize(size),
    onSuccess: () => qc.invalidateQueries({ queryKey: QK.preferences }),
  })

  const [showIntervalEdit, setShowIntervalEdit] = useState(false)
  const handleToggleIntervalEdit = useCallback((fromEvent?: boolean) => {
    setShowIntervalEdit(v => {
      const next = !v
      if (!fromEvent) {
        window.dispatchEvent(new CustomEvent('quote-interval-editor-toggle', { detail: { source: 'data' } }))
      }
      return next
    })
  }, [])
  useEffect(() => {
    const handler = (e: Event) => {
      const ce = e as CustomEvent
      if (ce.detail?.source !== 'data') {
        setShowIntervalEdit(v => !v)
      }
    }
    window.addEventListener('quote-interval-editor-toggle', handler)
    return () => window.removeEventListener('quote-interval-editor-toggle', handler)
  }, [])
  const quoteInterval = useQuoteInterval()
  const updateInterval = useUpdateQuoteInterval()
  const minuteRefresh = useQuery({
    queryKey: QK.minuteRefreshStatus,
    queryFn: api.minuteRefreshStatus,
    refetchInterval: 30_000,
  })

  const realtimeEnabled = prefs.data?.realtime_quotes_enabled ?? true
  const quoteStatus = useQuoteStatus()
  const toggleQuote = useToggleRealtimeQuotes()
  const realtimeIntervalS = quoteInterval.data?.interval ?? quoteStatus.data?.interval_s ?? 3
  const realtimeAvailable = routeCapUsable(matrix.data, 'realtime') ?? quoteStatus.data?.realtime_allowed
  // 路由感知能力门控: 矩阵判定生效源可用性, 未加载回退套餐视角
  const hasDailyBatchCap = usableOr('daily', !!tfCaps?.['kline.daily.batch'])
  const hasMinuteCap = usableOr('minute', !!tfCaps?.['kline.minute.batch'])
  const indexAuto = prefs.data?.pipeline_pull_index ?? true
  const etfAuto = prefs.data?.pipeline_pull_etf ?? true

  // 数据画像卡片显隐(由页面设置弹窗控制,存 localStorage)
  const [cardVisibleTick, setCardVisibleTick] = useState(0)
  useEffect(() => {
    const handler = () => setCardVisibleTick(t => t + 1)
    window.addEventListener('data-card-visible-change', handler)
    return () => window.removeEventListener('data-card-visible-change', handler)
  }, [])
  const cardVisible = getCardVisibility(mergedCaps)
  // 引用 cardVisibleTick 触发重渲染(避免 lint 警告)
  void cardVisibleTick

  const jobStatus = job.data?.status
  const jobKind = job.data?.kind
  const jobId = job.data?.id
  useEffect(() => {
    if (jobStatus && PIPELINE_TERMINAL_STATES.has(jobStatus)) {
      qc.invalidateQueries({ queryKey: QK.dataStatus })
      qc.invalidateQueries({ queryKey: QK.pipelineJobs })
      // 同步任务结束后 regime 覆盖范围可能变化, 一并刷新画像
      qc.invalidateQueries({ queryKey: QK.regimeCoverage })
      // 失败/取消也可能已有分块落盘; 失效已缓存的K线, 不依赖实时SSE是否开启。
      if (jobKind === 'repair_daily' || jobKind?.startsWith('kline_')) {
        qc.invalidateQueries({
          predicate: query => KLINE_QUERY_PREFIXES.some(prefix => query.queryKey[0] === prefix),
        })
      }
      // 同步重写了指数日K/enriched/日K → 失效消费这些数据的查询。
      // 侧边栏指数查询挂在 Layout 常驻不重挂载 (refetchOnWindowFocus 已关),
      // 不失效会一直显示同步前的旧值; 自选相关查询在页面正打开时同理。
      if (jobStatus === 'succeeded') {
        qc.invalidateQueries({ queryKey: QK.indexQuotes })
        qc.invalidateQueries({ queryKey: ['index-daily'] })
        qc.invalidateQueries({ queryKey: ['watchlist-enriched'] })
        qc.invalidateQueries({ queryKey: ['kline-batch'] })
      }
      const t = setTimeout(() => setActiveJobId(null), 5_000)
      return () => clearTimeout(t)
    }
  }, [jobStatus, jobKind, jobId, qc])

  useEffect(() => {
    if (job.isError && /404/.test(String((job.error as any)?.message ?? ''))) {
      setActiveJobId(null)
    }
  }, [job.isError, job.error])

  useEffect(() => {
    if (history.data?.active_id) {
      const historyJobId = history.data.active_id
      setActiveJobId(current => current ?? historyJobId)
    }
  }, [history.data?.active_id])

  const s = status.data
  const isLoading = status.isLoading
  const isRunning = !!job.data && !PIPELINE_TERMINAL_STATES.has(job.data.status)
  const canCancel = job.data?.status === 'queued'
    || job.data?.status === 'pending'
    || job.data?.status === 'running'
  const isStarting = startSync.isPending
  const hasData = !!(s?.instruments?.rows || s?.daily?.rows)
  // none 档(无 key / 无效 key) → 禁用立即同步 (同步依赖付费档的批量端点)
  const isNoKey = settings.data?.mode === 'none'
  const indexOverviewStats = s ? {
    rows: 0,
    earliest_date: s.index_daily?.earliest_date ?? s.index_enriched?.earliest_date ?? null,
    latest_date: s.index_daily?.latest_date ?? s.index_enriched?.latest_date ?? null,
    symbols_covered: s.index_daily?.symbols_covered ?? s.index_instruments?.rows ?? 0,
    trading_days: s.index_daily?.trading_days ?? s.index_enriched?.trading_days ?? 0,
  } : null
  // ETF 统计(后端已按 asset_type='etf' 从 index 存储中拆分)
  const etfOverviewStats = s ? {
    rows: 0,
    earliest_date: s.etf_daily?.earliest_date ?? s.etf_enriched?.earliest_date ?? null,
    latest_date: s.etf_daily?.latest_date ?? s.etf_enriched?.latest_date ?? null,
    symbols_covered: s.etf_daily?.symbols_covered ?? s.etf_instruments?.rows ?? 0,
    trading_days: s.etf_daily?.trading_days ?? s.etf_enriched?.trading_days ?? 0,
  } : null
  const indexOverviewLabel = s ? '日 · 维表 · 日K · 指标' : undefined
  const indexEarliestDate = s?.index_daily?.earliest_date ?? s?.index_enriched?.earliest_date ?? null
  const indexOffsetDays = indexExtendUnit === 'month' ? indexExtendValue * 30 : indexExtendValue * 365
  const indexTargetDate = (() => {
    const d = indexEarliestDate ? new Date(indexEarliestDate) : new Date()
    d.setDate(d.getDate() - indexOffsetDays)
    return d
  })()
  const indexTargetDateText = indexTargetDate.toISOString().slice(0, 10)
  const indexSyncDays = Math.min(
    5000,
    Math.max(30, Math.ceil((Date.now() - indexTargetDate.getTime()) / 86_400_000) + 1),
  )

  const activeCard = isRunning && job.data ? STAGE_CARD[job.data.stage] ?? null : null

  const skippedCards = new Set(
    (job.data?.result?.skipped_stages ?? [])
      .map(s => STAGE_CARD[s])
      .filter(Boolean) as string[]
  )

  const prevStageRef = useRef<string | null>(null)
  const [doneStages, setDoneStages] = useState<Set<string>>(new Set())
  const jobStage = job.data?.stage
  useEffect(() => {
    if (!jobStage) return
    const stage = jobStage
    if (stage === prevStageRef.current) return
    const prev = prevStageRef.current
    if (prev && STAGE_CARD[prev]) {
      setDoneStages((s) => new Set(s).add(STAGE_CARD[prev]))
    }
    prevStageRef.current = stage
    qc.invalidateQueries({ queryKey: QK.dataStatus })
  }, [jobStage, qc])

  useEffect(() => {
    if (!activeJobId) {
      setDoneStages(new Set())
      prevStageRef.current = null
    }
  }, [activeJobId])

  const handleJobClick = useCallback((id: string) => {
    setActiveJobId(id)
    requestAnimationFrame(() => {
      topRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })
    })
  }, [])

  // 按卡片 key 渲染对应的 StatCard (顺序由 getCardOrder 控制, 显隐由 cardVisible 控制)
  const renderStatCard = (k: CardKey): React.ReactNode => {
    switch (k) {
      case 'quotes':
        return (
          <QuoteConfigCard
            enabled={realtimeEnabled}
            running={quoteStatus.data?.running ?? false}
            isTrading={quoteStatus.data?.is_trading_hours ?? false}
            lastFetchMs={quoteStatus.data?.last_fetch_ms ?? null}
            intervalS={realtimeIntervalS}
            intervalMin={quoteInterval.data?.min_interval ?? 1}
            intervalMax={quoteInterval.data?.max_interval ?? 60}
            loading={quoteStatus.isLoading}
            onToggle={(v) => toggleQuote.mutate(v)}
            toggling={toggleQuote.isPending}
            showIntervalEdit={showIntervalEdit}
            onShowIntervalEdit={handleToggleIntervalEdit}
            onIntervalChange={(v) => updateInterval.mutate(v)}
          />
        )
      case 'instruments':
        return (
          <StatCard
            title="个股维表"
            hint="沪深官方名单 · 每天 09:00"
            stats={s?.instruments}
            isInstrument
            loading={isLoading}
            active={activeCard === 'instruments'}
            done={doneStages.has('instruments')}
            skipped={skippedCards.has('instruments')}
            stagePct={activeCard === 'instruments' ? (job.data?.stage_pct ?? 0) : 0}
            tierKey="instruments"
            capLimits={mergedCaps}
            customProvider="沪深交易所"
            auto
            onManage={() => setShowStockCatalog(true)}
            onShowFields={() => setSchemaTable('instruments')}
          />
        )
      case 'daily':
        return (
          <StatCard
            title="日 K"
            hint="交易日16:00同步"
            stats={s?.daily}
            loading={isLoading}
            active={activeCard === 'daily'}
            done={doneStages.has('daily')}
            skipped={skippedCards.has('daily')}
            stagePct={activeCard === 'daily' ? (job.data?.stage_pct ?? 0) : 0}
            tierKey="daily"
            capLimits={mergedCaps}
            customProvider={routeProviderDisplay(matrix.data, 'daily')}
            auto={klineConfigs.data?.['1d'].enabled ?? true}
            manual={!autoSyncEnabled}
            onShowFields={() => setSchemaTable('daily')}
            onSettings={() => setOpenSettings('daily')}
            settingsOpen={openSettings === 'daily'}
          />
        )
      case 'adj_factor':
        return (
          <StatCard
            title="除权因子"
            hint="增量同步 · 全市场"
            stats={s?.adj_factor}
            loading={isLoading}
            active={activeCard === 'adj_factor'}
            done={doneStages.has('adj_factor')}
            skipped={skippedCards.has('adj_factor')}
            stagePct={activeCard === 'adj_factor' ? (job.data?.stage_pct ?? 0) : 0}
            tierKey="adj_factor"
            capLimits={mergedCaps}
            customProvider={routeProviderDisplay(matrix.data, 'adj_factor')}
            auto
            manual={!autoSyncEnabled}
            onShowFields={() => setSchemaTable('adj_factor')}
          />
        )
      case 'enriched':
        return (
          <StatCard
            title="Enriched"
            hint="复权 OHLCV + 技术指标"
            stats={s?.enriched}
            loading={isLoading}
            active={activeCard === 'enriched'}
            done={doneStages.has('enriched')}
            skipped={skippedCards.has('enriched')}
            stagePct={activeCard === 'enriched' ? (job.data?.stage_pct ?? 0) : 0}
            tierKey="enriched"
            capLimits={mergedCaps}
            auto
            manual={!autoSyncEnabled}
            subLabel={status.data?.indicators_ready === false ? '字段 · 指标计算中…' : '字段 · 指标 · 信号'}
            localBadgeSuffix={`${prefs.data?.enriched_batch_size ?? 1000}只/批`}
            onShowFields={() => setSchemaTable('enriched')}
            onSettings={hasData ? () => setOpenSettings(v => v === 'enriched' ? null : 'enriched') : undefined}
            settingsOpen={openSettings === 'enriched'}
          />
        )
      case 'index':
        return (
          <StatCard
            title="指数"
            hint="CN_Index · 独立存储"
            stats={indexOverviewStats}
            loading={isLoading}
            active={activeCard === 'index_daily'}
            done={doneStages.has('index_daily')}
            skipped={skippedCards.has('index_daily')}
            stagePct={activeCard === 'index_daily' ? (job.data?.stage_pct ?? 0) : 0}
            tierKey="daily"
            capLimits={mergedCaps}
            auto={indexAuto}
            manual={!autoSyncEnabled}
            subLabel={indexOverviewLabel}
            fieldTabs={[
              { label: '维表', table: 'index_instruments' },
              { label: '日K', table: 'index_daily' },
              { label: '指标', table: 'index_enriched' },
            ] as FieldTab[]}
            onShowFields={(t) => setSchemaTable(t ?? 'index_daily')}
            onSettings={hasData ? () => setOpenSettings(v => v === 'index' ? null : 'index') : undefined}
            settingsOpen={openSettings === 'index'}
          />
        )
      case 'etf':
        return (
          <StatCard
            title="ETF"
            hint="场内基金 · 独立存储"
            stats={etfOverviewStats}
            loading={isLoading}
            tierKey="etf"
            capLimits={mergedCaps}
            customProvider={routeProviderDisplay(matrix.data, 'daily')}
            auto={etfAuto}
            manual={!autoSyncEnabled}
            subLabel="维表 · 日K · 指标"
            fieldTabs={[
              { label: '维表', table: 'etf_instruments' },
              { label: '日K', table: 'etf_daily' },
              { label: '指标', table: 'etf_enriched' },
            ] as FieldTab[]}
            onShowFields={(t) => setSchemaTable(t ?? 'etf_daily')}
          />
        )
      case 'minute':
        return (
          <StatCard
            title="1分K"
            hint="全市场同步"
            stats={s?.minute}
            loading={isLoading}
            active={activeCard === 'minute'}
            done={doneStages.has('minute')}
            skipped={skippedCards.has('minute')}
            stagePct={activeCard === 'minute' ? (job.data?.stage_pct ?? 0) : 0}
            tierKey="minute"
            capLimits={mergedCaps}
            customProvider={routeProviderDisplay(matrix.data, 'minute')}
            auto={klineConfigs.data?.['1m'].enabled ?? true}
            manual={!autoSyncEnabled}
            onShowFields={() => setSchemaTable('minute')}
            onSettings={() => setOpenSettings('minute')}
            settingsOpen={openSettings === 'minute'}
          />
        )
      case '5m':
      case '10m':
      case '15m':
      case 'weekly': {
        const period = k === 'weekly' ? '1w' : k
        return <StatCard
          title={k === 'weekly' ? '周K' : `${k.slice(0, -1)}分K`}
          hint={k === 'weekly' ? '由本地日K计算，不请求上游' : '由本地1分K计算，不请求上游'}
          stats={s?.[k]} loading={isLoading} tierKey={k === 'weekly' ? 'daily' : 'minute'}
          capLimits={mergedCaps} customProvider={isLocalDerivedPeriod(period) ? '本地计算' : routeProviderDisplay(matrix.data, 'daily')}
          auto={klineConfigs.data?.[period].enabled ?? true} manual={!autoSyncEnabled}
          active={activeCard === k} done={doneStages.has(k)}
          stagePct={activeCard === k ? job.data?.stage_pct ?? 0 : 0}
          onShowFields={() => setSchemaTable(k)} onSettings={() => setOpenSettings(k)}
          settingsOpen={openSettings === k}
        />
      }
      case 'financials': {
        const historicalShareRows = s?.financials?.tables?.shares?.rows ?? 0
        return (
          <StatCard
            title="财务数据"
            hint="财报 / 指标 / 历史股本"
            stats={s?.financials ? { rows: s.financials.rows } : null}
            loading={isLoading}
            tierKey="financials"
            capLimits={mergedCaps}
            customProvider={routeProviderDisplay(matrix.data, 'financial')}
            subLabel={`历史股本 · ${historicalShareRows.toLocaleString()} 条`}
            onSettings={hasData ? () => setOpenSettings(v => v === 'financials' ? null : 'financials') : undefined}
            settingsOpen={openSettings === 'financials'}
          />
        )
      }
      case 'regime':
        return (
          <StatCard
            title="环境"
            hint="每日环境状态 · 本地计算"
            stats={regimeCoverage.data ?? null}
            loading={regimeCoverage.isLoading}
            active={activeCard === 'regime'}
            done={doneStages.has('regime')}
            skipped={skippedCards.has('regime')}
            stagePct={activeCard === 'regime' ? (job.data?.stage_pct ?? 0) : 0}
            tierKey="regime"
            capLimits={mergedCaps}
            auto={prefs.data?.pipeline_regime_enabled ?? true}
            manual={!autoSyncEnabled}
            subLabel="状态 · 综合分 · 指标"
            onSettings={hasData ? () => setOpenSettings(v => v === 'regime' ? null : 'regime') : undefined}
            settingsOpen={openSettings === 'regime'}
          />
        )
      default:
        return null
    }
  }

  return (
    <>
      {showStockCatalog && <StockCatalogDialog onClose={() => setShowStockCatalog(false)} />}
      <div ref={topRef} />
      <PageHeader
        title="数据总览"
        subtitle={!hasData && !isLoading
          ? (isRunning ? '首次全量同步正在进行' : (autoSyncEnabled
            ? '初始化后自动同步已开启，可查看同步进度或点击立即同步重试'
            : '自动同步已关闭，请点击立即同步获取数据'))
          : '本地数据画像 · 同步状态 · 历史记录'}
        right={
          <div className="flex flex-wrap items-center gap-3 [&_button]:whitespace-nowrap">
            <button
              onClick={() => adjGate.guard(() => startSync.mutate())}
              disabled={isStarting}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-btn bg-gradient-to-r from-accent/25 to-accent/10 border border-accent/30 text-accent text-xs font-medium hover:from-accent/35 hover:to-accent/20 disabled:opacity-40 transition-all duration-150"
            >
              {(isStarting || isRunning) ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <Play className="h-3.5 w-3.5" />
              )}
              {isStarting ? '启动中…' : isRunning ? '同步中…' : '立即同步'}
            </button>
            <button
              onClick={() => setOpenSettings('page-settings')}
              className="inline-flex items-center gap-1 px-2 py-1 rounded-btn text-secondary hover:text-accent hover:bg-accent/8 text-xs transition-colors duration-150"
            >
              <SlidersHorizontal className="h-3.5 w-3.5" />
              页面设置
            </button>
            {canCancel && !!activeJobId && (
              <button
                onClick={() => setShowStopConfirm(true)}
                title="停止当前同步任务"
                className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-btn bg-danger/12 border border-danger/30 text-danger text-xs font-medium hover:bg-danger/20 transition-all duration-150"
              >
                <Square className="h-3 w-3 fill-current" />
                停止
              </button>
            )}
            <button
              onClick={() => setOpenSettings('pipeline-scope')}
              className="inline-flex items-center gap-1 px-2 py-1 rounded-btn text-secondary hover:text-accent hover:bg-accent/8 text-xs transition-colors duration-150"
            >
              <CheckSquare className="h-3.5 w-3.5" />
              数据范围
            </button>
            <div className="w-px h-4 bg-border" />
            <div className="flex flex-wrap items-center gap-1.5">
              <button
                onClick={() => setShowCreateExt(true)}
                className="inline-flex items-center gap-1 px-2 py-1 rounded-btn text-secondary hover:text-accent hover:bg-accent/8 text-xs transition-colors duration-150"
              >
                <Plus className="h-3.5 w-3.5" />
                扩展数据
              </button>
              <button
                onClick={() => setShowEndpointTest(true)}
                className="inline-flex items-center gap-1 px-2 py-1 rounded-btn text-secondary hover:text-accent hover:bg-accent/8 text-xs transition-colors duration-150"
              >
                <Wifi className="h-3.5 w-3.5" />
                测试端点
              </button>
              <div className="w-px h-4 bg-border" />
              <Link
                to="/settings?tab=data-sources&highlight=data-sources"
                className="inline-flex items-center gap-1 px-2 py-1 rounded-btn text-secondary hover:text-accent hover:bg-accent/8 text-xs transition-colors duration-150"
                title="切换数据源"
              >
                <Database className="h-3.5 w-3.5" />
                <span className="text-foreground/80 max-w-[120px] truncate">{activeDataSourceName}</span>
              </Link>
              <button
                onClick={() => setShowClearConfirm(true)}
                disabled={isRunning}
                className="inline-flex items-center gap-1 px-2 py-1 rounded-btn text-muted hover:text-danger hover:bg-danger/8 text-xs transition-colors duration-150 disabled:opacity-40 disabled:pointer-events-none"
              >
                <Trash2 className="h-3.5 w-3.5" />
                清除数据
              </button>
            </div>
          </div>
        }
      />

      <div className="mx-auto px-8 py-6 space-y-6 max-w-6xl">
        <DataReadiness quality={s?.quality} initialization={s?.initialization} />
        {/* 无 Key 提示 —— 非阻断: 历史日K走免费通道, 实时等能力取决于所选数据源 */}
        {isNoKey && (
          <div className="flex items-center gap-2 rounded-card border border-border bg-elevated/40 px-3 py-2 text-xs">
            <Info className="h-4 w-4 shrink-0 text-muted" />
            <span className="text-secondary leading-relaxed">
              当前无需 API Key,历史日K将使用免费通道获取。
              实时行情、分钟K等能力取决于所选数据源,可在
              <Link to="/settings?tab=data-sources&highlight=data-sources" className="mx-0.5 font-medium text-accent hover:underline">
                数据源设置
              </Link>
              中配置。
            </span>
          </div>
        )}

        {/* 实时进度 */}
        <AnimatePresence>
          {job.data && (
            <motion.div
              initial={{ opacity: 0, height: 0 }}
              animate={{ opacity: 1, height: 'auto' }}
              exit={{ opacity: 0, height: 0 }}
              transition={{ duration: 0.25, ease: [0.16, 1, 0.3, 1] }}
              className="overflow-hidden"
            >
              <ActiveJobCard job={job.data} />
            </motion.div>
          )}
        </AnimatePresence>

        {/* 数据同步 / 存储：桌面 3:1，窄屏纵向排列 */}
        <div className="grid grid-cols-1 xl:grid-cols-4 gap-4 items-start">
          <div className="min-w-0 xl:col-span-3">
            <ScheduleOverview
              onAutoSyncChange={(enabled) => updateAutoSync.mutate(enabled)}
              autoSyncDisabled={prefs.isLoading || prefs.isError || updateAutoSync.isPending}
              preferences={prefs.data}
              klineConfigs={klineConfigs.data}
              minuteRefresh={minuteRefresh.data}
              quoteStatus={quoteStatus.data}
              realtimeAvailable={realtimeAvailable}
              financialAvailable={usableOr('financial', !!tfCaps?.financial)}
              depthAvailable={usableOr('depth5', !!tfCaps?.['depth5.batch'])}
              extConfigs={extConfigs.data?.items ?? []}
            />
          </div>

          {/* 存储 */}
          <div className="min-w-0 rounded-card border border-border bg-surface p-4">
            <div className="flex items-center justify-between mb-3">
              <div className="flex items-center gap-2">
                <HardDrive className="h-4 w-4 text-secondary" />
                <h3 className="text-sm font-medium text-foreground">存储</h3>
              </div>
              {isLoading ? (
                <Skeleton w="w-12" />
              ) : (
                <span className="font-mono text-xs text-muted">{s ? `${s.storage.total_size_mb} MB` : '—'}</span>
              )}
            </div>
            <div className="space-y-2">
              {isLoading ? (
                Array.from({ length: 7 }).map((_, i) => (
                  <div key={i} className="flex items-center justify-between">
                    <Skeleton w="w-10" />
                    <div className="flex items-center gap-3">
                      <Skeleton w="w-14" />
                      <Skeleton w="w-16" />
                    </div>
                  </div>
                ))
              ) : [
                { label: '个股维表', files: s?.storage.instruments_files, size: s?.storage.instruments_size_mb },
                { label: '1分K',   files: s?.storage.minute_files,      size: s?.storage.minute_size_mb },
                ...(['5m', '10m', '15m'] as const).map(period => ({
                  label: `${period.slice(0, -1)}分K`, files: s?.storage[`${period}_files`], size: s?.storage[`${period}_size_mb`],
                })),
                { label: '日K',     files: s?.storage.daily_files,       size: s?.storage.daily_size_mb },
                { label: '周K', files: s?.storage.weekly_files, size: s?.storage.weekly_size_mb },
                { label: '除权因子', files: s?.storage.adj_factor_files,  size: s?.storage.adj_factor_size_mb },
                { label: 'Enriched', files: s?.storage.enriched_files,    size: s?.storage.enriched_size_mb },
                { label: '财务数据', files: s?.storage.financials_files,   size: s?.storage.financials_size_mb },
                { label: '扩展数据', files: s?.storage.ext_data_files,    size: s?.storage.ext_data_size_mb },
              ].map((item) => (
                <div key={item.label} className="flex flex-wrap items-center justify-between gap-x-2 gap-y-1 text-[11px]">
                  <span className="text-muted">{item.label}</span>
                  <div className="flex items-center gap-3">
                    <span className="font-mono text-secondary">{item.files ?? 0} 文件</span>
                    <span className="font-mono text-muted w-16 text-right">{(item.size ?? 0).toFixed(1)} MB</span>
                  </div>
                </div>
              ))}
            </div>
          </div>
        </div>

        {/* 数据画像 */}
        <div>
          <SectionTitle icon={Database}>数据画像</SectionTitle>
          <div className="mt-3 grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-5 gap-4 items-stretch">
            {getCardOrder().filter(k => cardVisible[k]).map((k: CardKey) => (
              <Fragment key={k}>{renderStatCard(k)}</Fragment>
            ))}
            {[...(extConfigs.data?.items ?? [])].sort((a, b) => {
              const rank = (name: string) => name.includes('概念') ? 0 : name.includes('行业') ? 1 : 2
              return rank(a.label) - rank(b.label)
            }).map((ext) => (
              <ExtDataStatCard
                key={ext.id}
                config={ext}
                onDelete={() => deleteExt.mutate(ext.id)}
                deleting={deleteExt.isPending}
                onEdit={() => setEditingExt(ext)}
              />
            ))}
          </div>
        </div>

        {/* 同步历史 */}
        <div>
          <SectionTitle icon={Clock}>同步历史</SectionTitle>
          <div className="mt-3 rounded-card border border-border overflow-hidden">
            {history.isLoading ? (
              <div className="px-5 py-6 space-y-3">
                {Array.from({ length: 3 }).map((_, i) => (
                  <div key={i} className="flex items-center justify-between">
                    <div className="flex items-center gap-3">
                      <Skeleton w="w-4" h="h-4" rounded="rounded-full" />
                      <div className="space-y-1.5">
                        <Skeleton w="w-20" />
                        <Skeleton w="w-28" h="h-3" />
                      </div>
                    </div>
                    <Skeleton w="w-32" />
                  </div>
                ))}
              </div>
            ) : history.data && history.data.jobs.length > 0 ? (
              <div className="divide-y divide-border">
                {history.data.jobs.map((j) => (
                  <HistoryRow key={j.id} job={j} onClick={() => handleJobClick(j.id)} />
                ))}
              </div>
            ) : (
              <div className="px-5 py-8 text-center text-sm text-muted">
                暂无同步记录 — 点右上角"立即同步"开始。
              </div>
            )}
          </div>
        </div>

        {startSync.isError && (
          <div className="rounded-btn border border-danger/40 bg-danger/5 px-3 py-2 text-sm text-danger">
            启动失败:{String((startSync.error as any).message)}
          </div>
        )}
      </div>

      {/* 弹窗 */}
      <EnrichedSchemaModal
        table={schemaTable}
        onClose={() => setSchemaTable(null)}
      />

      {showEndpointTest && (
        <EndpointTestDialog
          hasKey={settings.data?.mode === 'api_key'}
          tierLabel={settings.data?.tier_label ?? ''}
          currentEndpoint={settings.data?.current_endpoint ?? ''}
          onClose={() => setShowEndpointTest(false)}
        />
      )}

      <AnimatePresence>
        {showCreateExt && (
          <CreateExtDialog onClose={() => setShowCreateExt(false)} />
        )}
        {editingExt && (
          <EditExtDialog config={editingExt} onClose={() => setEditingExt(null)} />
        )}
      </AnimatePresence>

      <AnimatePresence>
        {openSettings && ['minute', '5m', '10m', '15m', 'daily', 'weekly'].includes(openSettings) && (
          <SettingsModal title={`${({ minute: '1分K', daily: '日K', weekly: '周K' } as Record<string, string>)[openSettings] ?? `${openSettings.slice(0, -1)}分K`} · 同步设置`} onClose={() => setOpenSettings(null)}>
            <KlineSyncConfig
              period={(({ minute: '1m', daily: '1d', weekly: '1w' } as Record<string, KlinePeriod>)[openSettings] ?? openSettings) as KlinePeriod}
              hasCap={openSettings === 'daily' ? hasDailyBatchCap : isLocalDerivedPeriod(openSettings) || hasMinuteCap}
              isRunning={!!activeJobId}
              earliestDate={s?.[openSettings as 'minute' | 'daily' | 'weekly' | '5m' | '10m' | '15m']?.earliest_date ?? null}
              onJobStart={id => { setActiveJobId(id); setOpenSettings(null) }}
            />
          </SettingsModal>
        )}
      </AnimatePresence>

      <AnimatePresence>
        {openSettings === 'enriched' && (
          <SettingsModal title="Enriched · 计算设置" onClose={() => setOpenSettings(null)}>
            <EnrichedRebuildPanel
              isRunning={!!activeJobId}
              onStart={(jobId) => { setActiveJobId(jobId); setOpenSettings(null) }}
            />
          </SettingsModal>
        )}
      </AnimatePresence>

      <AnimatePresence>
        {openSettings === 'financials' && (
          <SettingsModal title="财务数据 · 换手率重算" onClose={() => setOpenSettings(null)}>
            <EnrichedRebuildPanel
              isRunning={!!activeJobId}
              purpose="turnover"
              historicalShareRows={s?.financials?.tables?.shares?.rows ?? 0}
              onStart={(jobId) => { setActiveJobId(jobId); setOpenSettings(null) }}
            />
          </SettingsModal>
        )}
      </AnimatePresence>

      <AnimatePresence>
        {openSettings === 'regime' && (
          <SettingsModal title="环境 · 计算设置" onClose={() => setOpenSettings(null)}>
            <RegimeConfigCard />
          </SettingsModal>
        )}
      </AnimatePresence>

      <AnimatePresence>
        {openSettings === 'pipeline-scope' && (
          <SettingsModal title="盘后管道 · 拉取内容" onClose={() => setOpenSettings(null)}>
            <PipelineScopeConfig />
          </SettingsModal>
        )}
      </AnimatePresence>

      <AnimatePresence>
        {openSettings === 'page-settings' && (
          <SettingsModal title="页面设置 · 数据画像卡片" onClose={() => setOpenSettings(null)}>
            <PageSettingsModal caps={mergedCaps} />
          </SettingsModal>
        )}
      </AnimatePresence>

      <AnimatePresence>
        {openSettings === 'index' && (
          <SettingsModal title="指数 · 手动获取" onClose={() => setOpenSettings(null)}>
            <div className="space-y-4">
              <div className="rounded-card border border-border bg-base/30 p-4 space-y-3">
                <div>
                  <div className="text-sm font-medium text-foreground">指数日 K</div>
                  <div className="text-[11px] text-muted mt-1">获取数据时会先刷新 CN_Index 维表，再向前扩展指数历史；指数不需要复权。</div>
                </div>
                <div className="flex items-center gap-2">
                  <div className="flex items-center">
                    <button
                      onClick={() => setIndexExtendValue(v => Math.max(1, v - 1))}
                      disabled={!hasDailyBatchCap || !!activeJobId || syncIndexDaily.isPending}
                      className="h-6 w-6 flex items-center justify-center rounded-l-btn bg-elevated border border-border text-secondary hover:bg-border/50 disabled:opacity-30 transition-colors text-xs"
                    >−</button>
                    <div className="h-6 w-8 flex items-center justify-center border-y border-border text-[11px] font-mono tabular-nums text-foreground bg-base">
                      {indexExtendValue}
                    </div>
                    <button
                      onClick={() => setIndexExtendValue(v => Math.min(indexExtendUnit === 'year' ? 10 : 36, v + 1))}
                      disabled={!hasDailyBatchCap || !!activeJobId || syncIndexDaily.isPending}
                      className="h-6 w-6 flex items-center justify-center rounded-r-btn bg-elevated border border-border text-secondary hover:bg-border/50 disabled:opacity-30 transition-colors text-xs"
                    >+</button>
                  </div>

                  <div className="flex rounded-btn border border-border overflow-hidden">
                    {(['month', 'year'] as const).map(u => (
                      <button
                        key={u}
                        onClick={() => { setIndexExtendUnit(u); if (u === 'year' && indexExtendValue > 10) setIndexExtendValue(1); if (u === 'month' && indexExtendValue > 36) setIndexExtendValue(6) }}
                        disabled={!hasDailyBatchCap || !!activeJobId || syncIndexDaily.isPending}
                        className={`px-2 py-0.5 text-[10px] font-medium transition-colors disabled:opacity-40 ${
                          indexExtendUnit === u ? 'bg-accent/15 text-accent' : 'text-secondary hover:bg-elevated'
                        }`}
                      >{u === 'month' ? '月' : '年'}</button>
                    ))}
                  </div>
                </div>

                <div className="text-[10px] text-muted">
                  预计扩展至 <span className="font-mono text-secondary">{indexTargetDateText}</span>
                  {indexEarliestDate && <span> (当前最早: <span className="font-mono text-secondary">{indexEarliestDate}</span>)</span>}
                </div>

                <div className="rounded-btn border border-border bg-base/40 p-3 space-y-2">
                  <div className="flex items-center justify-between gap-3">
                    <div>
                      <div className="text-xs font-medium text-foreground">批次大小</div>
                      <div className="text-[10px] text-muted mt-0.5">每批同步并计算的指数数量，默认 100。</div>
                    </div>
                    <div className="flex items-center gap-2">
                      <input
                        type="number"
                        min={1}
                        max={10000}
                        value={indexBatchInput}
                        onChange={e => setIndexBatchInput(e.target.value)}
                        disabled={updateIndexBatchSize.isPending || !!activeJobId || syncIndexDaily.isPending}
                        className="w-20 px-2 py-1 rounded-btn bg-elevated border border-border text-xs font-mono text-foreground outline-none focus:border-accent disabled:opacity-40"
                      />
                      <button
                        onClick={() => {
                          const size = Math.max(1, Math.min(10000, Number(indexBatchInput) || 100))
                          setIndexBatchInput(String(size))
                          updateIndexBatchSize.mutate(size)
                        }}
                        disabled={updateIndexBatchSize.isPending || !!activeJobId || syncIndexDaily.isPending}
                        className="px-2.5 py-1 rounded-btn bg-elevated border border-border text-xs text-secondary hover:text-foreground disabled:opacity-40 transition-colors"
                      >
                        {updateIndexBatchSize.isPending ? '保存中…' : '保存'}
                      </button>
                    </div>
                  </div>
                  <div className="text-[10px] text-muted">
                    当前生效: <span className="font-mono text-secondary">{indexDailyBatchSize}</span>
                  </div>
                </div>
                <button
                  onClick={() => syncIndexDaily.mutate()}
                  disabled={!hasDailyBatchCap || !!activeJobId || syncIndexDaily.isPending}
                  className="w-full inline-flex items-center justify-center gap-1.5 px-3 py-1.5 rounded-btn bg-accent/90 text-white text-xs font-medium hover:bg-accent disabled:opacity-40 disabled:pointer-events-none transition-colors duration-150"
                >
                  {syncIndexDaily.isPending ? (
                    <>
                      <Loader2 className="h-3 w-3 animate-spin" />
                      获取中…
                    </>
                  ) : (
                    <>获取数据</>
                  )}
                </button>
                {!hasDailyBatchCap && (
                  <MissingCapChip capKey="kline.daily.batch" />
                )}
              </div>
            </div>
          </SettingsModal>
        )}
      </AnimatePresence>

      {/* 停止同步二次确认弹窗 */}
      <AnimatePresence>
        {showStopConfirm && (
          <Modal onClose={() => setShowStopConfirm(false)} ariaLabel="停止数据同步" size="sm" closeDisabled={stopSync.isPending} panelClassName="p-5">
              <div className="flex items-start gap-3">
                <div className="shrink-0 h-10 w-10 rounded-full bg-danger/12 flex items-center justify-center">
                  <AlertTriangle className="h-5 w-5 text-danger" />
                </div>
                <div className="min-w-0">
                  <h3 className="text-sm font-semibold text-foreground mb-1.5">确认停止数据同步？</h3>
                  <p className="text-xs text-secondary leading-relaxed">
                    当前任务将在<span className="text-foreground font-medium">正在拉取的分块完成后</span>中断（协作式停止，不会损坏已写入的数据）。
                  </p>
                  <p className="mt-2 text-[11px] text-danger/90 leading-relaxed">
                    停止后再次拉取需要重新走完整管道（拉取 → 指标计算 → 监控规则），无法从停止处续跑。
                  </p>
                  <div className="mt-2 flex items-start gap-1.5 text-[11px] text-muted">
                    <Info className="h-3.5 w-3.5 shrink-0 mt-px text-muted" />
                    <span>已写入的部分数据会保留，下次同步时覆盖或补齐。</span>
                  </div>
                </div>
              </div>
              <div className="flex items-center justify-end gap-2 mt-5">
                <button
                  onClick={() => setShowStopConfirm(false)}
                  disabled={stopSync.isPending}
                  className="px-3 py-1.5 rounded-btn bg-elevated text-secondary hover:bg-elevated/80 text-sm transition-colors disabled:opacity-50"
                >
                  取消
                </button>
                <button
                  onClick={() => stopSync.mutate()}
                  disabled={stopSync.isPending}
                  className="px-3 py-1.5 rounded-btn bg-danger/90 text-white text-sm font-medium hover:bg-danger disabled:opacity-50 transition-colors"
                >
                  {stopSync.isPending ? '停止中…' : '确认停止'}
                </button>
              </div>
          </Modal>
        )}
      </AnimatePresence>

      {/* 清除数据二次确认弹窗 */}
      <AnimatePresence>
        {adjGate.dialog}
        {showClearConfirm && (
          <Modal onClose={() => setShowClearConfirm(false)} ariaLabel="清除本地数据" size="sm" closeDisabled={clearData.isPending} panelClassName="p-5">
              <div className="flex items-start gap-3">
                <div className="shrink-0 h-10 w-10 rounded-full bg-danger/12 flex items-center justify-center">
                  <AlertTriangle className="h-5 w-5 text-danger" />
                </div>
                <div className="min-w-0">
                  <h3 className="text-sm font-semibold text-foreground mb-1.5">确认清除本地数据？</h3>
                  <p className="text-xs text-secondary leading-relaxed">
                    此操作将<span className="text-danger font-medium">永久删除</span>所有已同步的本地数据，包括：
                  </p>
                  <ul className="mt-2 text-[11px] text-muted leading-relaxed space-y-0.5">
                    <li>· 个股维表、日 K、除权因子</li>
                    <li>· Enriched 指标数据、分钟 K</li>
                    <li>· 财务数据、指数、ETF</li>
                  </ul>
                  <p className="mt-2 text-[11px] text-danger/90">
                    操作不可恢复，需重新执行同步才能恢复数据。
                  </p>
                  <div className="mt-2 flex items-start gap-1.5 text-[11px] text-warning">
                    <Info className="h-3.5 w-3.5 shrink-0 mt-px text-warning" />
                    <span>此操作不会清除扩展数据，如需删除请在扩展数据设置中单独操作。</span>
                  </div>
                </div>
              </div>
              <div className="flex items-center justify-end gap-2 mt-5">
                <button
                  onClick={() => setShowClearConfirm(false)}
                  disabled={clearData.isPending}
                  className="px-3 py-1.5 rounded-btn bg-elevated text-secondary hover:bg-elevated/80 text-sm transition-colors disabled:opacity-50"
                >
                  取消
                </button>
                <button
                  onClick={() => clearData.mutate()}
                  disabled={clearData.isPending}
                  className="px-3 py-1.5 rounded-btn bg-danger/90 text-white text-sm font-medium hover:bg-danger disabled:opacity-50 transition-colors"
                >
                  {clearData.isPending ? '清除中…' : '清除数据'}
                </button>
              </div>
          </Modal>
        )}
      </AnimatePresence>
    </>
  )
}
