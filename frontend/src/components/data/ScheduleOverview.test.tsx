import { describe, expect, it } from 'vitest'
import { buildDataScheduleItems } from './ScheduleOverview'

const klineConfigs = {
  '1m': { enabled: true, interval_seconds: 60, windows: [['09:15', '11:45'], ['13:00', '16:00']], time: '16:00' },
  '5m': { enabled: true, interval_seconds: 300, windows: [['09:15', '11:45'], ['13:00', '16:00']], time: '16:00' },
  '10m': { enabled: true, interval_seconds: 600, windows: [['09:15', '11:45'], ['13:00', '16:00']], time: '16:00' },
  '15m': { enabled: true, interval_seconds: 900, windows: [['09:15', '11:45'], ['13:00', '16:00']], time: '16:00' },
  '1d': { enabled: true, interval_seconds: 60, windows: [['09:15', '11:45'], ['13:00', '16:00']], time: '16:00' },
  '1w': { enabled: true, interval_seconds: 60, windows: [['09:15', '11:45'], ['13:00', '16:00']], time: '16:10' },
} as const

const preferences = {
  data_sync_auto_enabled: true,
  realtime_quotes_enabled: true,
  second_quote_archive_enabled: true,
  second_quote_archive_window: 'preopen',
  minute_refresh_enabled: true,
  minute_refresh_interval: 60,
  limit_ladder_monitor_enabled: true,
  depth_polling_interval: 10,
  depth_finalize_time: { hour: 15, minute: 2 },
  pipeline_schedule: { hour: 16, minute: 0 },
  pipeline_pull_a_share: true,
  pipeline_pull_index: true,
  pipeline_regime_enabled: true,
} as const

