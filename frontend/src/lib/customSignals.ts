import type { CustomSignal, LateSessionStrengthParams, MorningLiquidationParams, S01EntryParams, S01ExitParams, S01VolumeExitParams } from './api'

const signalNameOrder = new Intl.Collator('zh-CN', { numeric: true, sensitivity: 'base' })
export const sortSignalsByName = (signals: CustomSignal[]) =>
  [...signals].sort((a, b) => signalNameOrder.compare(a.name, b.name) || a.id.localeCompare(b.id))

export const LATE_SESSION_DEFAULTS: LateSessionStrengthParams = {
  time: '14:30:00', change_min_pct: 0.4, change_max_pct: 4,
  pool_up_min_pct: 60, ma5_1m_count: 7, ma5_5m_count: 4,
  evaluation_delay_minutes: 0.5,
  amount_min_100m: null, turnover_min_pct: null, amplitude_max_pct: null,
  volume_ratio_min: null, end_time: null,
}

export const MORNING_LIQUIDATION_DEFAULTS: Required<Omit<MorningLiquidationParams, 'amplitude_pct'>> = {
  start_time: null, decline_3m_pct: null, volume_ratio_min: null,
  take_profit_pct: null, stop_loss_pct: null,
  time: '10:30:00', fallback_time: '10:45:00', rise_pct: 6, fall_pct: 6,
}

export function morningLiquidationDraft(signal: CustomSignal) {
  const p = { ...MORNING_LIQUIDATION_DEFAULTS, ...signal.params } as MorningLiquidationParams
  // 编辑保存时显式转换旧振幅；读取和取消不会改写已保存定义。
  return { start_time: p.start_time ?? null, rise_pct: p.rise_pct === undefined ? 6 : p.rise_pct,
    fall_pct: p.fall_pct === undefined ? 6 : p.fall_pct,
    decline_3m_pct: p.decline_3m_pct ?? null, volume_ratio_min: p.volume_ratio_min ?? null,
    take_profit_pct: p.take_profit_pct ?? null, stop_loss_pct: p.stop_loss_pct ?? null,
    time: p.time, fallback_time: p.fallback_time === undefined ? '10:45:00' : p.fallback_time }
}

export const S01_EXIT_DEFAULTS: S01VolumeExitParams = {
  decline_3m: .002, volume_ratio_min: 1.5,
  start_time: '11:15:00', stop_time: '11:26:00', fallback_time: '13:30:00',
}

export const isS01Signal = (signal: CustomSignal) =>
  signal.template === 's01_late_entry' || signal.template === 's01_morning_exit'

export const parameterSignalValues = (signals?: Partial<Record<'entry' | 'exit', CustomSignal>>) =>
  ({ ...(signals?.entry?.params ?? {}), ...(signals?.exit?.params ?? {}) })

export function s01ExitDraft(signal: CustomSignal): S01VolumeExitParams {
  const stored = (signal.params ?? {}) as Partial<S01ExitParams>
  return {
    decline_3m: stored.decline_3m ?? S01_EXIT_DEFAULTS.decline_3m,
    volume_ratio_min: stored.volume_ratio_min ?? S01_EXIT_DEFAULTS.volume_ratio_min,
    start_time: stored.start_time ?? S01_EXIT_DEFAULTS.start_time,
    stop_time: stored.stop_time ?? (stored.guard_time ? `${stored.guard_time}:00` : S01_EXIT_DEFAULTS.stop_time),
    fallback_time: stored.fallback_time ?? (stored.sell_deadline ? `${stored.sell_deadline}:00` : S01_EXIT_DEFAULTS.fallback_time) }
}

export function s01SignalSummary(signal: CustomSignal): string[] {
  if (!signal.params || signal.error) return [signal.error ?? 'S01参数尚不可用']
  if (signal.template === 's01_late_entry') {
    const p = signal.params as S01EntryParams & { time?: string }
    return [
      `截至${p.time ?? '14:45'}使用已完成的可用分钟累计值；前日条件在策略数值过滤中设置`,
      `当日涨幅 ${p.current_return_min * 100}%～${p.current_return_max * 100}%，振幅 ≤ ${p.current_range_max * 100}%，成交额 ≥ ${p.current_amount_min / 1e8}亿元`,
      `市场有效证券 ≥ ${p.market_min_valid}只，上涨比例 ≥ ${p.market_positive_min * 100}%`,
    ]
  }
  const p = signal.params as S01ExitParams
  return [
    p.start_time
      ? `启动时间 ${p.start_time}，止损时间 ${p.stop_time}，兜底时间 ${p.fallback_time}（北京时间，次交易日）`
      : `旧冻结规则：11:15判断，${p.guard_time}无条件触发，${p.sell_deadline}成交截止`,
    ...(p.vwap_buffer == null ? [] : [
      `低于均价 ${p.vwap_buffer * 100}% 或最高浮盈 ≥ ${p.peak_profit_min! * 100}% 且回落 ≥ ${p.peak_retrace_min! * 100}%，同时最后15分钟涨幅 ≤ ${p.return_15m_max! * 100}%`,
    ]),
    `${p.vwap_buffer == null ? '放量下跌须同时满足：' : ''}3分钟跌幅 ≥ ${p.decline_3m * 100}%，最近3根/之前10根量比 ≥ ${p.volume_ratio_min}`,
    ...(p.start_time ? ['止损时点无条件全部清仓；兜底再次处理余股，信号与实际成交分别记录'] : []),
  ]
}

export const customSignalColumn = (signal: CustomSignal) =>
  `${signal.timeframe === 'intraday' ? 'csgi_' : 'csg_'}${signal.id}`

function clockTime(total: number): string {
  return [Math.floor(total / 3600), Math.floor(total / 60) % 60, total % 60]
    .map(value => String(value).padStart(2, '0')).join(':')
}

