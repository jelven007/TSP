import type { CustomSignal, LateSessionStrengthParams, MorningLiquidationParams, S01EntryParams, S01ExitParams } from './api'

export const isS01Signal = (signal: CustomSignal) =>
  signal.template === 's01_late_entry' || signal.template === 's01_morning_exit'

export const parameterSignalValues = (signals?: Partial<Record<'entry' | 'exit', CustomSignal>>) =>
  ({ ...(signals?.entry?.params ?? {}), ...(signals?.exit?.params ?? {}) })

const pct = (value: number) => `${Number((value * 100).toFixed(4))}%`
export const S01_ENTRY_TIMING_DEFAULTS = { time: '14:45:00', evaluation_delay_minutes: 1 }
export const S01_ENTRY_MARKET_VALIDITY = 'price_amount_prev_close_v2' as const
export const S01_VALID_SECURITY_DESCRIPTION = '有效证券：截止启动时间的当日每根1分钟K线的开、高、低、收价格均为有效正数，成交额有效且非负，允许为0；上一个交易日有收盘价且有效。'

export function s01SignalSummary(signal: CustomSignal): string[] {
  if (signal.error) return [signal.error]
  if (signal.template === 's01_late_entry') {
    const p = signal.params as S01EntryParams
    return [
      p.time ? `启动时间 ${p.time}，确认延时 ${p.evaluation_delay_minutes! * 60}秒，确认时间 ${lateSessionCheckTime({ time: p.time, evaluation_delay_minutes: p.evaluation_delay_minutes })}，K线截止 ${lateSessionBarTime(p.time)}（北京时间）`
        : '14:45判断，要求当日完整225根已完成分钟K',
      `当日市场有效证券 ≥ ${p.market_min_valid}只，当日市场上涨比例 ≥ ${pct(p.market_positive_min)}`,
      ...(p.market_validity === S01_ENTRY_MARKET_VALIDITY ? [S01_VALID_SECURITY_DESCRIPTION,
        '市场上涨比例：上述有效证券中，最新已完成收盘价高于上一个交易日收盘价的数量 / 有效证券数量'] : []),
      `累计成交额 ≥ ${p.current_amount_min / 1e8}亿元，振幅 ≤ ${pct(p.current_range_max)}（最高价－最低价）/ 昨收`,
      `涨幅 ${pct(p.current_return_min)}～${pct(p.current_return_max)}`,
      '前日价格、涨跌幅、振幅、成交额及上市天数在策略的数值过滤中设置',
    ]
  }
  const p = signal.params as S01ExitParams
  return [
    '买入后下一交易日11:15，使用完整105根早盘分钟K判断',
    `最后15分钟涨幅 ≤ ${pct(p.return_15m_max)}，且满足任一项：低于早盘均价 ≥ ${pct(p.vwap_buffer)}；最高浮盈 ≥ ${pct(p.peak_profit_min)}且自高点回落 ≥ ${pct(p.peak_retrace_min)}`,
    `11:15后3分钟跌幅 ≥ ${pct(p.decline_3m)}，且最近3根/之前10根成交量比 ≥ ${p.volume_ratio_min}`,
    `${p.guard_time}兜底触发；信号后下一分钟成交，最晚${p.sell_deadline}，午休不成交`,
  ]
}

export const LATE_SESSION_DEFAULTS: LateSessionStrengthParams = {
  time: '14:30:00', change_min_pct: 0.4, change_max_pct: 4,
  pool_up_min_pct: 60, ma5_1m_count: 7, ma5_5m_count: 4,
  evaluation_delay_minutes: 0.5,
}

export const MORNING_LIQUIDATION_DEFAULTS: Required<Omit<MorningLiquidationParams, 'amplitude_pct'>> = {
  time: '10:30:00', fallback_time: '10:45:00', rise_pct: 6, fall_pct: 6,
}

export const customSignalColumn = (signal: CustomSignal) =>
  `${signal.timeframe === 'intraday' ? 'csgi_' : 'csg_'}${signal.id}`

function clockTime(total: number): string {
  return [Math.floor(total / 3600), Math.floor(total / 60) % 60, total % 60]
    .map(value => String(value).padStart(2, '0')).join(':')
}

export function lateSessionCheckTime(p: Pick<LateSessionStrengthParams, 'time' | 'evaluation_delay_minutes'>): string {
  const [hour, minute, second = 0] = p.time.split(':').map(Number)
  return clockTime(hour * 3600 + minute * 60 + second + (p.evaluation_delay_minutes ?? 0) * 60)
}

export const lateSessionLatestStartTime = (delaySeconds: number): string => clockTime(15 * 3600 - delaySeconds)

export const lateSessionBarTime = (time: string): string => `${time.slice(0, 5)}:00`

export function lateSessionSummary(signal: CustomSignal): string[] {
  const stored = signal.params as Partial<LateSessionStrengthParams> | undefined
  const p = { ...LATE_SESSION_DEFAULTS, ...stored, evaluation_delay_minutes: stored?.evaluation_delay_minutes ?? 0 }
  const barTime = lateSessionBarTime(p.time)
  return [
    p.evaluation_delay_minutes
      ? `启动时间 ${p.time}，确认延迟 ${p.evaluation_delay_minutes * 60}秒，确认时间 ${lateSessionCheckTime(p)}，K线截止 ${barTime}（北京时间）`
      : `旧判断时间 ${p.time}（北京时间），未设置确认延迟；编辑时可选择30～900秒`,
    `截至 ${barTime} 的当日股票池有效上涨数 / 有效成员数 ≥ ${p.pool_up_min_pct}%`,
    `截至 ${barTime} 的涨幅 ${p.change_min_pct}%～${p.change_max_pct}%`,
    `最近 ${p.ma5_1m_count} 个 1分钟 MA5 严格递增（${p.ma5_1m_count - 1} 次比较）`,
    `最新完成的 ${p.ma5_5m_count} 个 5分钟 MA5 严格递增（${p.ma5_5m_count - 1} 次比较）`,
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
