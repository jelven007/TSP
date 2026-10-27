import {
  Activity,
  Archive,
  CalendarClock,
  Database,
  FolderSync,
  RefreshCw,
  TimerReset,
} from 'lucide-react'

type ScheduleState = 'running' | 'enabled' | 'partial' | 'disabled' | 'unavailable'
export type ScheduleCategory = 'foundation' | 'market' | 'kline' | 'maintenance' | 'extension'

const CATEGORY_ORDER: ScheduleCategory[] = [
  'foundation',
  'market',
  'kline',
  'maintenance',
  'extension',
]

const CATEGORY_META: Record<ScheduleCategory, { label: string; detail: string }> = {
  foundation: { label: '基础与完整性', detail: '目录同步与启动完整性检查' },
  market: { label: '实时行情与快照', detail: '盘中行情、竞价和五档盘口' },
  kline: { label: 'K线采集与加工', detail: '盘中增量、周期K线和盘后管道' },
  maintenance: { label: '财务与能力保障', detail: '财务指标与数据源可用性' },
  extension: { label: '扩展数据', detail: '按各扩展数据源的拉取配置执行' },
}

const BUILTIN_MARKET_HOURS_EXTENSIONS = new Set(['ext_gn_ths', 'ext_hy_ths'])

type SchedulePreferences = {
  data_sync_auto_enabled?: boolean
  realtime_quotes_enabled?: boolean
  second_quote_archive_enabled?: boolean
  second_quote_archive_window?: 'full' | 'preopen'
  minute_refresh_enabled?: boolean
  minute_refresh_interval?: number
  limit_ladder_monitor_enabled?: boolean
  depth_polling_interval?: number
  depth_finalize_time?: { hour: number; minute: number }
  pipeline_schedule?: { hour: number; minute: number }
  pipeline_pull_a_share?: boolean
  pipeline_pull_etf?: boolean
  pipeline_pull_index?: boolean
  pipeline_regime_enabled?: boolean
}

type KlineConfig = {
  enabled: boolean
  interval_seconds: number
  windows: readonly (readonly [string, string])[]
  time: string
}

type MinuteRefreshStatus = {
  available?: boolean
  capability_ok?: boolean
  enabled?: boolean
  running?: boolean
  effective_interval_seconds?: number
  gate_reason?: string | null
}

type QuoteStatus = {
  running?: boolean
  is_trading_hours?: boolean
  interval_s?: number
}

type ScheduleExtConfig = {
  id: string
  label: string
  pull?: {
    url: string
    enabled: boolean
    schedule_minutes: number
    time_window_start?: string | null
    time_window_end?: string | null
  } | null
}

export type DataScheduleItem = {
  id: string
  category: ScheduleCategory
  label: string
  detail: string
  schedule: string
  state: ScheduleState
}

