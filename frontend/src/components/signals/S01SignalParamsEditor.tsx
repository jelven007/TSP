import { TimePicker } from '@/components/TimePicker'
import type { CustomSignal, S01EntryParams, S01ExitParams } from '@/lib/api'
import { lateSessionBarTime, lateSessionCheckTime, S01_VALID_SECURITY_DESCRIPTION } from '@/lib/customSignals'

const ENTRY_FIELDS = [
  ['market_positive_min', '当日市场上涨比例下限（%）', 100, .1],
  ['current_amount_min', '累计成交额下限（亿元）', 1e-8, .01],
  ['current_range_max', '振幅上限（%）', 100, .01],
  ['current_return_min', '涨幅下限（%）', 100, .01],
  ['current_return_max', '涨幅上限（%）', 100, .01],
] as const
const EXIT_FIELDS = [
  ['decline_3m', '3分钟跌幅门槛（%）', 100, .01],
  ['volume_ratio_min', '最近3根/之前10根量比', 1, .1],
] as const
const LEGACY_EXIT_FIELDS = [
  ['vwap_buffer', 'VWAP缓冲（%）', 100, .01],
  ['peak_profit_min', '最低峰值盈利（%）', 100, .01],
  ['peak_retrace_min', '最低峰值回撤（%）', 100, .01],
  ['return_15m_max', '15分钟涨幅上限（%）', 100, .01],
  ...EXIT_FIELDS,
] as const

export function S01SignalParamsEditor({ signal, onChange }: {
  signal: CustomSignal
  onChange: (params: S01EntryParams | S01ExitParams) => void
}) {
  const entry = signal.template === 's01_late_entry'
  const values = signal.params as unknown as Record<string, number | string>
  const legacyExit = !entry && values?.guard_time != null && values?.start_time == null
  const update = (key: string, value: number | string) =>
    onChange({ ...values, [key]: value } as unknown as S01EntryParams | S01ExitParams)
  return <div className="space-y-3">
    <p className="text-[11px] leading-5 text-muted">{entry
      ? '启动时计算策略股票范围内的当日市场上涨比例；确认延时结束后计算个股累计成交额、振幅及涨幅。前日条件在策略数值过滤中设置。'
      : legacyExit ? '此信号沿用已保存的旧参数与时点；编辑和保存保留该定义。'
        : '次交易日从启动时间至止损前，只检查放量下跌：3分钟跌幅和量比须同时满足以下门槛。止损时间到无条件全部清仓，兜底时间再次处理未成交余股。'}</p>
    {entry && <p className="text-[11px] leading-5 text-muted">
      {S01_VALID_SECURITY_DESCRIPTION}
      市场上涨比例=上述有效证券中最新已完成收盘价高于昨收的数量 / 有效证券数量。
    </p>}
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
      {entry && <>
        <label className="space-y-1.5">
          <span className="text-[11px] text-muted">启动时间（北京时间）</span>
          <TimePicker aria-label="启动时间" showSeconds align="left" value={String(values?.time ?? '')}
            onChange={time => update('time', time)}
            className="w-full" buttonClassName="h-9 w-full justify-start" />
        </label>
        <label className="space-y-1.5">
          <span className="text-[11px] text-muted">确认延时（秒）</span>
          <input type="number" aria-label="确认延时（秒）" min={30} max={900} step={30}
            value={Number.isFinite(Number(values?.evaluation_delay_minutes)) ? Number(values.evaluation_delay_minutes) * 60 : ''}
            onChange={event => update('evaluation_delay_minutes', event.target.value === '' ? NaN : Number(event.target.value) / 60)}
            className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs text-foreground" />
        </label>
      </>}
      {(entry ? ENTRY_FIELDS : legacyExit ? LEGACY_EXIT_FIELDS : EXIT_FIELDS).map(([key, label, scale, step]) => <label key={key} className="space-y-1.5">
        <span className="text-[11px] text-muted">{label}</span>
        <input type="number" aria-label={label} step={step}
          min={key === 'return_15m_max' ? undefined : 0}
          max={key === 'market_positive_min' ? 100 : undefined}
          value={Number.isFinite(Number(values?.[key])) ? Number((Number(values[key]) * scale).toFixed(6)) : ''}
          onChange={event => update(key, event.target.value === '' ? NaN : Number((Number(event.target.value) / scale).toFixed(10)))}
          className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs text-foreground" />
      </label>)}
      {!entry && (legacyExit ? [
        ['guard_time', '兜底信号时间'], ['sell_deadline', '成交截止时间'],
      ] as const : [
        ['start_time', '启动时间'], ['stop_time', '止损时间'], ['fallback_time', '兜底时间'],
      ] as const).map(([key, label]) => <label key={key} className="space-y-1.5">
        <span className="text-[11px] text-muted">{label}（北京时间）</span>
        <TimePicker aria-label={label} showSeconds={!legacyExit}
          value={String(values?.[key] ?? '')} onChange={time => update(key, legacyExit ? time.slice(0, 5) : time)}
          align="left" className="w-full" buttonClassName="h-9 w-full justify-start" />
      </label>)}
    </div>
    {entry && <p className="text-[11px] leading-5 text-muted">
      确认延时30～900秒，须为30秒的整数倍，默认60秒。
      当前确认时间 {lateSessionCheckTime({ time: String(values?.time ?? ''),
        evaluation_delay_minutes: Number(values?.evaluation_delay_minutes) })}；
      K线截止 {lateSessionBarTime(String(values?.time ?? ''))}，只使用已完成数据，不使用启动后的分钟数据。
      个股累计使用已完成的可用分钟；重复时标不重复累计，不补造缺行情。振幅=（最高价－最低价）/ 昨收。
    </p>}
    {!entry && !legacyExit && <p className="text-[11px] leading-5 text-muted">时间须为整分钟，秒数为00；11:15≤启动＜止损≤11:28，兜底13:01～14:55。信号后下一分钟起最多五个交易分钟处理余股；13:30兜底对应13:31～13:35。停牌、跌停、T+1及容量限制仍适用。</p>}
  </div>
}
