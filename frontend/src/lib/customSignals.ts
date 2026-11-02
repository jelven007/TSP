import type { CustomSignal, LateSessionStrengthParams, MorningLiquidationParams } from './api'

export const LATE_SESSION_DEFAULTS: LateSessionStrengthParams = {
  time: '14:30:00', change_min_pct: 0.4, change_max_pct: 4,
  pool_up_min_pct: 60, ma5_1m_count: 7, ma5_5m_count: 4,
}

export const MORNING_LIQUIDATION_DEFAULTS: Required<Omit<MorningLiquidationParams, 'amplitude_pct'>> = {
  time: '10:30:00', fallback_time: '10:45:00', rise_pct: 6, fall_pct: 6,
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
  const stored = signal.params as Partial<MorningLiquidationParams> | undefined
  if (stored?.amplitude_pct != null) return [
    `旧振幅 ≥ ${stored.amplitude_pct}%，次交易日 ${stored.time ?? '10:30:00'} 前提前清仓`,
    `未达到阈值，在 ${stored.time ?? '10:30:00'} 触发清仓（北京时间）`,
    '旧振幅 =（当日至当前最高价 − 最低价）/ 昨收 × 100%；编辑保存后转换为涨跌幅规则',
  ]
  const p = { ...MORNING_LIQUIDATION_DEFAULTS, ...stored }
  return [
    `止损时间 ${p.time}，兜底时间 ${p.fallback_time}（北京时间，买入后下一个交易日）`,
    `现价相对原始昨收：涨幅 ≥ ${p.rise_pct}% 或跌幅 ≥ ${p.fall_pct}% 提前清仓`,
    '止损时间到无条件清仓；兜底时间再次处理余股，成交受停牌、跌停及容量限制',
  ]
}