export function buildDataScheduleItems({
  preferences,
  klineConfigs,
  minuteRefresh,
  quoteStatus,
  realtimeAvailable,
  financialAvailable,
  depthAvailable,
  extConfigs,
}: {
  preferences?: SchedulePreferences
  klineConfigs?: Partial<Record<'1m' | '5m' | '10m' | '15m' | '1d' | '1w', KlineConfig>>
  minuteRefresh?: MinuteRefreshStatus
  quoteStatus?: QuoteStatus
  realtimeAvailable?: boolean
  financialAvailable: boolean
  depthAvailable: boolean
  extConfigs: ScheduleExtConfig[]
}): DataScheduleItem[] {
  const auto = preferences?.data_sync_auto_enabled ?? true
  const quoteEnabled = preferences?.realtime_quotes_enabled ?? true
  const pipelineAt = preferences?.pipeline_schedule ?? { hour: 16, minute: 0 }
  const depthAt = preferences?.depth_finalize_time ?? { hour: 15, minute: 2 }
  const minuteInterval = minuteRefresh?.effective_interval_seconds
    ?? preferences?.minute_refresh_interval
    ?? klineConfigs?.['1m']?.interval_seconds
    ?? 60
  const stateForAuto = (enabled = true, available = true): ScheduleState => {
    if (!available) return 'unavailable'
    return auto && enabled ? 'enabled' : 'disabled'
  }
  const formatClock = ({ hour, minute }: { hour: number; minute: number }) =>
    `${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}`
  const dailyTime = klineConfigs?.['1d']?.time ?? formatClock(pipelineAt)
  const interval = (seconds: number) =>
    seconds >= 60 && seconds % 60 === 0 ? `每 ${seconds / 60} 分钟` : `每 ${seconds} 秒`
  const intradayWindow = (config?: KlineConfig) =>
    config?.windows.map(([start, end]) => `${start}–${end}`).join(' / ') ?? '配置的交易时段'
  const minuteAvailable = Boolean(minuteRefresh?.available ?? true) && Boolean(minuteRefresh?.capability_ok ?? true)
  const minuteEnabled = Boolean(klineConfigs?.['1m']?.enabled ?? preferences?.minute_refresh_enabled ?? true)
  const minuteState: ScheduleState = !minuteAvailable
    ? 'unavailable'
    : !auto || !minuteEnabled
      ? 'disabled'
      : minuteRefresh?.running && minuteRefresh.gate_reason == null
        ? 'running'
        : 'enabled'

  const items: DataScheduleItem[] = [
    {
      id: 'catalog',
      category: 'foundation',
      label: '个股维表',
      detail: '官方目录同步；启动补跑与每日任务共用同一份目录任务。',
      schedule: '每天 09:00（启动时补跑）',
      state: 'enabled',
    },
    {
      id: 'startup-integrity',
      category: 'foundation',
      label: '数据完整性',
      detail: '检查近期分钟快照与分区缺口；仅发现问题时才创建修复任务。',
      schedule: '应用启动后 30 秒',
      state: stateForAuto(),
    },
    {
      id: 'auction',
      category: 'market',
      label: '集合竞价',
      detail: '多次采集与发布重试合并展示，避免把同一份竞价数据重复计数。',
      schedule: '交易日 09:25:08 / 25 / 45，09:29:15 定版',
      state: stateForAuto(),
    },
    {
      id: 'quotes',
      category: 'market',
      label: '实时行情',
      detail: `${quoteStatus?.running
        ? (quoteStatus.is_trading_hours ? '行情轮询运行中；' : '服务已启动，等待交易时段；')
        : '按行情开关运行；'}交易日 09:15–11:45、13:00–16:00 轮询，午间暂停，15:00 后继续确认收盘定版。`,
      schedule: `交易日 09:15–11:45 / 13:00–16:00 · ${interval(quoteStatus?.interval_s ?? 3)}`,
      state: realtimeAvailable === false
        ? 'unavailable'
        : !quoteEnabled
          ? 'disabled'
          : quoteStatus?.running
            ? 'running'
            : 'enabled',
    },
    {
      id: 'second-quote-archive',
      category: 'market',
      label: '秒级行情',
      detail: '由实时行情采集驱动，写入时按保留期清理过期分区，不单独再起轮询任务。',
      schedule: preferences?.second_quote_archive_window === 'preopen'
        ? '交易日 09:15–09:25'
        : '随实时行情采集窗口',
      state: realtimeAvailable === false
        ? 'unavailable'
        : quoteEnabled && (preferences?.second_quote_archive_enabled ?? true)
          ? 'enabled'
          : 'disabled',
    },
    {
      id: 'minute-refresh',
      category: 'kline',
      label: '1分K盘中刷新',
      detail: '全市场增量落盘；冷启动、断档或空轮时自动改为全天修复。',
      schedule: `${intradayWindow(klineConfigs?.['1m'])} · ${interval(minuteInterval)}`,
      state: minuteState,
    },
  ]

  for (const period of ['5m', '10m', '15m'] as const) {
    const config = klineConfigs?.[period]
    items.push({
      id: `period-${period}`,
      category: 'kline',
      label: `${period.slice(0, -1)}分K周期同步`,
      detail: '由本地1分K聚合，不向上游重复请求。',
      schedule: `${intradayWindow(config)} · ${interval(config?.interval_seconds ?? Number(period.slice(0, -1)) * 60)}`,
      state: stateForAuto(config?.enabled ?? true),
    })
  }

  items.push(
    {
      id: 'period-1d',
      category: 'kline',
      label: '日K周期同步',
      detail: '随盘后数据管道同步日K，共用日K周期开关与执行时间。',
      schedule: `交易日 ${dailyTime}`,
      state: stateForAuto(klineConfigs?.['1d']?.enabled ?? true),
    },
    {
      id: 'period-1w',
      category: 'kline',
      label: '周K周期同步',
      detail: '每周六由本地日K聚合，不向上游重复请求。',
      schedule: `每周六 ${klineConfigs?.['1w']?.time ?? '16:00'} 后`,
      state: stateForAuto(klineConfigs?.['1w']?.enabled ?? true),
    },
    {
      id: 'daily-pipeline',
      category: 'kline',
      label: '盘后数据管道',
      detail: [
        '首次启动按需补齐',
        preferences?.pipeline_pull_a_share ?? true ? '日K' : null,
        '除权与指标',
        preferences?.pipeline_pull_index ?? true ? '指数' : null,
        preferences?.pipeline_pull_etf ?? true ? 'ETF' : null,
        preferences?.pipeline_regime_enabled ?? true ? '环境' : null,
        klineConfigs?.['1m']?.enabled ?? preferences?.minute_refresh_enabled ?? true ? '历史分钟K' : null,
      ].filter(Boolean).join(' · '),
      schedule: `交易日 ${dailyTime}`,
      state: stateForAuto(klineConfigs?.['1d']?.enabled ?? true),
    },
    {
      id: 'depth',
      category: 'market',
      label: '五档盘口',
      detail: !auto && preferences?.limit_ladder_monitor_enabled
        ? '盘中监控仍按实时行情开关运行；盘后定版受自动同步总开关暂停。'
        : preferences?.limit_ladder_monitor_enabled
          ? '盘中只采集涨跌停候选；盘后将最终盘口定版写入本地。'
        : '盘中监控未开启；保留盘后定版任务。',
      schedule: `盘中 ${interval(preferences?.depth_polling_interval ?? 10)}（监控开启时） · 交易日 ${formatClock(depthAt)} 定版`,
      state: !depthAvailable
        ? 'unavailable'
        : !auto && preferences?.limit_ladder_monitor_enabled
          ? 'partial'
          : stateForAuto(),
    },
    {
      id: 'financial-metrics',
      category: 'maintenance',
      label: '财务指标',
      detail: '只自动更新 metrics；其余财务表保持手动同步。',
      schedule: '启动后 60 秒，之后每 7 天',
      state: stateForAuto(true, financialAvailable),
    },
    {
      id: 'capability-reprobe',
      category: 'maintenance',
      label: '数据源能力',
      detail: '重新探测当前数据源能力，避免密钥或数据源变更后继续沿用旧状态。',
      schedule: '每 60 分钟',
      state: 'enabled',
    },
  )

  const extIds = new Set<string>()
  for (const config of extConfigs) {
    const pull = config.pull
    const id = `ext:${config.id}`
    if (!pull || extIds.has(id)) continue
    extIds.add(id)
    const usesUnifiedTradingWindow = BUILTIN_MARKET_HOURS_EXTENSIONS.has(config.id)
    items.push({
      id,
      category: 'extension',
      label: `扩展数据 · ${config.label}`,
      detail: usesUnifiedTradingWindow
        ? '仅在交易日 09:15–11:45 / 13:00–16:00 拉取。'
        : pull.time_window_start || pull.time_window_end
        ? `仅在 ${pull.time_window_start ?? '00:00'}–${pull.time_window_end ?? '24:00'} 拉取。`
        : '按该扩展数据源的拉取配置执行。',
      schedule: `启用后立即拉取，后每 ${pull.schedule_minutes} 分钟`,
      state: pull.enabled && Boolean(pull.url) ? 'enabled' : 'disabled',
    })
  }

  return items
}

