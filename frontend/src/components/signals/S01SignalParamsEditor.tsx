import { TimePicker } from '@/components/TimePicker'
import type { CustomSignal, S01EntryParams, S01ExitParams } from '@/lib/api'

const ENTRY_FIELDS = [
  ['current_amount_min', '当日累计成交额下限（亿元）', 1e-8, .01],
  ['current_return_min', '当日涨幅下限（%）', 100, .01],
  ['current_return_max', '当日涨幅上限（%）', 100, .01],
  ['current_range_max', '当日上下振幅上限（%）', 100, .01],
  ['market_min_valid', '市场有效证券下限（只）', 1, 1],
  ['market_positive_min', '市场上涨比例下限（%）', 100, .1],
] as const
const EXIT_FIELDS = [
  ['decline_3m', '3分钟跌幅门槛（%）', 100, .01],
  ['volume_ratio_min', '最近3根/之前10根量比', 1, .1],
] as const

export function S01SignalParamsEditor({ signal, onChange }: {
  signal: CustomSignal
  onChange: (params: S01EntryParams | S01ExitParams) => void
}) {
  const entry = signal.template === 's01_late_entry'
  const values = signal.params as unknown as Record<string, number | string>
  const update = (key: string, value: number | string) =>
    onChange({ ...values, [key]: value } as unknown as S01EntryParams | S01ExitParams)
  return <div className="space-y-3">
    <p className="text-[11px] leading-5 text-muted">{entry
      ? '14:45使用已完成的可用分钟累计值；当日上下振幅=（最高价－最低价）/ 昨收。前日条件在策略数值过滤中设置。'
      : '次交易日从启动时间至止损前，只检查放量下跌：3分钟跌幅和量比须同时满足以下门槛。止损时间到无条件全部清仓，兜底时间再次处理未成交余股。'}</p>
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
      {(entry ? ENTRY_FIELDS : EXIT_FIELDS).map(([key, label, scale, step]) => <label key={key} className="space-y-1.5">
        <span className="text-[11px] text-muted">{label}</span>
        <input type="number" aria-label={label} step={step}
          min={key === 'market_min_valid' ? 1 : 0}
          max={key === 'market_positive_min' ? 100 : undefined}
          value={Number.isFinite(Number(values?.[key])) ? Number((Number(values[key]) * scale).toFixed(6)) : ''}
          onChange={event => update(key, event.target.value === '' ? NaN : Number((Number(event.target.value) / scale).toFixed(10)))}
          className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs text-foreground" />
      </label>)}
      {!entry && ([
        ['start_time', '启动时间'], ['stop_time', '止损时间'], ['fallback_time', '兜底时间'],
      ] as const).map(([key, label]) => <label key={key} className="space-y-1.5">
        <span className="text-[11px] text-muted">{label}（北京时间）</span>
        <TimePicker aria-label={label} showSeconds
          value={String(values?.[key] ?? '')} onChange={time => update(key, time)}
          align="left" className="w-full" buttonClassName="h-9 w-full justify-start" />
      </label>)}
    </div>
    {!entry && <p className="text-[11px] leading-5 text-muted">时间须为整分钟，秒数为00；11:15≤启动＜止损≤11:28，兜底13:01～14:55。信号后下一分钟起最多五个交易分钟处理余股；13:30兜底对应13:31～13:35。停牌、跌停、T+1及容量限制仍适用。</p>}
  </div>
}