describe('buildDataScheduleItems', () => {
  it('classifies each data task once and reports its automatic collection cycle', () => {
    const items = buildDataScheduleItems({
      preferences,
      klineConfigs,
      minuteRefresh: {
        available: true,
        capability_ok: true,
        enabled: true,
        running: true,
        effective_interval_seconds: 60,
      },
      quoteStatus: { running: true, is_trading_hours: true, interval_s: 3 },
      realtimeAvailable: true,
      financialAvailable: true,
      depthAvailable: true,
      extConfigs: [
        { id: 'concept', label: '概念板块', pull: { url: 'https://example.test/concepts', enabled: true, schedule_minutes: 1 } },
        { id: 'industry', label: '行业板块', pull: { url: 'https://example.test/industries', enabled: false, schedule_minutes: 5 } },
        { id: 'concept', label: '概念板块（重复配置）', pull: { url: 'https://example.test/concepts', enabled: true, schedule_minutes: 1 } },
      ],
    })

    expect(items.map(item => item.id)).toEqual([
      'catalog',
      'auction',
      'quotes',
      'second-quote-archive',
      'minute-refresh',
      'period-5m',
      'period-10m',
      'period-15m',
      'period-1d',
      'period-1w',
      'daily-pipeline',
      'depth',
      'financial-metrics',
      'capability-reprobe',
      'ext:concept',
      'ext:industry',
    ])
    expect(new Set(items.map(item => item.id)).size).toBe(items.length)
    expect(items.map(item => item.category)).toEqual([
      'foundation',
      'market',
      'market',
      'market',
      'kline',
      'kline',
      'kline',
      'kline',
      'kline',
      'kline',
      'kline',
      'market',
      'maintenance',
      'maintenance',
      'extension',
      'extension',
    ])
    expect(items.find(item => item.id === 'auction')).toMatchObject({
      label: '集合竞价',
      schedule: '交易日 09:25:15 / 30 / 45，09:29:15 兜底',
      detail: expect.stringContaining('多次采集与发布重试'),
    })
    expect(items.find(item => item.id === 'quotes')).toMatchObject({
      detail: '行情轮询运行中；交易日 09:15–11:35、13:00–15:15 轮询，午间暂停，15:00 后继续确认收盘定版。',
      schedule: '交易日 09:15–11:35 / 13:00–15:15 · 每 3 秒',
    })
    expect(items.find(item => item.id === 'second-quote-archive')).toMatchObject({
      label: '秒级行情',
      schedule: '交易日 09:15–09:25',
      state: 'enabled',
    })
    expect(items.find(item => item.id === 'ext:industry')).toMatchObject({
      state: 'disabled',
      schedule: '启用后立即拉取，后每 5 分钟',
    })
    expect(items.find(item => item.id === 'daily-pipeline')?.detail).toContain('启动后默认补齐当日')
    expect(items.find(item => item.id === 'period-1w')).toMatchObject({
      detail: '每周六由本地日K聚合，不向上游重复请求。',
      schedule: '每周六 09:00',
    })
    expect(items.find(item => item.id === 'startup-integrity')).toBeUndefined()
    expect(items.find(item => item.id === 'kline-correction')).toBeUndefined()
    expect(items.find(item => item.id === 'period-1d')).toMatchObject({
      label: '日K周期同步', schedule: '交易日 15:35', state: 'enabled',
    })
    expect(items.find(item => item.id === 'daily-pipeline')?.schedule).toBe('启动后 · 交易日 15:35')
    expect(items.filter(item => item.category === 'kline').at(-1)).toMatchObject({
      id: 'daily-pipeline', label: '盘后数据管理', schedule: '启动后 · 交易日 15:35', state: 'enabled',
    })
    expect(items.find(item => item.id === 'financial-metrics')?.label).toBe('财务指标')
    expect(items.find(item => item.id === 'capability-reprobe')?.label).toBe('数据源能力')
  })

  it('keeps fixed tasks visible while marking auto-sync dependent work disabled', () => {
    const items = buildDataScheduleItems({
      preferences: { ...preferences, data_sync_auto_enabled: false },
      klineConfigs,
      minuteRefresh: { available: true, capability_ok: true, enabled: true, running: true },
      quoteStatus: { running: true, is_trading_hours: false, interval_s: 3 },
      realtimeAvailable: true,
      financialAvailable: true,
      depthAvailable: true,
      extConfigs: [],
    })

    expect(items.find(item => item.id === 'catalog')?.state).toBe('enabled')
    expect(items.find(item => item.id === 'capability-reprobe')?.state).toBe('enabled')
    expect(items.find(item => item.id === 'daily-pipeline')?.state).toBe('disabled')
    expect(items.find(item => item.id === 'period-1d')?.state).toBe('disabled')
    expect(items.find(item => item.id === 'minute-refresh')?.state).toBe('disabled')
    expect(items.find(item => item.id === 'financial-metrics')?.state).toBe('disabled')
  })

  it('keeps daily missing-only management fixed even when legacy period settings differ', () => {
    const items = buildDataScheduleItems({
      preferences: { ...preferences, pipeline_schedule: { hour: 17, minute: 10 } },
      klineConfigs: { ...klineConfigs, '1d': { ...klineConfigs['1d'], enabled: false, time: '17:10' } },
      financialAvailable: true,
      depthAvailable: true,
      extConfigs: [],
    })
    expect(items.find(item => item.id === 'period-1d')).toMatchObject({
      schedule: '交易日 15:35', state: 'enabled',
    })
    expect(items.find(item => item.id === 'daily-pipeline')).toMatchObject({
      schedule: '启动后 · 交易日 15:35', state: 'enabled',
    })
  })

  it('marks capability-gated data tasks unavailable instead of implying that they will run', () => {
    const items = buildDataScheduleItems({
      preferences,
      klineConfigs,
      minuteRefresh: { available: true, capability_ok: true, enabled: true, running: false },
      quoteStatus: { running: false, is_trading_hours: false, interval_s: 3 },
      realtimeAvailable: false,
      financialAvailable: true,
      depthAvailable: false,
      extConfigs: [],
    })

    expect(items.find(item => item.id === 'quotes')?.state).toBe('unavailable')
    expect(items.find(item => item.id === 'second-quote-archive')?.state).toBe('unavailable')
    expect(items.find(item => item.id === 'depth')?.state).toBe('unavailable')
  })

  it('reports the unified two-session window for builtin extension data', () => {
    const items = buildDataScheduleItems({
      preferences,
      klineConfigs,
      minuteRefresh: { available: true, capability_ok: true, enabled: true, running: false },
      quoteStatus: { running: false, is_trading_hours: false, interval_s: 3 },
      realtimeAvailable: true,
      financialAvailable: true,
      depthAvailable: true,
      extConfigs: [{
        id: 'ext_gn_ths',
        label: '扩展概念',
        pull: {
          url: 'https://example.test/concepts',
          enabled: true,
          schedule_minutes: 1,
          time_window_start: '09:15',
          time_window_end: '16:00',
        },
      }],
    })

    expect(items.find(item => item.id === 'ext:ext_gn_ths')?.detail).toBe(
      '仅在交易日 09:15–11:35 / 13:00–15:15 拉取。',
    )
  })

  it('uses the initialization defaults before configurations load', () => {
    const items = buildDataScheduleItems({ financialAvailable: true, depthAvailable: true, extConfigs: [] })
    expect(items.find(item => item.id === 'period-1d')?.schedule).toBe('交易日 15:35')
    expect(items.find(item => item.id === 'period-1w')?.schedule).toBe('每周六 09:00')
    expect(items.find(item => item.id === 'minute-refresh')?.schedule).toContain('交易日 09:30–11:30 / 13:00–15:00')
  })

  it('shows separate generation windows and the one-minute dependency despite legacy settings', () => {
    const items = buildDataScheduleItems({ klineConfigs, financialAvailable: true, depthAvailable: true, extConfigs: [] })
    for (const [period, window] of [
      ['5m', '09:36–11:31 / 13:06–15:01'],
      ['10m', '09:42–11:32 / 13:12–15:02'],
      ['15m', '09:48–11:33 / 13:18–15:03'],
    ]) {
      expect(items.find(item => item.id === `period-${period}`)).toMatchObject({
        label: `${period.slice(0, -1)}分K盘中刷新`,
        detail: expect.stringContaining('依赖1分K'),
        schedule: expect.stringContaining(`交易日 ${window}`),
        scheduleLabel: '自动生成周期',
      })
    }
  })
})
