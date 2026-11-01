import type { CustomSignal, LateSessionStrengthParams, MorningLiquidationParams } from './api'

export const LATE_SESSION_DEFAULTS: LateSessionStrengthParams = {
  time: '14:30:00', change_min_pct: 0.4, change_max_pct: 4,
  pool_up_min_pct: 60, ma5_1m_count: 7, ma5_5m_count: 4,
}

export const MORNING_LIQUIDATION_DEFAULTS: MorningLiquidationParams = {
  time: '10:30:00', amplitude_pct: 6,
}

export const customSignalColumn = (signal: CustomSignal) =>
  `${signal.timeframe === 'intraday' ? 'csgi_' : 'csg_'}${signal.id}`

export function lateSessionSummary(signal: CustomSignal): string[] {
  const p = { ...LATE_SESSION_DEFAULTS, ...signal.params as Partial<LateSessionStrengthParams> }
  return [
    `判断时间 ${p.time}（北京时间）`,
    `该时刻涨幅 ${p.change_min_pct}%～${p.change_max_pct}%`,
    `当日股票池上涨数 / 总数 ≥ ${p.pool_up_min_pct}%`,
    `最近 ${p.ma5_1m_count} 个 1分钟 MA5 严格递增`,
    `最近 ${p.ma5_5m_count} 个 5分钟 MA5 严格递增`,
  ]
}

export function morningLiquidationSummary(signal: CustomSignal): string[] {
  const p = { ...MORNING_LIQUIDATION_DEFAULTS, ...signal.params as Partial<MorningLiquidationParams> }
  return [
    `买入后下一个交易日 ${p.time} 前，振幅 ≥ ${p.amplitude_pct}% 提前清仓`,
    `未达到阈值，在 ${p.time} 触发清仓（北京时间）`,
    '振幅 =（当日至当前最高价 − 最低价）/ 昨收 × 100%',
  ]
}
