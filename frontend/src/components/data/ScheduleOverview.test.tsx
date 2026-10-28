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
      'startup-integrity',
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
      schedule: '交易日 09:25:08 / 25 / 45，09:29:15 定版',
      detail: expect.stringContaining('多次采集与发布重试'),
    })
    expect(items.find(item => item.id === 'quotes')).toMatchObject({
      detail: '行情轮询运行中；交易日 09:15–11:45、13:00–16:00 轮询，午间暂停，15:00 后继续确认收盘定版。',
      schedule: '交易日 09:15–11:45 / 13:00–16:00 · 每 3 秒',
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
    expect(items.find(item => item.id === 'daily-pipeline')?.detail).toContain('首次启动按需补齐')
    expect(items.find(item => item.id === 'period-1w')).toMatchObject({
      detail: '每周六由本地日K聚合，不向上游重复请求。',
      schedule: '每周六 16:10 后',
    })
    expect(items.find(item => item.id === 'startup-integrity')).toMatchObject({
      label: '数据完整性',
      schedule: '应用启动后 30 秒',
      state: 'enabled',
    })
    expect(items.find(item => item.id === 'period-1d')).toMatchObject({
      label: '日K周期同步', schedule: '交易日 16:00', state: 'enabled',
    })
    expect(items.find(item => item.id === 'daily-pipeline')?.schedule).toBe('交易日 16:00')
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
    expect(items.find(item => item.id === 'startup-integrity')?.state).toBe('disabled')
    expect(items.find(item => item.id === 'daily-pipeline')?.state).toBe('disabled')
    expect(items.find(item => item.id === 'period-1d')?.state).toBe('disabled')
    expect(items.find(item => item.id === 'minute-refresh')?.state).toBe('disabled')
    expect(items.find(item => item.id === 'financial-metrics')?.state).toBe('disabled')
  })

  it('keeps the daily entry aligned with the saved pipeline schedule and daily switch', () => {
    const items = buildDataScheduleItems({
      preferences: { ...preferences, pipeline_schedule: { hour: 17, minute: 10 } },
      klineConfigs: { ...klineConfigs, '1d': { ...klineConfigs['1d'], enabled: false, time: '17:10' } },
      financialAvailable: true,
      depthAvailable: true,
      extConfigs: [],
    })
    for (const id of ['period-1d', 'daily-pipeline']) {
      expect(items.find(item => item.id === id)).toMatchObject({
        schedule: '交易日 17:10', state: 'disabled',
      })
    }
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
      '仅在交易日 09:15–11:45 / 13:00–16:00 拉取。',
    )
  })
})