const STATE_STYLE: Record<ScheduleState, { label: string; className: string }> = {
  running: { label: '运行中', className: 'border-accent/35 bg-accent/10 text-accent' },
  enabled: { label: '已启用', className: 'border-border bg-elevated text-secondary' },
  partial: { label: '部分暂停', className: 'border-warning/30 bg-warning/10 text-warning' },
  disabled: { label: '已暂停', className: 'border-border bg-base text-muted' },
  unavailable: { label: '不可用', className: 'border-warning/30 bg-warning/10 text-warning' },
}

function ScheduleIcon({ id }: { id: string }) {
  const cls = 'h-3.5 w-3.5'
  if (id === 'quotes') return <Activity className={cls} />
  if (id === 'second-quote-archive') return <Archive className={cls} />
  if (id === 'daily-pipeline') return <FolderSync className={cls} />
  if (id === 'capability-reprobe') return <RefreshCw className={cls} />
  if (id.startsWith('ext:')) return <Database className={cls} />
  if (id.startsWith('period-') || id === 'minute-refresh') return <TimerReset className={cls} />
  return <CalendarClock className={cls} />
}

export function ScheduleOverview(props: Parameters<typeof buildDataScheduleItems>[0] & {
  onAutoSyncChange: (enabled: boolean) => void
  autoSyncDisabled: boolean
}) {
  const items = buildDataScheduleItems(props)
  const autoSyncEnabled = props.preferences?.data_sync_auto_enabled ?? true
  const enabledCount = items.filter(item => item.state === 'enabled' || item.state === 'running').length
  const groupedItems = CATEGORY_ORDER
    .map(category => ({
      category,
      ...CATEGORY_META[category],
      items: items.filter(item => item.category === category),
    }))
    .filter(group => group.items.length > 0)

  return (
    <section aria-label="数据同步" className="min-w-0 rounded-card border border-border bg-surface p-4">
      <div className="mb-3">
        <div className="flex items-start justify-between gap-3">
          <div className="flex items-center gap-2">
            <CalendarClock className="h-4 w-4 text-secondary" />
            <h3 className="text-sm font-medium text-foreground">数据同步</h3>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            <span className="text-[11px] text-secondary">自动同步总开关</span>
            <button
              type="button"
              role="switch"
              aria-label="自动同步"
              aria-checked={autoSyncEnabled}
              onClick={() => props.onAutoSyncChange(!autoSyncEnabled)}
              disabled={props.autoSyncDisabled}
              className={`relative inline-flex h-4 w-7 shrink-0 items-center rounded-full transition-colors duration-200 ${
                autoSyncEnabled ? 'bg-accent shadow-[0_0_6px_rgba(59,130,246,0.3)]' : 'bg-elevated'
              } ${props.autoSyncDisabled ? 'cursor-not-allowed opacity-50' : 'cursor-pointer'}`}
            >
              <span className={`inline-block h-3 w-3 rounded-full bg-white shadow-sm transition-transform duration-200 ${
                autoSyncEnabled ? 'translate-x-[14px]' : 'translate-x-0.5'
              }`} />
            </button>
          </div>
        </div>
        <p className="mt-2 text-[10px] leading-relaxed text-muted">
          关闭后保留任务清单并标注暂停项；个股维表与数据源能力继续运行，实时行情独立控制。
        </p>
        <div className="mt-1 flex flex-wrap items-baseline gap-x-2 gap-y-1 text-[10px]">
          <span className="text-muted">按数据生命周期分类；每项标明自动采集周期。</span>
          <span className="font-mono text-secondary">{enabledCount}/{items.length} 已启用</span>
        </div>
      </div>
      <div className="space-y-3">
        {groupedItems.map(group => (
          <div key={group.category}>
            <div className="mb-1.5 flex items-baseline justify-between gap-3 px-0.5">
              <div className="min-w-0">
                <span className="text-[11px] font-medium text-secondary">{group.label}</span>
                <span className="ml-1.5 text-[10px] text-muted">{group.detail}</span>
              </div>
              <span className="shrink-0 font-mono text-[10px] text-muted">{group.items.length} 项</span>
            </div>
            <div className="divide-y divide-border/60 border-y border-border/60">
              {group.items.map(item => {
                const state = STATE_STYLE[item.state]
                return (
                  <div key={item.id} className="grid grid-cols-[auto_minmax(0,1fr)_auto] gap-x-2 py-2.5 first:pt-2 last:pb-2">
                    <span className="mt-0.5 text-secondary"><ScheduleIcon id={item.id} /></span>
                    <div className="min-w-0">
                      <div className="text-xs font-medium text-foreground">{item.label}</div>
                      <p className="mt-0.5 text-[10px] leading-relaxed text-muted">{item.detail}</p>
                      <div className="mt-1 flex flex-wrap items-baseline gap-x-1.5 font-mono text-[10px] text-secondary">
                        <span className="text-muted">自动采集周期</span>
                        <span>{item.schedule}</span>
                      </div>
                    </div>
                    <span className={`mt-0.5 h-fit whitespace-nowrap rounded-full border px-1.5 py-0.5 text-[10px] ${state.className}`}>
                      {state.label}
                    </span>
                  </div>
                )
              })}
            </div>
          </div>
        ))}
      </div>
    </section>
  )
}