export function lateSessionCheckTime(p: LateSessionStrengthParams): string {
  if (p.time == null) return '按当前已完成分钟确认'
  const [hour, minute, second = 0] = p.time.split(':').map(Number)
  return clockTime(hour * 3600 + minute * 60 + second + (p.evaluation_delay_minutes ?? 0) * 60)
}

export const lateSessionLatestStartTime = (delaySeconds: number): string => clockTime(15 * 3600 - delaySeconds)

export const lateSessionBarTime = (time: string | null): string =>
  time == null ? '当前已完成分钟' : `${time.slice(0, 5)}:00`

export function lateSessionSummary(signal: CustomSignal): string[] {
  const stored = signal.params as Partial<LateSessionStrengthParams> | undefined
  const p = { ...LATE_SESSION_DEFAULTS, ...stored,
    evaluation_delay_minutes: stored?.evaluation_delay_minutes === undefined ? 0 : stored.evaluation_delay_minutes }
  const barTime = lateSessionBarTime(p.time)
  return [
    p.time == null
      ? '启动时间 未设置，按当前已完成分钟'
      : `${p.evaluation_delay_minutes === 0 ? '旧判断时间' : '启动时间'} ${p.time}（北京时间）`,
    p.evaluation_delay_minutes
      ? `确认延迟 ${p.evaluation_delay_minutes * 60}秒 · 确认时间 ${lateSessionCheckTime(p)}`
      : '确认延迟 未设置',
    `K线截止 ${barTime}`,
    ...(p.pool_up_min_pct == null ? [] : [`股票池有效成员上涨占比 ≥ ${p.pool_up_min_pct}%`]),
    ...(p.amount_min_100m == null ? [] : [`当日成交额 ≥ ${p.amount_min_100m}亿元`]),
    ...(p.turnover_min_pct == null ? [] : [`当日换手率 ≥ ${p.turnover_min_pct}%`]),
    ...(p.amplitude_max_pct == null ? [] : [`当日振幅 ≤ ${p.amplitude_max_pct}%`]),
    ...(p.change_min_pct == null ? [] : [`当日涨幅 ≥ ${p.change_min_pct}%`]),
    ...(p.change_max_pct == null ? [] : [`当日涨幅 ≤ ${p.change_max_pct}%`]),
    ...(p.volume_ratio_min == null ? [] : [`当日量比 ≥ ${p.volume_ratio_min}`]),
    ...(p.ma5_1m_count == null ? [] : [`1分钟MA5 连增 ${p.ma5_1m_count} 个值（${p.ma5_1m_count - 1} 次比较）`]),
    ...(p.ma5_5m_count == null ? [] : [`5分钟MA5 连增 ${p.ma5_5m_count} 个值（${p.ma5_5m_count - 1} 次比较）`]),
    ...(p.end_time == null ? [] : [`买入结束时间 ${p.end_time}（北京时间），超过停止买入并撤销余单`]),
    ...(p.entry_groups == null ? [] : [
      `A/B入场筛选：${[p.entry_groups.a.enabled ? 'A组' : '', p.entry_groups.b.enabled ? 'B组' : ''].filter(Boolean).join(' 或 ')}；组内全部满足，双命中按A组优先`,
      '逐股完成A/B判断后按策略保存的买入顺序执行；具体阈值见编辑配置',
    ]),
  ]
}

export function morningLiquidationSummary(signal: CustomSignal): string[] {
  const stored = signal.params as Partial<MorningLiquidationParams> | undefined
  if (stored?.amplitude_pct != null) return [
    `旧振幅 ≥ ${stored.amplitude_pct}%，次交易日 ${stored.time ?? '10:30:00'} 前提前清仓`,
    `未达到阈值，在 ${stored.time ?? '10:30:00'} 触发清仓（北京时间）`,
    '旧振幅 =（当日至当前最高价 − 最低价）/ 昨收 × 100%；编辑保存后转换为涨跌幅规则',
  ]
  const p = morningLiquidationDraft(signal)
  if (Object.values(p).every(value => value == null)) return ['全部参数为空，早盘清仓模板不触发出场']
  const returns = [
    ...(p.rise_pct == null ? [] : [`涨幅 ≥ ${p.rise_pct}%`]),
    ...(p.fall_pct == null ? [] : [`跌幅 ≥ ${p.fall_pct}%`]),
  ]
  const group = [
    ...(p.decline_3m_pct == null ? [] : [`3分钟跌幅 ≥ ${p.decline_3m_pct}%`]),
    ...(p.volume_ratio_min == null ? [] : [`最近3根/之前10根量比 ≥ ${p.volume_ratio_min}`]),
  ]
  const holdingReturns = [
    ...(p.take_profit_pct == null ? [] : [`止盈 ≥ ${p.take_profit_pct}%`]),
    ...(p.stop_loss_pct == null ? [] : [`止损 ≥ ${p.stop_loss_pct}%`]),
  ]
  return [
    `买入后下一个交易日${p.start_time == null ? '，启动时间未设置' : `，启动时间 ${p.start_time}`}（北京时间）`,
    ...(returns.length ? [`现价相对原始昨收：${returns.join(' 或 ')} 提前清仓`] : []),
    ...(group.length ? [`${returns.length ? '或' : ''}${group.join(' 且 ')}，使用已完成连续分钟`] : []),
    ...(holdingReturns.length ? [`相对买入成交均价：${holdingReturns.join(' 或 ')}；仅信号名单按冻结买入参考价`] : []),
    `止损时间${p.time == null ? '未启用' : ` ${p.time}`}触发清仓；兜底时间${p.fallback_time == null ? '未启用' : ` ${p.fallback_time}`}，到点立即停止所有股票卖出`,
  ]
}
