import { TimePicker } from '@/components/TimePicker'
import type { CustomSignal, S01EntryParams, S01ExitParams } from '@/lib/api'
import { lateSessionBarTime, lateSessionCheckTime, S01_VALID_SECURITY_DESCRIPTION } from '@/lib/customSignals'

const ENTRY_FIELDS = [
  ['market_min_valid', '当日市场有效证券下限（只）', 1, 1],
  ['market_positive_min', '当日市场上涨比例下限（%）', 100, .1],
  ['current_amount_min', '累计成交额下限（亿元）', 1e-8, .01],
  ['current_range_max', '振幅上限（%）', 100, .01],
  ['current_return_min', '涨幅下限（%）', 100, .01],
  ['current_return_max', '涨幅上限（%）', 100, .01],
] as const
const EXIT_FIELDS = [
  ['vwap_buffer', '低于早盘均价幅度（%）', 100, .01],
  ['peak_profit_min', '早盘最高浮盈门槛（%）', 100, .01],
  ['peak_retrace_min', '早盘高点回落门槛（%）', 100, .01],
  ['return_15m_max', '最后15分钟涨幅上限（%）', 100, .01],
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
      ? '启动时计算当日市场有效证券和上涨比例；确认延时结束后计算个股累计成交额、振幅及涨幅。前日条件在策略数值过滤中设置。'
      : '次交易日11:15使用完整105根早盘K判断：最后15分钟涨幅达到上限，且满足均价偏离或浮盈回落条件；其后检查3分钟放量下跌，到兜底时间无条件触发。'}</p>
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
      {(entry ? ENTRY_FIELDS : EXIT_FIELDS).map(([key, label, scale, step]) => <label key={key} className="space-y-1.5">
        <span className="text-[11px] text-muted">{label}</span>
        <input type="number" aria-label={label} step={step}
          min={key === 'return_15m_max' ? undefined : key === 'market_min_valid' ? 1 : 0}
          max={key === 'market_positive_min' ? 100 : undefined}
          value={Number.isFinite(Number(values?.[key])) ? Number((Number(values[key]) * scale).toFixed(6)) : ''}
          onChange={event => update(key, event.target.value === '' ? NaN : Number((Number(event.target.value) / scale).toFixed(10)))}
          className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs text-foreground" />
      </label>)}
      {!entry && (['guard_time', 'sell_deadline'] as const).map(key => <label key={key} className="space-y-1.5">
        <span className="text-[11px] text-muted">{key === 'guard_time' ? '兜底信号时间（北京时间）' : '卖出成交截止时间（北京时间）'}</span>
        <TimePicker aria-label={key === 'guard_time' ? '兜底信号时间' : '卖出成交截止时间'}
          value={String(values?.[key] ?? '')} onChange={time => update(key, time.slice(0, 5))}
          align="left" className="w-full" buttonClassName="h-9 w-full justify-start" />
      </label>)}
    </div>
    {entry && <p className="text-[11px] leading-5 text-muted">
      确认延时30～900秒，须为30秒的整数倍，默认60秒。
      当前确认时间 {lateSessionCheckTime({ time: String(values?.time ?? ''),
        evaluation_delay_minutes: Number(values?.evaluation_delay_minutes) })}；
      K线截止 {lateSessionBarTime(String(values?.time ?? ''))}，只使用已完成数据，不使用启动后的分钟数据。
      个股指标仍要求完整、唯一的分钟窗口；默认截止14:45共225根。振幅=（最高价－最低价）/ 昨收。
    </p>}
    {!entry && <p className="text-[11px] leading-5 text-muted">兜底时间11:15～11:28；截止须晚于兜底，不得落在午休。信号后下一分钟按既有成交规则处理；停牌、跌停、T+1及容量限制仍适用。</p>}
  </div>
}
